# Phase 3 — Companion Live Camera as Hardware H.264 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The companion's live camera streams hardware-encoded H.264 at 30 fps (adaptive bitrate, thermal step-down) instead of ~7 fps CPU-encoded base64 JPEG, so motion and Wi-Fi/relay links no longer collapse the frame rate or heat the phone, and recording the preview saves instantly at stream quality.

**Architecture:** On the phone, Camera2 renders into a `MediaCodec` AVC encoder's input `Surface` (zero-copy); a drain thread sends each encoded packet as a binary WebSocket frame `[u16 headerLen][JSON {"type":"CAMERA_PKT","k":1|2|3,"pts":µs}][Annex-B bytes]` (the framing FS_RES/PB already use). The laptop hub turns those into Phase 2 `VideoPacket`s on stream id `<device_id>#cam` and publishes them through `MediaFanout`; the dashboard's Live Preview decodes them with `H264Player`. A per-recording `H264Recorder` remuxes the same packets with `ffmpeg -c copy`. The JPEG path stays for old apps/browsers (negotiated by `codec` in START_CAMERA_STREAM).

**Tech Stack:** Kotlin (Camera2, MediaCodec, OkHttp), Python asyncio, ffmpeg (remux only), WebCodecs.

**Spec:** `docs/superpowers/plans/2026-11-11-upgrade-roadmap.md` ("Phase 3 decisions", baseline rows "Live camera", "Phone heat", "Live recording").

## Global Constraints

