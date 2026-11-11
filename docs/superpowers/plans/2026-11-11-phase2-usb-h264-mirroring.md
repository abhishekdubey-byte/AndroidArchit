# Phase 2 — USB Hardware-H.264 Mirroring & Instant Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-frame `screencap -p` mirror (0.29 fps measured) with a continuous hardware-H.264 stream decoded in the browser (target ≥ 30 fps, 60 where possible), and route USB input through the same session (target < 50 ms per action instead of 0.75–3 s).

**Architecture:** The laptop pushes the open-source scrcpy server (Apache-2.0, pinned v2.7) to the phone over ADB; it encodes the screen with the phone's MediaCodec H.264 encoder and serves a video socket and a control socket through `adb forward`. `ScrcpySession` (Python, asyncio) reads packets, the hub fans them out to dashboards as binary WebSocket messages in the shared packet format (roadmap §"Shared decisions"), with per-client drop-to-key-frame backpressure. The browser decodes with WebCodecs into the existing canvas. Input events go to the control socket. The old JPEG loop stays as the fallback.

**Tech Stack:** Python asyncio (`asyncio.open_connection`, `create_subprocess_exec`), scrcpy-server v2.7, WebCodecs `VideoDecoder`, stdlib `unittest`.

**Spec:** `docs/superpowers/plans/2026-11-11-upgrade-roadmap.md` (baseline rows "USB screen mirror", "ADB input"; "Shared decisions for Phases 2–5").

## Global Constraints

- scrcpy server **v2.7** exactly: file `scrcpy-server-v2.7` from `https://github.com/Genymobile/scrcpy/releases/download/v2.7/scrcpy-server-v2.7`, stored at `server/storage/vendor/scrcpy-server-v2.7`, SHA-256 checked against the value published on that release page (copy it into `SCRCPY_SERVER_SHA256`). Pushed to `/data/local/tmp/scrcpy-server.jar`. The version string passed on the command line must be `2.7`.
- Before writing byte layouts, confirm them against upstream `doc/develop.md` and `app/src/control_msg.c` **at tag v2.7**; the unit tests below pin the layouts this plan expects.
- Defaults: `max_size=1280`, `video_bit_rate=8000000`, `max_fps=60`, `video_codec=h264`, `video_codec_options=i-frame-interval:int=2`, `audio=false`, `power_off_on_close=false`, `clipboard_autosync=false`, `cleanup=true`. Env overrides: `ACC_MIRROR_MAX_SIZE`, `ACC_MIRROR_BITRATE`, `ACC_MIRROR_FPS`.
- Phase 1 must be merged first (ADB path, UTF-8 console, Unicode routing).
- Nothing is written on the phone except `/data/local/tmp/scrcpy-server.jar` (scrcpy deletes it on exit with `cleanup=true`).
- WebCodecs only exists in secure contexts: `http://localhost` / `127.0.0.1` qualify; a dashboard opened from another machine over plain `http://` does not — it must get the JPEG fallback.
- No new Python dependencies. Tests: `cd server && .venv/Scripts/python -m unittest discover -s tests -t .`
- One commit per task on `vivek/feature-updates`.

## Review Focus

1. Dashboard opened from another computer (`http://192.168.x.x:8000`, no WebCodecs) while the laptop dashboard watches in H.264 → remote viewer still sees the slow JPEG mirror with a notice, laptop viewer unaffected (test in Task 4).
2. Phone rotates to landscape mid-stream → new config packet; player reconfigures; taps land where clicked because the hub maps to the *current* frame size (test in Task 2 via `sps_dimensions`, manual in Task 6).
3. USB unplugged mid-stream → session closes within 2 s, `adb forward` removed, server process killed, dashboards told "Phone is offline", loop retries; no zombie process after replug (test in Task 3).
4. Two dashboards, one on a slow link → fast one keeps every frame; slow one skips to the next key frame instead of growing a queue (test in Task 4).
5. Text with characters scrcpy cannot inject (Devanagari, emoji) → routed to the companion (Phase 1 rule), never silently lost (test in Task 5).

---

### Task 1: Shared media packet format

**Files:**
- Create: `server/app/services/media_packets.py`
- Test: `server/tests/test_media_packets.py`

