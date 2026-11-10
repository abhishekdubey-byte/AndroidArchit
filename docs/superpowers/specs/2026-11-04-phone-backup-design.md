# Phone Backup — Design

Date: 2026-11-04 · Status: approved in chat, awaiting spec review

## Goal

From the laptop dashboard, press **Sync now** for a phone and copy its chosen folders/files to a
dedicated laptop folder for that phone. Works over USB, same Wi-Fi and mobile data (Cloudflare relay),
for 5+ phones, each fully isolated.

## Decisions (from the user)

| Topic | Decision |
|---|---|
| Direction | One-way phone → laptop. Nothing on the phone is ever modified. |
| Deletions | File deleted on the phone is **kept** on the laptop, moved to `Deleted on phone/`. |
| Changed files | Laptop copy is replaced (no version history). |
| Trigger | **Manual only** — "Sync now" per phone. No schedules, no auto-sync. |
| Selection | Per phone: preset folders + custom folders/single files. |
| Location | `~/PhoneBackups/<phone>/` (outside the project). |
| Scanning | Both: **B** phone-side `scan` op when available, **A** laptop-walks-folders fallback. |

## 1. Storage layout & identity

```
~/PhoneBackups/
  .phones.json                        {phone_key: folder_name}
  Realme RMX3031 · 8fc4f390/
    DCIM/Camera/IMG_….jpg             same relative paths as on the phone (leading /sdcard/ stripped;
                                      other volumes under _volumes/<volume-id>/…)
    Deleted on phone/DCIM/Camera/…    files since deleted on the phone
    .backup/selection.json            {presets: [...], custom: [...]}
    .backup/manifest.json             {path: {size, mtime, state: "present"|"deleted"}}
    .backup/last_run.json             summary of the last run
```

- **phone_key** = the companion relay id (`android_<model>_<ANDROID_ID>`, stable across networks and
  reinstalls). Phones without the companion app (ADB-only) use `adb:<serial>`.
- **Folder name** = `<Brand Model> · <first 8 chars of the ANDROID_ID part>` (or of the serial), chosen
  on the first backup and recorded in `.phones.json`. It never changes afterwards, so later renames
  never move or split a backup. Name collisions get a numeric suffix.
- When a phone is on USB, its relay id is read from the published `relay_id` file (existing
  `DeviceManager._relay_id_for`), so USB and remote runs land in the same folder.

## 2. Sync engine — `server/app/services/backup.py`

One run per phone (`BackupJob`), phones run in parallel. A second "Sync now" on a running phone is a
no-op that returns the running job's status. Every job can be cancelled.

1. **Backend** — same rule as `storage_routes._backend`: ADB `StorageService` when on USB/ADB, otherwise
   `RelayStorage`. `ACC_PREFER_RELAY=1` forces the relay (testing).
2. **Scan** each selected root:
   - **B** (relay backend only): `scan` op, paged (see §3). If the phone replies
     `Unknown storage operation`, remember it for this run and use A.
   - **A**: recursive `list_dir` from the laptop, 4 directories in flight.
   - Skip `.trashed-*`, `.pending-*` files and `.thumbnails` directories.
   - A selected single file is `stat`-ed instead of scanned.
3. **Diff** against the manifest:
   - not in manifest, or size/mtime differ, or state `deleted` → **download**.
   - in manifest as `present`, under a root that **scanned successfully**, but absent from the scan →
     move to `Deleted on phone/<rel path>` (if a file already exists there, add ` (2)`, ` (3)`…),
     mark `deleted`.
   - **Safety rule:** a root that fails to scan (permission missing, volume gone, disconnect, error) is
     reported and **skipped entirely** — its manifest entries are left untouched, never marked deleted.
4. **Copy** each file: stream with `read_range` into `<dest>.part` (resume from the `.part` size if it
   exists and is smaller than the phone size; restart if larger). When the `.part` size equals the phone
   size: `os.utime` to the phone mtime, atomic rename into place, update manifest. Manifest is written
   (atomically, tmp + rename) every 50 files and at the end.