- Encoder: `video/avc`, 1280×720 (or the closest ≤ 1280×720 camera output size of the same aspect), 30 fps, `KEY_I_FRAME_INTERVAL` 2, `BITRATE_MODE_CBR` when supported else VBR, start 2.5 Mbps, adapt 0.8–4 Mbps; `KEY_PRIORITY` 0 (real-time) on API 23+.
- Camera: `TEMPLATE_RECORD`, AE target FPS range `[30,30]` if advertised else the highest `[x,30]`; the test phone is Camera2 LEGACY.
- No pixel rotation on the phone: the sensor orientation (and front-camera mirroring) is sent in `CAMERA_STREAM_STATE` and applied by the browser when drawing.
- Adaptive bitrate every 1 s from `webSocket.queueSize()`: > 512 KB → ×0.7; < 64 KB for 5 s → ×1.15; > 2 MB → skip sending deltas until the next key frame and request one (`PARAMETER_KEY_REQUEST_SYNC_FRAME`).
- Thermal step-down every 10 s: API 29+ `PowerManager.currentThermalStatus` ≥ `THERMAL_STATUS_MODERATE`, else max of `/sys/class/thermal/thermal_zone*/temp` for types containing `therm` ≥ 45 °C → AE range `[15,15]` and bitrate cap 1.5 Mbps; back to 30 fps after 60 s below the threshold.
- Backward compatibility both ways: an old app ignores `codec` and keeps sending JPEG (dashboard keeps rendering it); a browser without WebCodecs gets the JPEG path (the hub asks for `codec:"jpeg"`).
- Packet stream id on the laptop: `<device_id>#cam` (so it never mixes with the screen mirror's `<device_id>`).
- No new Python dependencies; tests: `cd server && .venv/Scripts/python -m unittest discover -s tests -t .`. App version → 1.9.28 / versionCode 56.
- One commit per task on `vivek/feature-updates`.

## Review Focus

1. Front camera: picture is mirrored and rotated correctly (sensor 270°), not upside down (manual, Task 5).
2. Lens switch mid-stream → new config packet; viewers and an active recording continue without a broken file (test in Task 3, manual in Task 5).
3. Relay/Wi-Fi congestion → bitrate falls, frame rate stays ~30, no socket death from OkHttp's 16 MB queue limit (Task 1 unit logic, manual Task 5 with `ACC` throttled link if available).
4. Old companion (JPEG only) + new laptop, and new companion + browser without WebCodecs → still a working preview (Task 2/4 tests).
5. Stop while a recording is open → recording finalised in < 0.5 s regardless of length, playable, audio kept (test in Task 3).

---

### Task 1: Phone — `CameraH264Streamer` (Kotlin)

**Files:**
- Create: `android-companion/app/src/main/java/com/operator/companion/service/CameraH264Streamer.kt`
- Create: `android-companion/app/src/main/java/com/operator/companion/service/BitrateController.kt` (pure logic, unit-testable)
- Test: `android-companion/app/src/test/java/com/operator/companion/service/BitrateControllerTest.kt`

**Interfaces:**
- `class BitrateController(start: Int = 2_500_000, min: Int = 800_000, max: Int = 4_000_000)` with `fun onQueue(queueBytes: Long, nowMs: Long): Int?` (new bitrate or null) and `fun shouldSkipDeltas(queueBytes: Long): Boolean`; `fun capAt(maxBps: Int)` for thermal.
- `class CameraH264Streamer(context, onPacket: (kind: Int, ptsUs: Long, data: ByteArray) -> Unit, onState: (JSONObject) -> Unit, onError: (String) -> Unit, queueBytes: () -> Long)` with `start(lens)`, `switchLens(lens)`, `requestKeyFrame()`, `stop()`. `onState` reports `{width, height, rotation, mirror, fps, bitrate}`.

- [ ] Step 1: JVM unit tests for `BitrateController` (down-step at > 512 KB, up-step only after 5 s calm, clamps, skip-deltas above 2 MB, thermal cap).
- [ ] Step 2: run `./gradlew testFullDebugUnitTest` → FAIL; implement; → PASS.
- [ ] Step 3: implement the streamer (encoder configure → `createInputSurface()` → camera session on that surface → drain thread; codec-config flag → kind 1, key flag → kind 2, else 3; rotation/mirror from `SENSOR_ORIENTATION`/`LENS_FACING`).
- [ ] Step 4: commit — `feat(companion): hardware H.264 camera streamer`

### Task 2: Phone — protocol negotiation and binary send

**Files:**
- Modify: `.../network/CompanionWebSocketClient.kt` (START_CAMERA_STREAM reads `codec`; new `sendCameraPacket`, `CAMERA_KEYFRAME` handler)
- Modify: `.../service/LivePreviewService.kt` (choose `CameraH264Streamer` when `codec == "h264"`, else today's `CameraPreviewStreamer`)
- Modify: `app/build.gradle.kts` (version 1.9.28 / 56)

- [ ] Step 1: implement; build `assembleFullDebug`; install on the phone.
- [ ] Step 2: commit — `feat(companion): negotiate H.264 live camera, binary packets`

### Task 3: Laptop — camera packets into the fan-out + instant-save H.264 recorder

**Files:**
- Modify: `server/app/websocket/hub.py` (device binary frames with header `type == "CAMERA_PKT"` → `fanout.publish(f"{did}#cam", VideoPacket)`; START_CAMERA_STREAM adds `codec` from the requesting dashboard's `wants_h264`; viewer join → `CAMERA_KEYFRAME` to the phone; recorder choice)
- Create: `server/app/services/h264_recorder.py`
- Test: `server/tests/test_h264_recorder.py`, `server/tests/test_hub_camera_h264.py`

**Interfaces:**
- `parse_device_binary(data: bytes) -> tuple[dict, bytes]` (u16 header length + JSON + payload) in hub or a small helper module.
- `class H264Recorder(device_name: str, folder: Path)` with `submit_video(kind, pts_us, data)`, `submit_audio(pcm, rate)`, `close() -> Optional[str]`; writes Annex-B to `ffmpeg -f h264 -i pipe:0 -c copy -movflags +frag_keyframe+empty_moov+default_base_moof` (fragmented MP4, playable while writing); audio PCM to a side file muxed `-c copy` video + AAC on close (audio encode only, video never re-encoded). Starts at the first key frame (with the cached config).

- [ ] Step 1: tests — binary header parsing; CAMERA_PKT published under `#cam` id; START_CAMERA_STREAM forwards `codec:"h264"` only when the dashboard wants H.264; recorder with real ffmpeg on generated H.264 fixtures: 10 s clip closes in < 0.5 s for video-only, file probes as h264 with the same frame count; lens-switch (second config) produces a playable file.
- [ ] Step 2: RED → implement → GREEN; full suite.
- [ ] Step 3: commit — `feat(camera): H.264 camera packets on the laptop, instant-save recorder`

### Task 4: Dashboard — Live Preview with `H264Player`

**Files:**
- Modify: `server/static/js/app.js` (live preview: `START_CAMERA_STREAM` unchanged; binary packets whose id ends with `#cam` → a second `H264Player` on the live-stage canvas; rotation/mirror from `CAMERA_STREAM_STATE` applied with a canvas transform; JPEG `CAMERA_FRAME` still rendered when it arrives)
- Modify: `server/static/js/h264-player.js` (optional `transform` hook: rotation 0/90/180/270 + horizontal mirror)

- [ ] Step 1: implement; manual check in the browser with the phone: rear and front lens, rotate phone, lens switch, start/stop recording.
- [ ] Step 2: commit — `feat(dashboard): hardware-decoded live camera preview`

### Task 5: Acceptance

- [ ] `tools/bench_stream.py <id> --mode camera --seconds 300` with motion in front of the camera: every 10 s window ≥ 25 fps; `msm_therm` rise ≤ 6 °C over 5 min (baseline: 7 fps rear / 5.5 front, +10 °C CPU sensor in 4 min); 3-minute recording saved in < 0.5 s after stop (baseline 0.99 s and growing). Record numbers in the ledger/PR.