**Interfaces:**
- Produces: constants `KIND_CONFIG = 1`, `KIND_KEY = 2`, `KIND_DELTA = 3`, `KIND_AUDIO_CONFIG = 4`, `KIND_AUDIO = 5`.
- Produces: `pack(kind: int, device_id: str, pts_us: int, payload: bytes) -> bytes` → `[u8 kind][u8 idLen][id utf-8][u64 BE pts][payload]`; raises `ValueError` if the UTF-8 id is longer than 255 bytes.
- Produces: `unpack(data: bytes) -> tuple[int, str, int, bytes]`.

- [ ] **Step 1: Failing tests** — round-trip for each kind; `pack(KIND_KEY, "dev_é", 2**40, b"\x00\x00\x00\x01e")[:2] == bytes([2, 6])`; a 300-byte id raises `ValueError`.
- [ ] **Step 2: Run** `-m unittest tests.test_media_packets -v` → FAIL.
- [ ] **Step 3: Implement** with `struct.pack(">BB", …)` + `">Q"`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(media): shared binary packet format for dashboards`

### Task 2: scrcpy v2.7 protocol helpers (pure functions)

**Files:**
- Create: `server/app/services/scrcpy_protocol.py`
- Test: `server/tests/test_scrcpy_protocol.py`

**Interfaces:**
- Produces: `SCRCPY_VERSION = "2.7"`, `DEVICE_JAR = "/data/local/tmp/scrcpy-server.jar"`.
- Produces: `new_scid() -> str` (8 lowercase hex chars, value < 0x80000000); `socket_name(scid: str) -> str` → `"scrcpy_" + scid`.
- Produces: `server_command(scid: str, *, video: bool, max_size: int, bit_rate: int, max_fps: int) -> str` — the shell string `CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / com.genymobile.scrcpy.Server 2.7 scid=<scid> log_level=info tunnel_forward=true audio=false control=true video=<true|false> video_codec=h264 max_size=… video_bit_rate=… max_fps=… video_codec_options=i-frame-interval:int=2 send_device_meta=false send_dummy_byte=true send_codec_meta=true send_frame_meta=true cleanup=true power_off_on_close=false clipboard_autosync=false`.
- Produces: `parse_codec_meta(b: bytes) -> tuple[str, int, int]` (12 bytes: u32 codec id `0x68323634`="h264", u32 width, u32 height; big-endian).
- Produces: `parse_frame_header(b: bytes) -> tuple[int, bool, bool, int]` → `(pts_us, is_config, is_key, size)` from 12 bytes: u64 BE (bit 63 config, bit 62 key, low 62 bits PTS µs) + u32 BE size.
- Produces: `sps_dimensions(annexb_config: bytes) -> tuple[int, int]` — find the SPS NAL (type 7), strip emulation-prevention bytes, exp-Golomb parse to `pic_width/height` minus frame cropping; must handle profile_idc ≥ 100 (chroma_format etc.).
- Produces control messages (big-endian):
  - `key_event(action: int, keycode: int, repeat: int = 0, meta: int = 0) -> bytes` → type 0; 14 bytes.
  - `text_events(text: str) -> list[bytes]` → type 1 + u32 length + UTF-8, each chunk ≤ 300 bytes, never splitting a code point.
  - `touch_event(action: int, x: int, y: int, frame_w: int, frame_h: int, pressure: float = 1.0) -> bytes` → type 2, action u8, pointer id u64 `0xFFFFFFFFFFFFFFFE` (generic finger), x i32, y i32, w u16, h u16, pressure u16 (`0xFFFF` = 1.0), action_button i32 = 0, buttons i32 = 0; 32 bytes.
  - Constants `ACTION_DOWN = 0`, `ACTION_UP = 1`, `ACTION_MOVE = 2`; `KEYCODES = {"HOME": 3, "BACK": 4, "RECENTS": 187, "POWER": 26, "ENTER": 66, "DEL": 67, "VOLUME_UP": 24, "VOLUME_DOWN": 25}`.
- Produces: `scrcpy_can_type(text: str) -> bool` — True iff every char is ASCII or Latin-1 letter (scrcpy injects via `KeyCharacterMap` and composes accents; Devanagari/emoji fail).

- [ ] **Step 1: Failing tests** — exact bytes:

```python
def test_key_event_layout(self):
    self.assertEqual(key_event(ACTION_DOWN, 3), bytes([0, 0]) + (3).to_bytes(4, "big") + bytes(8))

