# Phase 5 — Remote Screen View on Android 8–10 (MediaProjection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phones that are not on USB (Wi-Fi / Cloudflare relay) show a live, smooth screen in the dashboard on Android 8+, instead of nothing on Android 8–10 and ~1 fps accessibility screenshots on 11+.

**Architecture:** Implement the `ScreenCaptureService` that `AndroidManifest.xml:160-163` already declares: MediaProjection (user consent once per session, a system requirement) → `VirtualDisplay` → `MediaCodec` H.264 input Surface, 720 p, 24–30 fps, CBR 1.5–3 Mbps with the Phase 3 `BitrateController`, key frame every 2 s, `KEY_REPEAT_PREVIOUS_FRAME_AFTER` 100 ms so a static screen still refreshes late joiners. Packets go over the existing companion socket as `CAMERA_PKT`-style frames with `"type":"SCREEN_PKT"`; the hub publishes them on stream `<device_id>` — the same stream id, fan-out, subscriptions and browser player as the USB mirror, so the dashboard needs no new player. Input over the companion stays on the Accessibility service. Accessibility `takeScreenshot` remains the fallback when the user declines consent.

**Tech Stack:** Kotlin (MediaProjectionManager, VirtualDisplay, MediaCodec), Python hub, existing WebCodecs player.

**Spec:** `docs/superpowers/plans/2026-11-11-upgrade-roadmap.md` ("Phase 5 decisions", baseline row "Remote screen view").

## Global Constraints

- Consent: `MediaProjectionManager.createScreenCaptureIntent()` from a transparent activity; the token is kept for the service's lifetime only (no persistence — Android forbids reuse after the service stops). Foreground service type `mediaProjection` (already declared).
- Encoder: 720 p long side max, 30 fps cap, `KEY_I_FRAME_INTERVAL` 2, `KEY_REPEAT_PREVIOUS_FRAME_AFTER` 100 000 µs, bitrate controller as in Phase 3, thermal step-down as in Phase 3 (`ThermalZones` / PowerManager).
- Rotation: recreate the VirtualDisplay/encoder on configuration change (new config packet; the browser player already handles size changes).
- Secure windows (`FLAG_SECURE`) stay black — MediaProjection enforces it; say so in the notice.
- USB phones keep the scrcpy path (Phase 2) — the hub only asks the companion for screen packets when the phone is not on USB.
- App version → 1.9.29 / 57.

## Review Focus

1. User declines consent → dashboard notice "Allow screen sharing on the phone" and Accessibility screenshots as before.
2. Phone goes USB ↔ Wi-Fi while mirroring → exactly one source at a time; no duplicate stream on `<id>`.
3. Static screen → a dashboard opening late still sees the picture within 0.5 s (repeat-previous-frame + cached group).
4. Relay throughput drop (Cloudflare) → bitrate falls, no OkHttp 16 MB socket death.
5. Rotation mid-stream → picture re-fits, taps (Accessibility) land correctly.

---

### Task 1: Phone — `ScreenCaptureService` + consent activity
**Files:** Create `service/ScreenCaptureService.kt`, `ScreenConsentActivity.kt`; modify `AndroidManifest.xml` (activity entry), `CompanionWebSocketClient.kt` (`START_SCREEN_PKT` / `STOP_SCREEN_PKT`, `SCREEN_PKT` frames, consent state messages).
- [ ] JVM tests for the size/bitrate selection helper (long side 720, even dimensions, aspect kept).
- [ ] Implement; build; install; manual consent flow on the phone.
- [ ] Commit — `feat(companion): MediaProjection H.264 screen stream`

### Task 2: Laptop — remote screen packets on the mirror stream
**Files:** `server/app/websocket/hub.py` (`_screen_stream_loop`: when not on USB and the companion advertises `screen_h264`, ask for `START_SCREEN_PKT` and publish `SCREEN_PKT` on `<id>`; fall back to accessibility screenshots on refusal), tests `tests/test_hub_remote_screen.py`.
- [ ] Tests: remote phone + H.264 viewer → START_SCREEN_PKT sent once, packets published on `<id>`; consent refused → SCREEN_UNAVAILABLE notice + snapshot path; phone moves to USB → STOP_SCREEN_PKT and scrcpy takes over.
- [ ] Commit — `feat(mirror): remote H.264 screen through the companion`

### Task 3: Acceptance over Wi-Fi (no USB)
- [ ] With the phone on the same Wi-Fi (firewall allowing port 8000 on the laptop — the user's decision) or via the relay: `bench_stream.py --mode mirror` ≥ 24 fps while scrolling, notice on consent refusal, 5-minute heat rise ≤ 6 °C. Record numbers.