5. **Path safety:** every phone path is normalised; the destination must resolve inside the phone's
   folder (`Path.resolve()` + `is_relative_to`). Anything else is refused and logged. Folder names are
   sanitised (no `/`, no leading `.`).
6. **Progress** is broadcast to dashboards as `BACKUP_PROGRESS`
   `{device_id, phase: scanning|copying|done|error|cancelled, files_done, files_total, bytes_done,
   bytes_total, current, message}` at most twice per second.

## 3. Phone side — app v1.8.7, `RemoteFileManager.kt`

New read-only op `scan`:

- args: `{path, page}` (page 0 starts a new walk). The phone walks `path` with `java.io.File`
  (existing All-files-access check applies), applying the same skip rules, and caches the result for
  2 minutes keyed by `path`.
- reply: `bytes` = gzip of JSON `{"entries": [[relPath, size, mtimeSec], ...], "next": page+1 | null}`,
  5,000 entries per page (well under the relay's 16 MiB message cap).
- Nothing else in the app changes. Older apps keep working via A.

## 4. Dashboard & API

- New page `server/static/backup.html` (+ `js/backup.js`), linked from the dashboard and file manager.
  One card per known phone (online now, or with an existing backup folder):
  status (USB / relay / offline) · preset checkboxes · custom list with ✕ · **Sync now** / **Cancel** ·
  live progress bar · last-run summary · laptop folder path.
- Presets (each may expand to several phone paths; missing ones are silently skipped):
  - Camera → `/sdcard/DCIM/Camera`
  - Screenshots → `/sdcard/Pictures/Screenshots`, `/sdcard/DCIM/Screenshots`
  - WhatsApp media → `/sdcard/Android/media/com.whatsapp/WhatsApp/Media`, `/sdcard/WhatsApp/Media`
  - Documents → `/sdcard/Documents`
  - Download → `/sdcard/Download`
- File manager (`files.html`): selection → **Add to backup** (adds to that phone's custom list).
- API (same auth as the rest of `/api`):
  - `GET  /api/backup` — phones with selection, status, last run, folder path
  - `PUT  /api/backup/{device_id}/selection` — `{presets: [...], custom: [...]}`
  - `POST /api/backup/{device_id}/custom` — `{paths: [...]}` append (used by "Add to backup")
  - `POST /api/backup/{device_id}/run` · `POST /api/backup/{device_id}/cancel`
  - `GET  /api/backup/{device_id}/status`

## 5. Errors

| Situation | Behaviour |
|---|---|
| Phone disconnects mid-run | Wait up to 60 s for it (`relays.wait_for`), then stop: "Phone disconnected — press Sync now to resume". `.part` files are kept for resume. |
| All files access off | Root fails → skipped, message: "All files access is off — on the phone: UNLOCK MORE POWERS → VAULT". |
| Single file unreadable | Logged in the run summary, skipped, run continues. |
| Laptop disk full (`ENOSPC`) | Stop with "Laptop disk full". |
| Path escapes phone folder | Refused, logged. |

## 6. Testing

- **Automated** (`server/tests/test_backup.py`, stdlib unittest, fake backend): diff/new/changed/deleted, safety rule
  (failed root ≠ deletions), `Deleted on phone` move + name collision, `.part` resume, path-escape
  refusal, two phones → two folders.
- **Real device (Realme, USB + relay):** back up a test folder via USB (A), via relay with B, and via
  relay with B disabled (A); sha256 of every file equals `adb pull`; delete a file on the phone → lands
  in `Deleted on phone/`; interrupt mid-copy → next run resumes; second run copies 0 files.
- One reviewer subagent pass before building the release APK.

## Out of scope

Two-way sync, schedules/auto-sync, version history, backing up app-private data (`/data`), restore to
phone (the existing file manager upload can be used manually).
