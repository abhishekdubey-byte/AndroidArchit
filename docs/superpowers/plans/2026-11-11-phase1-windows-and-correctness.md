# Phase 1 — Windows Readiness & Correctness Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server runs on Windows with no manual setup, the unit suite is green there, and every defect found in the 2026-11-11 USB test either works or fails fast with a clear message instead of a false "success".

**Architecture:** Small, independent fixes in the existing FastAPI server (`server/app/...`) and dashboard (`server/static/js/app.js`). No new services; two new exception types and a few pure helper functions so each fix is unit-testable without a phone.

**Tech Stack:** Python 3.13 (`server/.venv`), FastAPI/asyncio, stdlib `unittest`, vanilla JS.

**Spec:** `docs/superpowers/plans/2026-11-11-upgrade-roadmap.md` (baseline table + "Other confirmed defects").

## Global Constraints

- No new Python dependencies (no pytest, no httpx). Tests use `unittest`.
- Test command (Windows): `cd server && .venv/Scripts/python -m unittest discover -s tests -t .` — Linux: `.venv/bin/python` instead.
- Must keep working on Linux (the original host) — no Windows-only calls without a fallback.
- Never send a delete/write to the phone in tests; every phone interaction is faked by overriding `run_device_shell`.
- Work on branch `vivek/feature-updates`; one commit per task.
- User-facing error text: plain English, says what to do next.

## Review Focus

1. Text that mixes ASCII, Unicode and `\n`, sent while USB **and** the companion are connected → the whole string goes through the companion (newlines become Enter), nothing is half-typed over ADB (test in Task 3).
2. `ADB_PATH` set to a path that does not exist → the explicit setting still wins (a person who set it expects it used); startup prints one warning (test in Task 1).
3. MIUI blocks the launch so another app stays in front → the AI step fails naming the foreground app and the MIUI setting, never "verified successfully" (test in Task 4).
4. Recording frames that arrive after `close()` started → ignored, `close()` still returns the MP4 path (test in Task 6).
5. A reconnect event for a phone that is not the selected one → the selected phone's header is untouched (manual check in Task 7).

---

### Task 1: Windows runtime basics (UTF-8 console, ADB auto-detect)

**Files:**
- Modify: `server/app/logsetup.py` (add helper)
- Modify: `server/run_server.py` (call helper first thing)
- Modify: `server/tests/__init__.py` (call helper so `print("→")` in services cannot crash tests)
- Modify: `server/app/config.py:17`
- Test: `server/tests/test_windows_runtime.py`

**Interfaces:**
- Produces: `logsetup.force_utf8_stdio(streams: Optional[list] = None) -> None` — calls `stream.reconfigure(encoding="utf-8", errors="replace")` on each stream that has `reconfigure`; default `[sys.stdout, sys.stderr]`.
- Produces: `config.resolve_adb_path(env: Mapping[str, str] = os.environ, which: Callable[[str], Optional[str]] = shutil.which, exists: Callable[[str], bool] = os.path.isfile) -> str` — order: `env["ADB_PATH"]` (returned even if missing, with a one-line warning) → `which("adb")` → `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe` → `~/Android/Sdk/platform-tools/adb` → `/usr/bin/adb`. `ADB_PATH = resolve_adb_path()`.

- [ ] **Step 1: Write the failing tests**

```python
class TestWindowsRuntime(unittest.TestCase):
    def test_utf8_stdio_survives_arrow_on_cp1252(self):
        raw = io.BytesIO(); w = io.TextIOWrapper(raw, encoding="cp1252")
        force_utf8_stdio([w]); print("sync started → folder", file=w); w.flush()
        self.assertIn("→".encode("utf-8"), raw.getvalue())

    def test_adb_env_wins_even_if_missing(self):
        self.assertEqual(resolve_adb_path({"ADB_PATH": r"C:\nope\adb.exe"}, lambda n: None, lambda p: False), r"C:\nope\adb.exe")

    def test_adb_from_path(self):
        self.assertEqual(resolve_adb_path({}, lambda n: r"C:\tools\adb.exe", lambda p: False), r"C:\tools\adb.exe")

    def test_adb_from_windows_sdk(self):
        sdk = r"C:\Users\u\AppData\Local\Android\Sdk\platform-tools\adb.exe"
        self.assertEqual(resolve_adb_path({"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, lambda n: None, lambda p: p == sdk), sdk)

    def test_adb_linux_default(self):
        self.assertEqual(resolve_adb_path({}, lambda n: None, lambda p: False), "/usr/bin/adb")
```

