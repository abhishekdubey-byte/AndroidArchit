# Smoothness & Reliability Upgrade — Roadmap

Index for the upgrade work that came out of the 2026-11-11 end-to-end test on a Redmi Note 5 Pro
(Android 9, MIUI 11, Snapdragon 636, Camera2 level LEGACY) driven from a Windows 11 laptop over USB,
with the companion app (debug build of v1.9.27, all permissions granted) connected through `adb reverse`.

Detailed, task-level plans exist for Phase 1 and Phase 2. Phases 3–5 are specified here at the
decision level and get their own task plans once Phase 2 lands, because they reuse its packet format
and browser player.

## Measured baseline (what the tests found)

| Area | Measured today | Target after upgrade |
|---|---|---|
| USB screen mirror (`screencap -p` per frame) | **0.29 fps**, 3.3 s per frame, steady over 6 min | ≥ 30 fps sustained 5 min (60 fps where the panel allows), < 100 ms glass-to-glass |
| ADB input (tap / key / text) | **0.75–3 s** per action | < 50 ms per action |
| Companion input (Accessibility) | ~100 ms key, ~300 ms tap/text | unchanged (already good) |
| Non-English text / emoji over ADB | silently dropped, API says "success" | typed correctly, or a clear 422 |
| UI tree over ADB (uiautomator) | always `[]` after 12 s on MIUI | companion tree (241 ms) or a clear 503 |
| Live camera (companion, JPEG frames) | 7.0 fps rear / 5.5 fps front, capped by 130 ms frame gap; steady with motion over USB | 30 fps H.264, bitrate-adaptive, no collapse on Wi-Fi/relay |
| Phone heat, 4-min camera stream | CPU sensor 45 → 55 °C, still rising | flat below ~48 °C after 10 min |
| Live recording to laptop | 8 fps, 640×640, ~50–750 kbps; save after stop 0.19 s (20 s clip) → 0.99 s (3 min clip), grows with length | 30 fps at stream quality; save < 0.5 s regardless of length |
| Headless video (companion MediaRecorder) | 1280×720 ≈ 28.6 fps, ~1.9 Mbps; **audio 8 kHz mono ≈ 12 kbps** | 48 kHz AAC ≥ 96 kbps |
| Headless photo | 1920×1080 in 2.3 s (sensor supports 4000×3000) | full sensor resolution option |
| Preview audio | 16 kHz mono PCM, 20 ms browser cushion, no A/V timestamps; 1.5 s stall seen | 48 kHz Opus/AAC, 80 ms jitter buffer, PTS-locked to video |
| Remote screen view (no USB) on Android 9 | **not available** ("needs Android 11") — MediaProjection service missing | works on Android 8+ |
| "Listen Live" button | placeholder: plays nothing (`app.js:1228`) | real phone-mic stream (Phase 4) or hidden until then |
| Server on Windows | needs `ADB_PATH` set by hand; `print("→")` crashes on cp1252 console; 11/66 unit tests fail | works out of the box; 66/66 tests pass |

Other confirmed defects (fixed in Phase 1): AI "open settings" fails over ADB (system apps missing from
app list) and reports false success over the companion (no foreground check); camera endpoints take
43–63 s to fail when the companion is missing; stale "OFFLINE" header after reconnect; `app.js:1704`
crash on 503; recording `name` holds the full Windows path; live-recording writes block the event loop
(`hub.py:229`); hard-coded real-looking number in `simulate_incoming`; storage at 95 % reported as
"audit normal".

## Phases

| Phase | Plan | Delivers | Rough effort |
|---|---|---|---|
| 1 | `2026-11-11-phase1-windows-and-correctness.md` | Windows-ready server, green test suite, honest APIs, AI/app-list fixes, non-blocking recording, dashboard fixes | 2–3 days |
| 2 | `2026-11-11-phase2-usb-h264-mirroring.md` | 30–60 fps USB mirroring via scrcpy-server (hardware H.264) + WebCodecs player; < 50 ms input incl. Unicode text | 4–5 days |
| 3 | to be written after Phase 2 | Companion live camera as hardware H.264 over the Phase-2 packet format; adaptive bitrate; thermal step-down; ML Kit off the live path | 3–4 days |
| 4 | to be written after Phase 3 | Instant-save recording, 48 kHz audio, PTS-based A/V sync, real "Listen Live", AudioWorklet mic | 3–4 days |
| 5 | to be written after Phase 2 | Remote screen view on Android 8–10 via MediaProjection (the declared-but-missing `ScreenCaptureService`) | 3 days |

