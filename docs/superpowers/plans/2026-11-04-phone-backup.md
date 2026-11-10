# Phone Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Manual per-phone "Sync now" that copies selected phone folders/files one-way into a dedicated `~/PhoneBackups/<phone>/` folder, over USB or the relay, keeping files deleted on the phone.

**Architecture:** A transport-agnostic engine (`server/app/services/backup.py`) drives either backend that already exists — `StorageService` (ADB, key = serial) or `RelayStorage` (companion, key = relay_id), which share `list_dir/stat/read_range`. Scanning uses the new phone `scan` op when available (B) and falls back to laptop-side recursive `list_dir` (A). A `BackupService` owns one job per phone and exposes REST + WebSocket progress; a new `backup.html` page drives it.

**Tech Stack:** FastAPI/asyncio (Python 3, server/.venv), stdlib `unittest` (`IsolatedAsyncioTestCase`), Kotlin companion app, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-04-phone-backup-design.md`

## Global Constraints

- One-way phone → laptop; the engine never calls any write/delete op on the phone.
- Backup root: `~/PhoneBackups` (env `ACC_BACKUP_ROOT` overrides — tests only).
- phone_key = `dev.relay_id` if set, else `adb:<serial>`; folder `"<name> · <8 chars>"`, fixed forever via `~/PhoneBackups/.phones.json`.
- Skip `.trashed-*`, `.pending-*` files and `.thumbnails` dirs.
- Failed root scan ⇒ its manifest entries untouched (never marked deleted).
- Manifest saved atomically every 50 files and at the end.
- `scan` op: gzip JSON `{"entries": [[rel, size, mtimeSec]], "next": int|null}`, 5,000 entries/page, 2-min cache.
- No new dependencies (no pytest). Tests: `cd server && .venv/bin/python -m unittest tests.test_backup -v`.
- App version → 1.8.7 / versionCode 25; publish with `server/publish_apk.sh`.
- Project is not a git repo: no commit steps.

## Review Focus

1. Phone loses "All files access" (list returns permission error) → that root is skipped, nothing moved to `Deleted on phone/` (test in Task 2).
2. Phone disconnects mid-file → `.part` kept, next run resumes from its size and the final file is byte-identical (test in Task 2).
3. Hostile/odd phone paths (`..`, absolute rel from `scan`, names with newlines) → refused, never written outside the phone folder (test in Task 1 and Task 3).
4. Two phones with the same model name → different folders, never shared (test in Task 1).
5. File re-appears on the phone after being marked deleted → copied again to the main tree; old copy stays in `Deleted on phone/` (test in Task 2).

---

### Task 1: Phone store — identity, layout, manifest, path safety

**Files:**
- Create: `server/app/services/backup.py`
- Create: `server/tests/__init__.py`, `server/tests/test_backup.py`

**Interfaces:**
- Produces:
  - `BackupError(Exception)`
  - `backup_root() -> Path` (env `ACC_BACKUP_ROOT` else `Path.home()/"PhoneBackups"`)
  - `PRESETS: Dict[str, List[str]]` keys `camera, screenshots, whatsapp, documents, download` with the spec §4 paths
  - `phone_key(dev) -> str`
  - `skip(name: str, is_dir: bool) -> bool`
  - `rel_path(phone_path: str) -> str` — `/sdcard/X` and `/storage/emulated/0/X` → `X`; `/storage/<vol>/X` → `_volumes/<vol>/X`; anything else → `_root/<path>`
  - `class PhoneStore(root: Path, key: str, display_name: str)` with `.dir: Path`, `load_selection() -> dict` (default `{"presets": [], "custom": []}`), `save_selection(sel: dict)`, `load_manifest() -> Dict[str, dict]` (`{phone_path: {"size","mtime","state"}}`), `save_manifest(m)` (tmp + `os.replace`), `dest_for(phone_path) -> Path`, `deleted_dest(phone_path) -> Path` (adds ` (2)`, ` (3)`… before the suffix when taken), `save_last_run(d)`, `load_last_run() -> Optional[dict]`
  - `selected_paths(sel: dict) -> List[str]` — presets expanded + custom, de-duplicated, order kept

- [ ] **Step 1: Write failing tests** in `server/tests/test_backup.py` (`TestStore`):
  - `test_folder_is_stable_and_unique`: two stores, keys `android_rmx3031_8fc4f390aaaa` and `android_rmx3031_1111222233` with the same display name → different `.dir`; re-creating the first store after changing display name → same `.dir` as before.
  - `test_rel_path`: `rel_path("/sdcard/DCIM/a.jpg") == "DCIM/a.jpg"`, `rel_path("/storage/emulated/0/DCIM/a.jpg") == "DCIM/a.jpg"`, `rel_path("/storage/1A2B-3C4D/x.mp4") == "_volumes/1A2B-3C4D/x.mp4"`.
  - `test_dest_refuses_escape`: `dest_for("/sdcard/../../etc/passwd")` resolves inside `.dir` or raises `BackupError` — assert result `.is_relative_to(store.dir)` when returned; `dest_for("/sdcard/a\nb")` raises `BackupError`.
  - `test_deleted_dest_collision`: create `Deleted on phone/DCIM/a.jpg` → `deleted_dest("/sdcard/DCIM/a.jpg").name == "a (2).jpg"`.
  - `test_manifest_roundtrip_atomic`: save/load equal; no `*.tmp` left.
  - `test_skip`: `.trashed-1-x.jpg`, `.pending-2-y.jpg` files and `.thumbnails` dir skipped; `.nomedia` kept.
  - Use `ACC_BACKUP_ROOT` = a `tempfile.TemporaryDirectory()` in `setUp`.
- [ ] **Step 2: Run** `cd server && .venv/bin/python -m unittest tests.test_backup -v` → fails (ImportError).
- [ ] **Step 3: Implement** the Task 1 interfaces in `backup.py`. Folder name sanitising: replace `/` and control chars with `_`, strip leading `.`; 8-char suffix = last `_`-separated segment of the key (ANDROID_ID / serial) `[:8]`; on name collision in `.phones.json` append ` 2`, ` 3`.
- [ ] **Step 4: Run tests** → all `TestStore` pass.

### Task 2: Engine — scan (A), diff, deleted-moves, copy with resume

**Files:**
- Modify: `server/app/services/backup.py`
- Test: `server/tests/test_backup.py` (`TestEngine`, with `FakeBackend`)

**Interfaces:**
- Consumes: Task 1.
- Backend protocol (already true for `StorageService` and `RelayStorage`): `list_dir(key, path) -> {"items": [{"name","path","is_dir","size","mtime"}]}`, `stat(key, path) -> {...same entry...}`, `read_range(key, path, start, length) -> AsyncIterator[bytes]`; errors raise `StorageError`.
- Produces:
  - `async walk(backend, key, root: str) -> List[Tuple[str, int, int]]` — `(phone_path, size, mtime)`, recursive, 4 dirs in flight (`asyncio.Semaphore(4)`), applies `skip`.
  - `class BackupJob(store: PhoneStore, backend, key: str, roots: List[str], scanner: Callable[[str], Awaitable[List[Tuple[str,int,int]]]], progress: Callable[[dict], None], cancel: asyncio.Event)` with `async run() -> dict` summary `{"copied","bytes","kept_deleted","errors": [str], "skipped_roots": [str], "status": "done"|"cancelled"|"error", "message"}`.
  - Progress dicts follow spec §2.6 (`phase, files_done, files_total, bytes_done, bytes_total, current, message`), throttled to ≤ 2/s except phase changes.

- [ ] **Step 1: Write failing tests** (`FakeBackend` holds `{path: bytes, mtime}` + dirs; can be told to raise `StorageError("Permission denied", 403)` for a root, and to raise after N bytes of `read_range` once):
  - `test_first_run_copies_all_and_preserves_mtime`: 3 files → summary `copied == 3`, bytes identical, `os.stat(dest).st_mtime == mtime`.
  - `test_second_run_copies_nothing`: rerun → `copied == 0`.
  - `test_changed_file_replaced`: change size → `copied == 1`, new content.
  - `test_deleted_on_phone_kept`: remove a file → `kept_deleted == 1`, file now at `Deleted on phone/<rel>`, gone from main tree, manifest `state == "deleted"`.
  - `test_failed_root_not_treated_as_deleted` (Review Focus 1): root raises permission error → `skipped_roots == [root]`, `kept_deleted == 0`, main-tree files untouched.
  - `test_resume_part_file` (Review Focus 2): first run, backend raises after 1000 bytes of a 5000-byte file → status `error`, `<dest>.part` exists with 1000 bytes; second run → file complete, byte-identical, and FakeBackend recorded a `read_range` call starting at 1000.
  - `test_reappeared_file` (Review Focus 5): delete → sync → re-add → sync: main-tree copy exists again and `Deleted on phone/` copy still exists.
  - `test_cancel`: cancel event set from progress callback after first file → status `cancelled`, manifest saved with the copied file.
- [ ] **Step 2: Run tests** → `TestEngine` fails.
- [ ] **Step 3: Implement** `walk` and `BackupJob.run`: per root: `scanner(root)` (catch `StorageError`/`OSError` → `skipped_roots`, continue; a 403/permission error adds the message `"All files access is off — on the phone: UNLOCK MORE POWERS → VAULT"` to `errors` once); for a custom entry that is a file, `stat` it instead (`roots` items whose `stat` says not dir). Diff per spec §2.3 only within successfully scanned roots (manifest entries whose path is under a scanned root or equal to a stat-ed file). Copy per spec §2.4: `.part` resume when `0 < part_size < size`, discard when `> size`; after copy verify size, `os.utime`, `os.replace`. `ENOSPC` → stop with `"Laptop disk full"`. Single-file `StorageError` → append to `errors`, continue. Disconnect-type errors (`StorageError` with status 503) → stop with `"Phone disconnected — press Sync now to resume"`. Save manifest every 50 copies and in `finally`. Write `last_run.json`.
- [ ] **Step 4: Run tests** → all pass.

### Task 3: Phone `scan` op (B) + server client + fallback

**Files:**
- Modify: `android-companion/app/src/main/java/com/operator/companion/relay/RemoteFileManager.kt` (dispatch near line 66, new `scan()`)
- Modify: `server/app/services/relay.py` (`RelayStorage.scan`)
- Modify: `server/app/services/backup.py` (`phone_scanner`)
- Modify: `android-companion/app/build.gradle.kts` (versionCode 25, versionName "1.8.7")
- Test: `server/tests/test_backup.py` (`TestPhoneScan`)

**Interfaces:**
- Phone op `"scan"` args `{path: String, page: Int}` → `FsResult(bytes = gzip(JSON))`, JSON `{"entries": [[rel, size, mtimeSec], ...], "next": Int|null}`; `rel` is relative to `path`, `/`-separated; walk with `java.io.File`, iterative, skip per Global Constraints, don't follow symlinked dirs; cache `path → List<entries>` for 120 s, page 0 always re-walks; `requireAccess()` already applies to all ops except roots.
- `RelayStorage.scan(rid: str, path: str, page: int) -> dict` — `request("scan", ..., timeout=300)`, `gzip.decompress` the payload, `json.loads`.
- `phone_scanner(relay_storage, rid) -> Callable[[str], Awaitable[List[Tuple[str,int,int]]]]` — pages until `next` is null; joins `root + "/" + rel`, rejects any `rel` that is absolute or contains a `..` segment or `\n` (log + skip); raises `ScanUnsupported` when the phone error message contains `Unknown storage operation`.
- `class ScanUnsupported(Exception)`; `BackupJob` on `ScanUnsupported` switches its scanner to `walk` for the rest of the run.
- Env `ACC_BACKUP_NO_SCAN=1` makes `phone_scanner` raise `ScanUnsupported` immediately (testing the A fallback over the relay).

- [ ] **Step 1: Write failing tests** (`TestPhoneScan`, fake relay storage):
  - `test_pages_joined`: two pages (5000 + 2) → 5002 tuples with absolute paths under the root.
  - `test_bad_rel_rejected` (Review Focus 3): entries `["../x", 1, 1]`, `["/etc/x", 1, 1]`, `["a\nb", 1, 1]` dropped; `["ok.jpg", 1, 1]` kept.
  - `test_unsupported_falls_back`: fake raises `StorageError("Unknown storage operation: scan", 400)` → `BackupJob` completes using `walk` and copies the files.
- [ ] **Step 2: Run tests** → fail.
- [ ] **Step 3: Implement** server side (`RelayStorage.scan`, `phone_scanner`, `ScanUnsupported`, fallback in `BackupJob`).
- [ ] **Step 4: Run tests** → pass.
- [ ] **Step 5: Implement** Kotlin `scan` op; bump version to 1.8.7 / 25.
- [ ] **Step 6: Build** `cd /home/mrityunjay/AndroidArchit && bash server/publish_apk.sh` → two `version_code: 25` lines, exit 0.

### Task 4: BackupService, API, progress, wiring

**Files:**
- Modify: `server/app/services/backup.py` (`BackupService`)
- Create: `server/app/api/backup_routes.py`
- Modify: `server/app/main.py` (construct service, `include_router`)
- Test: `server/tests/test_backup.py` (`TestService`)

**Interfaces:**
- Consumes: Tasks 1–3; `device_manager.get_device(id)`, `device_manager.devices`, `relays.get(rid)`, `ws_hub.broadcast_to_dashboards(dict)`.
- Produces `class BackupService(device_manager, storage, relay_storage, relays, broadcast: Callable[[dict], Awaitable[None]])`:
  - `phones() -> List[dict]` — online devices + phones in `.phones.json`: `{device_id, phone_key, name, folder, online, transport: "usb"|"relay"|None, selection, running, last_run}`
  - `set_selection(device_id, sel: dict) -> dict`, `add_custom(device_id, paths: List[str]) -> dict` (paths validated with `storage_service.normalize`)
  - `start(device_id) -> dict` (no-op returning status if already running), `cancel(device_id) -> dict`, `status(device_id) -> dict`
  - Backend choice: ADB `storage`/serial when on USB unless `ACC_PREFER_RELAY=1` and relay up; else `relay_storage`/relay_id; scanner = `phone_scanner` for relay, `walk` for ADB. Broadcast `{"type": "BACKUP_PROGRESS", "device_id": ..., "payload": progress}`.
- Routes (spec §4): `GET /api/backup`, `PUT /api/backup/{device_id}/selection`, `POST /api/backup/{device_id}/custom`, `POST /api/backup/{device_id}/run`, `POST /api/backup/{device_id}/cancel`, `GET /api/backup/{device_id}/status`; unknown device → 404; offline device on run → 409 "Phone is offline".

- [ ] **Step 1: Write failing tests** (`TestService`, fake device manager + fake backends): `test_two_phones_parallel_separate_folders` (two devices, start both, await both → each folder has only its own files); `test_start_twice_is_noop`; `test_offline_run_409` (service raises `BackupError` with status 409).
- [ ] **Step 2: Run** → fail. **Step 3: Implement** service + routes + wiring. **Step 4: Run** → pass; `.venv/bin/python -c "import app.main"` prints nothing on stderr.

### Task 5: Dashboard UI

**Files:**
- Create: `server/static/backup.html`, `server/static/js/backup.js`
- Modify: `server/static/files.html`, `server/static/js/files.js` ("Add to backup" on selection), `server/static/index.html` (nav link "Backup")

**Interfaces:** Consumes Task 4 routes and `BACKUP_PROGRESS` on `/ws/dashboard` (same token handling as `files.js`). Card contents per spec §4; preset labels: "Camera", "Screenshots", "WhatsApp media", "Documents", "Download". Reuse `files.html` CSS.

- [ ] **Step 1:** Build page; **Step 2:** open `http://localhost:8000/backup.html` in the built-in browser with the Realme online: card shows, ticking presets persists across reload, "Add to backup" from the file manager appears in the custom list.