- [ ] **Step 2: Run to verify they fail** — `.venv/Scripts/python -m unittest tests.test_windows_runtime -v` → `ImportError` for `force_utf8_stdio` / `resolve_adb_path`.
- [ ] **Step 3: Implement both helpers** with the signatures above; call `force_utf8_stdio()` in `run_server.py` right after `from app.logsetup import …` (before anything prints) and in `tests/__init__.py`. Build the Windows SDK candidate with `ntpath.join` so the test path matches on any OS.
- [ ] **Step 4: Run the new tests → PASS; run the full suite → the 2 `test_backup` `UnicodeEncodeError`s are gone** (9 sqlite errors remain until Task 2).
- [ ] **Step 5: Manual check** — start the server on Windows *without* `ADB_PATH`/`PYTHONIOENCODING`; `GET /api/devices` lists the USB phone.
- [ ] **Step 6: Commit** — `fix(server): auto-detect adb and force UTF-8 console on Windows`

### Task 2: Close PowerBoosters SQLite so the suite is green on Windows

**Files:**
- Modify: `server/app/services/powerboosters.py:181-190` (class `PowerBoosters`)
- Modify: `server/tests/test_powerboosters.py:17-18`, `server/tests/test_pb_gateway.py` (tearDown)
- Test: `server/tests/test_powerboosters.py`

**Interfaces:**
- Produces: `PowerBoosters.close() -> None` — closes `self.db`; idempotent.

- [ ] **Step 1: Write the failing test** — `test_close_releases_db_file`: create `PowerBoosters(d / "pb.db", d / "files")`, call `close()` twice, then `(d / "pb.db").unlink()` succeeds (on Windows this raises `PermissionError` today).
- [ ] **Step 2: Run** `-m unittest tests.test_powerboosters -v` → FAIL (`AttributeError: close`).
- [ ] **Step 3: Implement `close()`**; in both test files' `tearDown`, call `close()` on the `PowerBoosters` instance before `self.tmp.cleanup()`.
- [ ] **Step 4: Run the full suite → `Ran 67 tests … OK`** (66 existing + this one).
- [ ] **Step 5: Commit** — `fix(pb): close SQLite handle; green test suite on Windows`

### Task 3: Honest text input and UI tree over ADB

**Files:**
- Modify: `server/app/services/adb_bridge.py:260-276` (`inject_text`), `:315-322` (`dump_ui_hierarchy`)
- Modify: `server/app/services/remote_control.py:69` (`DeviceControl.inject_text`)
- Modify: `server/app/api/routes.py:103-106` (text route), `:124-127` (ui route)
- Modify: `server/app/services/ai_agent.py:258` (catch the new UI exception)
- Test: `server/tests/test_adb_input.py`

**Interfaces:**
- Produces in `adb_bridge.py`: `adb_typable(text: str) -> bool` (True iff every char is printable ASCII or `\n`); `class UnsupportedText(Exception)`; `class UiDumpUnavailable(Exception)`.
- `ADBBridge.inject_text` raises `UnsupportedText` when `not adb_typable(text)` (before sending anything).
- `DeviceControl.inject_text`: if `not adb_typable(text)` and the device's companion relay is connected → send through the companion even on USB (same pattern `dump_ui_hierarchy` already uses at `remote_control.py:86-92`).
- `ADBBridge.dump_ui_hierarchy` raises `UiDumpUnavailable("The screen never went idle (animations/video); turn on the companion app's Accessibility to read it")` on timeout or `ERROR:` output instead of returning `[]`.
- Routes: `UnsupportedText` → HTTP 422 `"Non-English text and emoji need the companion app connected"`; `UiDumpUnavailable` → HTTP 503 with its message.
- `ai_agent.py:258`: on `UiDumpUnavailable`, fail the step with that message.

- [ ] **Step 1: Write the failing tests** (fake bridge = `ADBBridge` subclass whose `run_device_shell` records commands / returns scripted output)

```python
def test_adb_typable(self):
    self.assertTrue(adb_typable("battery & saver (test) 50%\nok"))
    self.assertFalse(adb_typable("café")); self.assertFalse(adb_typable("😀"))

def test_unicode_raises_and_sends_nothing(self):
    b = FakeBridge(); 
    with self.assertRaises(UnsupportedText): run(b.inject_text("s", "नमस्ते"))
    self.assertEqual(b.commands, [])

def test_ui_dump_timeout_raises(self):
    b = FakeBridge(output="ERROR: could not get idle state.")
    with self.assertRaises(UiDumpUnavailable): run(b.dump_ui_hierarchy("s"))

def test_mixed_text_goes_to_companion_when_connected(self):
    ctl = FakeDeviceControl(usb=True, companion=True)
    run(ctl.inject_text("s", "hi café\nok"))
    self.assertEqual(ctl.companion_texts, ["hi café\nok"]); self.assertEqual(ctl.adb_commands, [])
```