def test_touch_event_layout(self):
    b = touch_event(ACTION_DOWN, 100, 200, 720, 1440)
    self.assertEqual(len(b), 32); self.assertEqual(b[:2], bytes([2, 0]))
    self.assertEqual(b[2:10], b"\xff" * 7 + b"\xfe"); self.assertEqual(b[10:18], (100).to_bytes(4, "big") + (200).to_bytes(4, "big"))
    self.assertEqual(b[18:24], (720).to_bytes(2, "big") + (1440).to_bytes(2, "big") + b"\xff\xff")

def test_text_chunks_never_split_codepoints(self):
    chunks = text_events("é" * 200)            # 400 UTF-8 bytes
    self.assertEqual([len(c) - 5 for c in chunks], [300, 100])

def test_frame_header_flags(self):
    h = ((1 << 62) | 123456).to_bytes(8, "big") + (999).to_bytes(4, "big")
    self.assertEqual(parse_frame_header(h), (123456, False, True, 999))

def test_codec_meta(self):
    self.assertEqual(parse_codec_meta(b"h264" + (720).to_bytes(4, "big") + (1440).to_bytes(4, "big")), ("h264", 720, 1440))

def test_sps_dimensions_real_stream(self):
    # fixture: first config packet captured from a real scrcpy session (Task 6 Step 1 saves it)
    self.assertEqual(sps_dimensions(FIXTURE_CONFIG_720x1440), (720, 1440))

def test_server_command(self):
    cmd = server_command("0a1b2c3d", video=True, max_size=1280, bit_rate=8_000_000, max_fps=60)
    self.assertIn(" com.genymobile.scrcpy.Server 2.7 scid=0a1b2c3d ", cmd); self.assertIn(" video=true ", cmd)
```

  Until Task 6 records a real fixture, generate one locally with `ffmpeg -f lavfi -i testsrc=size=720x1440:rate=30 -frames:v 1 -c:v libx264 -bsf:v h264_mp4toannexb -f h264 tests/fixtures/sps_720x1440.h264` (ffmpeg is installed) and add a second fixture with cropping (`size=1080x2160` → 1088-high coded size, cropped to 2160).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** For `sps_dimensions`, a small bit reader (`read_bits`, `read_ue`, `read_se`); skip `scaling_list` when `seq_scaling_matrix_present_flag`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(mirror): scrcpy v2.7 protocol helpers with pinned byte layouts`

### Task 3: `ScrcpySession` — process, sockets, packets

**Files:**
- Create: `server/app/services/scrcpy_session.py`
- Test: `server/tests/test_scrcpy_session.py`

**Interfaces:**
- Consumes: Task 2 helpers; `ADBBridge.run_command`/`adb_path` from `adb_bridge.py`.
- Produces: `ensure_server_jar(vendor_dir: Path = config.STORAGE_DIR / "vendor") -> Path` — downloads once (urllib, 30 s timeout), verifies `SCRCPY_SERVER_SHA256`, raises `MirrorUnavailable("…")` on failure.
- Produces: `class MirrorUnavailable(Exception)`.
- Produces: `@dataclass VideoPacket: pts_us: int; is_config: bool; is_key: bool; data: bytes`.
- Produces: `class ScrcpySession`:
  - `__init__(self, adb_path: str, serial: str, *, video: bool = True, max_size=1280, bit_rate=8_000_000, max_fps=60)`
  - `async start(self) -> None` — push jar (`adb push` only when the size on the phone differs), pick a free local port, `adb forward tcp:<port> localabstract:scrcpy_<scid>`, spawn `adb -s <serial> shell <server_command>` with `create_subprocess_exec`, connect video socket (retry every 100 ms up to 3 s; read and discard the 1 dummy byte), then the control socket; when `video`, read codec meta into `self.width/self.height`.
  - `async packets(self) -> AsyncIterator[VideoPacket]` — header + exact-size body reads; ends cleanly on EOF.
  - `async send(self, msg: bytes) -> None` — writes to the control socket (serialised by an `asyncio.Lock`).
  - `frame_size: tuple[int, int]` — updated from `sps_dimensions` on each config packet.
  - `async stop(self) -> None` — idempotent: close sockets, terminate then kill the process after 2 s, `adb forward --remove tcp:<port>`.
  - `async __aenter__/__aexit__` calling `start`/`stop`.

- [ ] **Step 1: Failing tests** — use a fake "device": an `asyncio.start_server` on 127.0.0.1 that sends the dummy byte, codec meta (720×1440), one config packet (Task 2 fixture) and two frames, then closes. Subclass `ScrcpySession` overriding `_adb(*args)` to no-op and `_spawn_server()` to no-op and `_port()` to the fake server's port. Assert: `width/height == (720, 1440)`; packets yield `[config, key, delta]` with the right flags and byte lengths; after the fake closes, `packets()` ends within 1 s; `stop()` called twice does not raise and records exactly one `forward --remove`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(mirror): ScrcpySession over adb forward`

