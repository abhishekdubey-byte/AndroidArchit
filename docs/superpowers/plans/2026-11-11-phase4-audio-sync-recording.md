# Phase 4 — Audio Quality, A/V Sync and Live Phone Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 48 kHz AAC audio with the live camera and in recordings, audio and video locked to the same phone clock (no drift, no 1.5 s stalls), real "Listen Live" (phone microphone in the browser), and laptop-mic capture on an AudioWorklet instead of the deprecated ScriptProcessor.

**Architecture:** The phone encodes the microphone with `MediaCodec` AAC-LC 48 kHz mono 96–128 kbps and sends binary `CAMERA_PKT` frames with `k = 4` (AudioSpecificConfig) / `5` (AAC frame), `pts` from the same `System.nanoTime()/1000` origin as the video encoder's `presentationTimeUs` (MediaCodec surface input already uses that clock). The laptop publishes them on the same `<id>#cam` stream (kinds 4/5 already exist in `media_packets`), `MediaFanout` keeps per-track state (audio never cached in the video GOP, never dropped by a video resync), and `H264Recorder` becomes an `AvRecorder` that writes **MPEG-TS** with real PTS (written in Python, no new dependency) and remuxes to MP4 with `-c copy` on close — instant, correct duration at any frame rate. The browser decodes audio with WebCodecs `AudioDecoder`, plays through an `AudioWorklet` ring buffer (80 ms target) and schedules video frames against the audio clock. "Listen Live" starts audio-only streaming (`START_PHONE_AUDIO`) through the same path.

**Tech Stack:** Kotlin (AudioRecord, MediaCodec AAC), Python (TS muxer), WebCodecs AudioDecoder, AudioWorklet.

**Spec:** `docs/superpowers/plans/2026-11-11-upgrade-roadmap.md` ("Phase 4 decisions"); Phase 3 reviewer notes in `.superpowers/sdd/2026-11-11-phase2-usb-h264-mirroring/progress.md` (I11: raw H.264 has no PTS; audio must not enter the video GOP).

## Global Constraints

- Audio: 48 kHz, mono, AAC-LC, 96 kbps (64 kbps when the bitrate controller is below 1 Mbps); 1024-sample frames (21.3 ms).
- One clock: every packet PTS in µs from `System.nanoTime()/1000` on the phone; the laptop never re-times packets.
- Recorder output: MPEG-TS (PAT/PMT, H.264 PID 0x100, ADTS AAC PID 0x101, PTS/DTS 90 kHz, PCR on video) → `ffmpeg -c copy` to MP4 + `-display_rotation` on close; stop = flush TS + one copy pass (< 0.5 s for 3 min).
- Browser jitter buffer: 80 ms target, 200 ms max; video frame shown when `frame.timestamp <= audioClock + 20 ms`, dropped if > 100 ms late.
- Laptop mic: `getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}})` → AudioWorklet → 16 kHz PCM to the phone (call bridge unchanged on the phone side).
- Old apps (JPEG + 16 kHz PCM `CAMERA_AUDIO`) keep working.
- Platform limit, stated in the UI: stock Android does not let apps capture the caller's voice; the call bridge stays speaker↔microphone.

## Review Focus

1. Lens switch / thermal fps change mid-recording → recording duration equals wall time ±0.2 s; audio stays in sync (test with synthetic PTS).
2. Network stall of 2 s on the relay → no audio burst/garble; playback resumes within 300 ms of data arriving.
3. A viewer joining late gets audio config before audio frames (separate audio track cache).
4. Background tab → audio stops with the mirror pause; no AudioContext left running.
5. Phone without mic permission → video-only stream, recording still valid.

---

### Task 1: Phone — AAC encoder and shared clock
**Files:** Create `service/AacMicEncoder.kt`; modify `CameraH264Streamer.kt` (start/stop audio with video, same clock), `CompanionWebSocketClient.kt` (`sendCameraPacket` kinds 4/5), `LivePreviewService.kt`; JVM test for the ADTS/ASC helper (`AudioConfigTest`).
- [ ] Test: AudioSpecificConfig for 48 kHz mono AAC-LC = `0x11 0x88`; ADTS header builder for a 200-byte frame matches a known vector.
- [ ] Implement (AudioRecord VOICE_RECOGNITION/CAMCORDER source, 48 kHz, 20 ms reads → MediaCodec "audio/mp4a-latm"); PTS from `System.nanoTime()/1000` at read time.
- [ ] Build, install, commit — `feat(companion): 48 kHz AAC live audio on the video clock`

### Task 2: Laptop — audio track in the fan-out
**Files:** `server/app/websocket/media_fanout.py`, `server/app/websocket/hub.py`; test `tests/test_fanout_audio.py`.
- [ ] Tests: kind 4 cached per stream and replayed on subscribe before any kind 5; kind 5 never appended to the video GOP; a video overflow resync does not clear queued audio; JPEG viewers get none.
- [ ] Implement per-track caches (`_audio_config`), separate audio queue lane per viewer (small, drop-oldest), video lane unchanged.
- [ ] Commit — `feat(media): audio track with its own cache and lane`

### Task 3: Laptop — `AvRecorder` (MPEG-TS with real PTS)
**Files:** Create `server/app/services/ts_mux.py`, `server/app/services/av_recorder.py` (replaces H264Recorder usage in hub); tests `tests/test_ts_mux.py`, `tests/test_av_recorder.py`.
- [ ] Tests: TS packets are 188 bytes, continuity counters wrap per PID, PES PTS encodes 90 kHz from µs; a 10 s synthetic stream at 30 fps then 15 fps (thermal) produces an MP4 whose duration is 10.0 s ±0.1 (ffprobe) — the case raw `-f h264` gets wrong; audio+video MP4 has both streams with start times within 40 ms; close() < 0.5 s.
- [ ] Implement; hub uses AvRecorder for H.264 streams.
- [ ] Commit — `feat(recording): timestamp-exact A/V recordings via MPEG-TS remux`

### Task 4: Browser — AudioDecoder + AudioWorklet + A/V sync
**Files:** Create `server/static/js/audio-player.js` (+ `audio-worklet.js`); modify `h264-player.js` (optional `clock` to schedule frames), `app.js` (live preview audio, Listen Live, mic via AudioWorklet).
- [ ] Manual checks (no JS harness): clap test (hand clap visible and audible within 1 frame); 2 s stall test; background tab; Listen Live on/off.
- [ ] Commit — `feat(dashboard): synced live audio, real Listen Live, AudioWorklet mic`

### Task 5: Acceptance
- [ ] 5-min camera run with talking + motion: no audible gaps, A/V offset ≤ 40 ms at start and end (clap test), recording duration = wall time ±0.2 s, saved < 0.5 s after stop. Record in the ledger.