Order: 1 → 2 → (3 and 5 can run in parallel, they touch different Kotlin files) → 4.

## Shared decisions for Phases 2–5

- **One video packet format everywhere** (USB scrcpy, companion camera, companion screen) so the
  browser has one player. Binary WebSocket message to dashboards:
  `[u8 kind][u8 idLen][idLen bytes device_id utf-8][u64 BE ptsMicros][payload]`
  with kind `1` = codec config (SPS/PPS, Annex-B), `2` = key frame, `3` = delta frame, `4` = audio config,
  `5` = audio packet. Defined in Phase 2 Task 3 (`server/app/services/media_packets.py`).
- **Browser decode with WebCodecs** (`VideoDecoder` / `AudioDecoder`), Annex-B input, `optimizeForLatency: true`;
  JPEG path kept only as a fallback for browsers without WebCodecs.
- **Never re-encode on the laptop.** Recording = remux the same H.264/AAC packets (`ffmpeg -c copy`,
  fragmented MP4) so stopping a recording only closes a file.
- **Backpressure = drop to next key frame**, never queue unbounded. Encoders run with a 2 s I-frame interval.

## Phase 3 decisions — companion live camera

- Camera2 `TEMPLATE_RECORD` → `MediaCodec` input `Surface` (zero-copy; removes `yuvToJpeg`/`rotateNv21`
  in `CameraPreviewStreamer.kt:190-262`). AE target FPS range `[30,30]` if advertised, else `[15,30]`
  (the test phone is LEGACY).
- 1280×720 @ 30 fps, `BITRATE_MODE_CBR` start 2.5 Mbps, adapt 0.8–4 Mbps with
  `PARAMETER_KEY_VIDEO_BITRATE` from the OkHttp queue size (today's guard drops frames at 2 MB,
  `CompanionWebSocketClient.kt:100`); `PARAMETER_KEY_REQUEST_SYNC_FRAME` when a viewer joins.
- Rotation via `KEY_ROTATION`/display transform metadata, not pixel rotation.
- Thermal step-down every 10 s: Android 10+ `PowerManager.getCurrentThermalStatus()`, Android 8–9 read
  `/sys/class/thermal/thermal_zone*/temp`; ladder 720p30 → 720p24 → 540p20 → 480p15.
- ML Kit (`SceneSense.kt`) only while HUNT is active, on a 320 px copy every 3 s.
- Send binary frames (`ByteString`) — no base64 (saves ~33 %).

## Phase 4 decisions — recording, audio, sync

- Phone: `AudioRecord` 48 kHz mono → `MediaCodec` AAC-LC 128 kbps; video and audio PTS both from
  `System.nanoTime()/1000` at capture.
- Laptop recorder replaces `cam_record.py`'s JPEG→libx264 pipeline with
  `ffmpeg -f h264 -i <video pipe> -f adts -i <audio pipe> -c copy -movflags +frag_keyframe+empty_moov`
  written incrementally; stop = close pipes (< 0.5 s for any length).
- Headless video audio: `MediaRecorder` audio 48 kHz / 128 kbps AAC (today 8 kHz, `BackgroundCameraService.kt:479-490`).
- Browser: `AudioDecoder` → `AudioWorklet` ring buffer, 80 ms target latency, video presented
  against the audio clock. Laptop mic: `AudioWorklet` + `getUserMedia({echoCancellation:true, noiseSuppression:true})`
  replacing `ScriptProcessor` (`app.js:1451`).
- "Listen Live" streams the phone microphone using the same audio packets.
- Platform limit to keep in the UI copy: stock Android does not let an app capture the caller's voice;
  the call bridge stays speaker-to-microphone.

## Phase 5 decisions — remote screen without USB

- Implement `ScreenCaptureService` (already declared in `AndroidManifest.xml:160-163`): MediaProjection
  consent once per session → `VirtualDisplay` → `MediaCodec` H.264, 720p, 24–30 fps, 1.5–3 Mbps,
  same packet format. Accessibility `takeScreenshot` stays as the fallback on Android 11+.

## How to measure acceptance

Phase 2 Task 6 adds `server/tools/bench_stream.py`, which reproduces the measurements above
(frames per 10 s window, frame-gap p95, payload KB/s, phone thermal zones every 30 s, input round-trip).
Every phase's last task runs it on a real phone over USB and records the numbers in the PR description.