### Task 4: Hub — H.264 fan-out with backpressure and JPEG fallback

**Files:**
- Modify: `server/app/websocket/hub.py:170-178` (START_SCREEN_STREAM), `:241-244` (STOP), `:682-735` (`_screen_stream_loop`), `:737-750` (`broadcast_to_dashboards`); new message `CLIENT_CAPS` in `_process_dashboard_message`.
- Create: `server/app/websocket/media_fanout.py`
- Test: `server/tests/test_media_fanout.py`

**Interfaces:**
- Consumes: `media_packets.pack`, `ScrcpySession`, `MirrorUnavailable`.
- Produces: `class MediaFanout`:
  - `add(ws, wants_h264: bool)`, `remove(ws)`
  - `async publish(device_id: str, pkt: VideoPacket) -> None` — config packets are cached per device and always delivered; the last key frame is cached and sent first to a client that joins; each client has an `asyncio.Queue(maxsize=60)` drained by its own sender task; when a client's queue is full it is marked `waiting_key` and gets nothing but config packets until the next key frame.
  - `h264_viewers(device_id) -> int`, `jpeg_viewers(device_id) -> int`.
- Dashboards send `{"type": "CLIENT_CAPS", "payload": {"h264": true}}` after connecting (Task 6); clients that never send it count as JPEG viewers.
- `_screen_stream_loop` on USB: start `ScrcpySession(video=True)` → for each packet `await fanout.publish(...)`; run the existing JPEG loop **only while** `jpeg_viewers(device_id) > 0`, at most 1 fps. On `MirrorUnavailable`, tell dashboards once (`SCREEN_UNAVAILABLE`, reason "Fast mirroring unavailable: <reason> — using slow snapshots") and run today's JPEG loop. On session end, retry every 2 s while mirroring is on. Remote (companion) devices keep today's path.
- Input frame size for Task 5 is read from the live session (`session.frame_size`).

- [ ] **Step 1: Failing tests** with fake websockets (`send_bytes` awaits an event the test controls):
  - fast + slow client: publish config, key, 100 deltas, key, 5 deltas; fast receives all 108; slow receives config, key, then nothing until the second key, then the 5 deltas.
  - late joiner receives cached config + last key frame before live deltas.
  - client without `h264` receives no binary messages.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `MediaFanout`, then wire the hub.
- [ ] **Step 4: Run** all tests → PASS.
- [ ] **Step 5: Commit** — `feat(mirror): H.264 fan-out with drop-to-keyframe backpressure`

### Task 5: Fast input through the session

**Files:**
- Modify: `server/app/websocket/hub.py:246-272` (INPUT_TOUCH / INPUT_SWIPE / INPUT_KEY / INPUT_TEXT)
- Modify: `server/app/api/routes.py:87-110` (action/touch, action/key, action/text)
- Create: `server/app/services/input_router.py`
- Test: `server/tests/test_input_router.py`