- [ ] **Step 2: Run** `-m unittest tests.test_adb_input -v` → FAIL (names undefined).
- [ ] **Step 3: Implement** per the Interfaces block.
- [ ] **Step 4: Run** the new tests and the full suite → PASS.
- [ ] **Step 5: Manual check on a phone** — over USB without companion: `POST action/text {"text":"café"}` → 422 in < 1 s; with companion connected → text appears in the Settings search box.
- [ ] **Step 6: Commit** — `fix(input): never report success for untypeable text; explicit UI-dump errors`

### Task 4: App list, launch result and AI operator honesty

**Files:**
- Modify: `server/app/services/adb_bridge.py:278-313` (`launch_package`, `get_installed_apps`)
- Modify: `server/app/services/ai_agent.py:533-553` (`compile_report`, `open_app_by_name`)
- Test: `server/tests/test_apps_and_agent.py`

**Interfaces:**
- Produces in `adb_bridge.py`: `parse_launcher_packages(output: str) -> List[str]` — parses `cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER` (lines `pkg/.Activity`; ignore other lines; de-dupe, keep order).
- `ADBBridge.get_installed_apps`: launchable packages from the command above (fallback to today's `pm list packages -3` when the command prints nothing), `is_system = pkg not in <pm list packages -3>`; add to `friendly_map`: `com.android.settings: Settings`, `com.facebook.katana: Facebook`, `com.facebook.orca: Messenger`, `com.android.vending: Play Store`, `com.miui.securitycenter: Security`.
- `ADBBridge.launch_package` returns `"Events injected: 1" in out`.
- Produces: `ADBBridge.foreground_package(serial: str) -> Optional[str]` — parses `mCurrentFocus=Window{… u0 <pkg>/<activity>}` from `dumpsys window windows`.
- `open_app_by_name`: after launching, poll `foreground_package` every 0.5 s up to 3 s; if it never equals the target, raise `RuntimeError(f"Tried to open {app} but {fg or 'another app'} stayed in front. On MIUI allow 'Display pop-up windows while running in the background' for Operator Companion.")`.
- `compile_report`: warnings joined with "; " when `storage.percent >= 90` (`"Storage {p}% full — recordings may fail"`) or `battery.temperature >= 45` (`"Battery hot ({t}°C)"`); "System telemetry audit normal." only when no warning.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_launcher_packages(self):
    out = "com.android.settings/.MainSettings\ncom.whatsapp/.Main\nsome header\ncom.android.settings/.Other\n"
    self.assertEqual(parse_launcher_packages(out), ["com.android.settings", "com.whatsapp"])

def test_settings_listed_and_named(self):
    apps = run(FakeBridge(launcher=["com.android.settings"], third_party=[]).get_installed_apps("s"))
    self.assertEqual((apps[0].app_name, apps[0].is_system), ("Settings", True))

def test_open_app_fails_when_other_app_stays_in_front(self):
    task = run(run_instruction(make_agent(fg="com.miui.home"), "open settings"))
    self.assertEqual(task.status, "failed"); self.assertIn("com.miui.home", task.result_summary)

def test_report_warns_on_full_storage(self):
    task = run(run_instruction(make_agent(storage_percent=95), "check battery status"))
    self.assertIn("Storage 95% full", task.steps[-1].result)
```

(`make_agent` builds the agent class from `ai_agent.py` with a fake device manager + fake bridge; `run_instruction(agent, text)` = `submit_task(text, device_id)` then await until status is not pending/executing — both helpers live in the test file, not in production code.)

- [ ] **Step 2: Run** `-m unittest tests.test_apps_and_agent -v` → FAIL.
- [ ] **Step 3: Implement** per the Interfaces block.
- [ ] **Step 4: Run** new tests + full suite → PASS.
- [ ] **Step 5: Manual check** — over USB, AI "open settings" opens Settings and reports done; app list shows Settings with `is_system: true`.
- [ ] **Step 6: Commit** — `fix(agent): list system launchers, verify the app is in front, warn on full storage`

### Task 5: Fast camera failure and neutral simulated caller

**Files:**
- Modify: `server/app/services/adb_bridge.py` (new method next to `capture_headless_photo_adb`, `:537`)
- Modify: `server/app/api/routes.py:263-305` (`simulate_incoming_call`, camera photo/video routes)
- Test: `server/tests/test_camera_routes.py`

**Interfaces:**
- Produces: `ADBBridge.companion_installed(serial: str) -> bool` (`pm list packages com.operator.companion` contains `package:com.operator.companion`).
- Camera routes: when the hub path did not succeed **and** `companion_installed` is False → HTTP 409 `"Install the Operator Companion app on this phone to use the camera"` immediately (today: 43 s / 63 s timeouts).
- `simulate_incoming_call` defaults: `phone_number="+00 00000 00000"`, `caller_name="Test Caller"`.

- [ ] **Step 1: Write the failing test** — `companion_installed` returns False for output `""` and True for `"package:com.operator.companion\n"`; a second test asserts `inspect.signature(simulate_incoming_call)` defaults are the neutral values (build the router with fakes as `tests/test_relay.py` does for its objects).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests → PASS. **Manual:** with the companion uninstalled, `curl -X POST …/camera/photo` answers 409 in < 2 s.
- [ ] **Step 5: Commit** — `fix(camera): fail fast without the companion; neutral demo caller`

### Task 6: Non-blocking live recording and correct file name

**Files:**
- Modify: `server/app/services/cam_record.py:26-159`
- Modify: `server/app/websocket/hub.py:226-236` (frame/audio intake), `_finalize_recording` (name)
- Test: `server/tests/test_cam_record.py`

**Interfaces:**
- Produces: `CamRecorder.submit(jpeg: bytes) -> None` and `CamRecorder.submit_audio(pcm: bytes, rate: int) -> None` — enqueue to one worker thread (`queue.Queue(maxsize=240)`; when full, drop the oldest *video* item, never audio); return in < 1 ms. Items submitted after `close()` began are ignored.
- `CamRecorder.close()` drains the queue, then runs today's finalize logic, returns the path.
- Produces: `cam_record.display_name(path: Optional[str]) -> Optional[str]` = `Path(path).name` or None.
- `hub.py`: replace `rec.write(...)`/`rec.write_audio(...)` with `submit`/`submit_audio`; `_finalize_recording` sends `"name": display_name(path)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_submit_never_blocks_and_keeps_order(self):
    rec = CamRecorder.__new__(CamRecorder); rec._start_worker()   # skips ffmpeg start-up; tests the queue only
    seen = []; rec.write = lambda b: (time.sleep(0.05), seen.append(b))
    t = time.perf_counter(); [rec.submit(bytes([i])) for i in range(20)]
    self.assertLess(time.perf_counter() - t, 0.1)
    rec.drain(); self.assertEqual(seen, [bytes([i]) for i in range(20)])

def test_display_name_windows_path(self):
    self.assertEqual(display_name(r"D:\a\b\live_x.mp4"), "live_x.mp4"); self.assertIsNone(display_name(None))
```

(Expose the queue start-up as `_start_worker()` called from `__init__`, and `drain()` used by `close()`; the test may call them directly.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests + full suite → PASS. **Manual (companion connected):** a 60 s live recording while scrolling the file manager in another tab — no >500 ms frame gaps on the dashboard; saved file's `name` is the bare file name.
- [ ] **Step 5: Commit** — `fix(recording): move frame writes off the event loop; bare file name`

### Task 7: Dashboard fixes (apps crash, stale OFFLINE, Listen Live honesty)

**Files:**
- Modify: `server/static/js/app.js:435-461` (`selectDevice`), `:467-480` (`updateDevice`), `:1704` (`loadDeviceApps`), `:998-1016` (Listen Live button)

**Interfaces:**
- Produces: `ControlCentreApp.renderActiveDeviceHeader(dev)` — the title/link/screen-notice block moved out of `selectDevice` (`app.js:450-461`); called by `selectDevice` and by `updateDevice` whenever the **active** device's `status` or `transport` changed.
- `loadDeviceApps`: if the response is not OK or not an array, show "Apps unavailable — phone offline" and return (no `.map` on an error object).
- Listen Live: until Phase 4, the button is disabled with title "Coming soon: live phone audio" and no "Listening…" text.

- [ ] **Step 1: Implement** the three changes.
- [ ] **Step 2: Manual verification in the browser** (no JS test harness exists):
  1. Open the dashboard, unplug the phone → header shows `(OFFLINE)`; plug back in → within 5 s it shows `(USB)` and mirroring can be turned on, without a page reload.
  2. With a second phone selected, replug the first → selected header unchanged (Review Focus 5).
  3. Phone offline → console has no `map is not a function` error.
  4. Listen Live is visibly disabled.
- [ ] **Step 3: Commit** — `fix(dashboard): live header on reconnect, apps error guard, honest Listen Live`

### Task 8: Phase acceptance run

- [ ] **Step 1:** Full suite on Windows → `OK`. 
- [ ] **Step 2:** On a USB phone, repeat the 2026-11-11 checks for every row of the roadmap's "Other confirmed defects" and note pass/fail in the PR description.