### Task 6: Real-device verification + review + release

- [ ] **Step 1:** Install 1.8.7 on the Realme over USB (`adb install -r OperatorCompanion.apk`); restart server.
- [ ] **Step 2:** Create `/sdcard/Download/backup_test/` with ~20 files (incl. one 20 MB) via `adb push`; select it as custom; Sync over USB (A) → sha256 of every file == `adb pull` copy.
- [ ] **Step 3:** Restart server with `ACC_PREFER_RELAY=1`; Sync → log shows `scan` op (B); 0 copied (already synced). Delete one file via adb, add one → 1 copied, 1 in `Deleted on phone/`.
- [ ] **Step 4:** Force A over relay by sending a phone-version check fallback (`ACC_BACKUP_NO_SCAN=1`, see Task 3) → completes identically.
- [ ] **Step 5:** Start a sync of the 20 MB file over the relay and toggle the phone's mobile data off mid-copy → status "Phone disconnected…"; re-enable, Sync → resumes and file is identical.
- [ ] **Step 6:** One reviewer subagent over `backup.py`, `backup_routes.py`, `relay.py` scan, Kotlin `scan`, UI; fix findings; re-run unit tests.
- [ ] **Step 7:** Remove `/sdcard/Download/backup_test` and the test backup folder; restore server to normal mode; update memory.