**Interfaces:**
- Consumes: `ScrcpySession`, Task 2 control builders, Phase 1 `adb_typable` / companion routing in `DeviceControl.inject_text`.
- Produces: `class InputRouter`:
  - `async tap(device_id: str, x: int, y: int) -> str` (returns the path used: `"scrcpy" | "companion" | "adb"`)
  - `async swipe(device_id, x1, y1, x2, y2, duration_ms: int) -> str` — DOWN, MOVE every 16 ms (linear), UP.
  - `async key(device_id, name: str) -> str`
  - `async text(device_id, text: str) -> str` — `scrcpy_can_type(text)` → scrcpy; else Phase 1 rule (companion, or `UnsupportedText`).
  - Coordinates arrive in device pixels (as today) and are scaled to `session.frame_size` (the phone's real size comes from `dev.display`).
  - Uses the mirror session when one is running; otherwise a control-only `ScrcpySession(video=False)` started on first use and closed after 60 s idle; on any session error falls back to today's ADB/companion call for that event.
- Hub handlers and REST routes call `InputRouter` only.

- [ ] **Step 1: Failing tests** with a fake session recording `send()` bytes: tap at (540, 1080) on a 1080×2160 phone with frame 720×1440 → DOWN/UP at (360, 720); swipe 300 ms → first DOWN, ≥ 15 MOVE, last UP; `text("नमस्ते")` with companion connected → `"companion"` and no scrcpy bytes; key `"HOME"` → keycode 3 down+up; session `send` raising → falls back to `"adb"`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** and wire.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(input): sub-50ms USB input via scrcpy control socket`

### Task 6: Browser H.264 player

**Files:**
- Create: `server/static/js/h264-player.js`
- Modify: `server/static/index.html` (script tag before `app.js`)
- Modify: `server/static/js/app.js` — WebSocket setup (`binaryType = "arraybuffer"`, send `CLIENT_CAPS` with `h264: "VideoDecoder" in window && window.isSecureContext`), `onmessage` binary branch, `renderScreenFrame` (`app.js:491-499`) left for the JPEG fallback.

**Interfaces:**
- Produces (global, no bundler): `parseMediaPacket(buf: ArrayBuffer) -> {kind, deviceId, ptsUs, payload: Uint8Array}`; `avcCodecString(spsNal: Uint8Array) -> string` → `"avc1." + hex(profile_idc) + hex(constraint_flags) + hex(level_idc)`; `class H264Player { constructor(canvas); push(pkt); close(); get stats() -> {fps, decodeQueue, dropped} }`.
- Player rules: on `KIND_CONFIG` → (re)`configure({codec: avcCodecString(sps), optimizeForLatency: true})` (Annex-B, no `description`) and keep the config bytes; prepend them to the next key frame `EncodedVideoChunk`; wait for a key frame after (re)configure; `output` callback draws with `ctx.drawImage(frame, 0, 0, canvas.width, canvas.height)` then `frame.close()`; resize the canvas to `frame.displayWidth/Height` when they change (rotation); if `decodeQueueSize > 4`, drop deltas until the next key frame; on decoder `error` → `close()` and wait for the next config.
- `app.js` touch mapping keeps sending device-pixel coordinates (unchanged).

- [ ] **Step 1: Capture fixtures** — run a session for 3 s against a USB phone and save the first config packet to `server/tests/fixtures/scrcpy_config_<w>x<h>.h264`; update Task 2's real-stream test to use it.
- [ ] **Step 2: Implement** the player and the `app.js` wiring.
- [ ] **Step 3: Manual verification on `http://localhost:8000`:**
  1. Mirroring on → picture within 1 s; stats overlay (temporary `?debug=1`) shows ≥ 30 fps while scrolling a list.
  2. Rotate the phone → picture re-fits, taps still hit the right element (Review Focus 2).
  3. Open the dashboard from a second computer via `http://<laptop-ip>:8000` → JPEG fallback with notice; laptop view unaffected (Review Focus 1).
  4. Unplug → "Phone is offline" within 2 s; replug → mirroring resumes; `adb forward --list` shows no stale entries; Task Manager shows no orphaned `adb shell` processes (Review Focus 3).
- [ ] **Step 4: Commit** — `feat(dashboard): WebCodecs H.264 player with JPEG fallback`

### Task 7: Benchmark tool and phase acceptance

**Files:**
- Create: `server/tools/bench_stream.py`

**Interfaces:**
- Produces: CLI `python tools/bench_stream.py <device_id> --seconds 300 [--mode mirror|camera] [--input-probe]` — connects to `/ws/dashboard`, sends `CLIENT_CAPS` h264, starts the stream, counts video packets per 10 s window (binary) or `SCREEN_FRAME`/`CAMERA_FRAME` (JSON), prints fps windows, frame-gap median/p95/max, KB/s, and every 30 s the phone's `dumpsys battery` temperature and thermal zones `msm_therm`, `tsens_tz_sensor1`, `tsens_tz_sensor9` (skip names that do not exist); `--input-probe` times 20 `POST action/key {"key":"VOLUME_UP"}`/`VOLUME_DOWN` pairs.

- [ ] **Step 1: Implement** (port of the 2026-11-11 test harness; read-only on the phone).
- [ ] **Step 2: Acceptance run on the Redmi Note 5 Pro (or any USB phone), 5 minutes, scrolling continuously:** record fps windows, p95 gap, KB/s, temperatures, input median/p95. Pass when: every 10 s window ≥ 30 fps, input p95 < 50 ms, phone `msm_therm` rise ≤ 6 °C over 5 min. Paste results into the PR description next to the baseline (0.29 fps; 0.75–3 s input).
- [ ] **Step 3: Commit** — `chore(bench): stream/input benchmark tool`
