"""Phone Backup: one-way copy of chosen phone folders/files into a dedicated laptop folder per phone.

Works over either storage backend (ADB StorageService keyed by serial, or RelayStorage keyed by the
companion relay id) — they share list_dir / stat / read_range. Nothing on the phone is ever modified.
Files deleted on the phone are kept on the laptop under "Deleted on phone/".
See docs/superpowers/specs/2026-11-04-phone-backup-design.md.
"""
import json
import os
import posixpath
import re
from pathlib import Path
from typing import Dict, List, Optional

PRESETS: Dict[str, List[str]] = {
    "camera": ["/sdcard/DCIM/Camera"],
    "screenshots": ["/sdcard/Pictures/Screenshots", "/sdcard/DCIM/Screenshots"],
    "whatsapp": ["/sdcard/Android/media/com.whatsapp/WhatsApp/Media", "/sdcard/WhatsApp/Media"],
    "documents": ["/sdcard/Documents"],
    "download": ["/sdcard/Download"],
}
DELETED_DIR = "Deleted on phone"
META_DIR = ".backup"
RESERVED = {DELETED_DIR, META_DIR, "_volumes", "_root"}


class BackupError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def backup_root() -> Path:
    return Path(os.getenv("ACC_BACKUP_ROOT") or Path.home() / "PhoneBackups")


def phone_key(dev) -> str:
    return dev.relay_id or f"adb:{dev.serial}"


def skip(name: str, is_dir: bool) -> bool:
    """Android's trash/temporary entries are not user data."""
    if is_dir:
        return name == ".thumbnails"
    return name.startswith(".trashed-") or name.startswith(".pending-")


def _clean_phone_path(phone_path: str) -> str:
    if not phone_path.startswith("/") or any(c in phone_path for c in "\x00\n\r"):
        raise BackupError(f"Refused odd phone path: {phone_path!r}")
    return posixpath.normpath(phone_path)


def rel_path(phone_path: str) -> str:
    """Where a phone file lives inside its phone's backup folder."""
    p = _clean_phone_path(phone_path)
    for internal in ("/sdcard", "/storage/emulated/0", "/storage/self/primary"):
        if p == internal or p.startswith(internal + "/"):
            rel = p[len(internal):].lstrip("/")
            if rel.split("/", 1)[0] in RESERVED:
                raise BackupError(f"Skipped {p}: its folder name is reserved by the backup layout")
            return rel
    m = re.match(r"^/storage/([^/]+)(/.*)?$", p)
    if m:
        return f"_volumes/{m.group(1)}" + (m.group(2) or "")
    return "_root" + p


def _safe_name(text: str) -> str:
    text = re.sub(r"[/\x00-\x1f]", "_", text).strip().lstrip(".")
    return text or "Phone"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def selected_paths(sel: dict) -> List[str]:
    out: List[str] = []
    for name in sel.get("presets", []):
        out += PRESETS.get(name, [])
    out += sel.get("custom", [])
    seen, result = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


class PhoneStore:
    """One phone's backup folder: fixed name (recorded in .phones.json), selection, manifest, last run."""

    def __init__(self, root: Path, key: str, display_name: str, aliases: Optional[List[str]] = None):
        self.root = Path(root)
        self.key = key
        self.aliases = [a for a in (aliases or []) if a and a != key]
        self._index_file = self.root / ".phones.json"
        index = _read_json(self._index_file, {})
        # the same phone may first be seen as adb:<serial> and later by its relay id (or the reverse)
        folder = index.get(key) or next((index[a] for a in self.aliases if a in index), None)
        self._claimed = key in index and all(a in index for a in self.aliases)
        if not folder:
            suffix = key.rsplit("_", 1)[-1].rsplit(":", 1)[-1][:8]
            base = _safe_name(f"{display_name} · {suffix}")
            taken = set(index.values())
            folder, n = base, 2
            while folder in taken:
                folder, n = f"{base} {n}", n + 1
        self.dir = self.root / folder
        self.meta = self.dir / META_DIR

    def _claim(self) -> None:
        """Fix the folder name forever — only once something is actually stored (viewing must not)."""
        if self._claimed:
            return
        index = _read_json(self._index_file, {})
        for k in [self.key] + self.aliases:
            index.setdefault(k, self.dir.name)
        _write_json(self._index_file, index)
        self.dir = self.root / index[self.key]
        self.meta = self.dir / META_DIR
        self._claimed = True

    def load_selection(self) -> dict:
        sel = _read_json(self.meta / "selection.json", {})
        return {"presets": list(sel.get("presets", [])), "custom": list(sel.get("custom", []))}

    def save_selection(self, sel: dict) -> None:
        self._claim()
        _write_json(self.meta / "selection.json", {"presets": sel.get("presets", []), "custom": sel.get("custom", [])})

    def load_manifest(self) -> Dict[str, dict]:
        return _read_json(self.meta / "manifest.json", {})

    def save_manifest(self, manifest: Dict[str, dict]) -> None:
        self._claim()
        _write_json(self.meta / "manifest.json", manifest)

    def load_last_run(self) -> Optional[dict]:
        return _read_json(self.meta / "last_run.json", None)

    def save_last_run(self, data: dict) -> None:
        self._claim()
        _write_json(self.meta / "last_run.json", data)

    def _inside(self, rel: str, base: Path) -> Path:
        dest = (base / rel).resolve()
        if not dest.is_relative_to(self.dir.resolve()) or dest == self.dir.resolve():
            raise BackupError(f"Refused path outside the phone's folder: {rel!r}")
        if dest.is_relative_to(self.meta.resolve()):
            raise BackupError(f"Refused path inside the backup metadata: {rel!r}")
        return dest

    def part_for(self, phone_path: str) -> Path:
        """Unfinished download of one phone file (+ ".json" with the size/mtime it belongs to)."""
        import hashlib
        return self.meta / "parts" / (hashlib.sha1(phone_path.encode()).hexdigest() + ".part")

    def dest_for(self, phone_path: str) -> Path:
        return self._inside(rel_path(phone_path), self.dir)

    def deleted_dest(self, phone_path: str) -> Path:
        dest = self._inside(rel_path(phone_path), self.dir / DELETED_DIR)
        n = 2
        candidate = dest
        while candidate.exists():
            candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
            n += 1
        return candidate


# ---------------------------------------------------------------- engine

import asyncio  # noqa: E402
import errno  # noqa: E402
import time  # noqa: E402
from typing import Awaitable, Callable, Tuple  # noqa: E402

from .storage_service import StorageError  # noqa: E402

Entry = Tuple[str, int, int]  # (phone path, size, mtime seconds)
ACCESS_HINT = "All files access is off — on the phone: UNLOCK MORE POWERS → VAULT"
DISCONNECTED = "Phone disconnected — press Sync now to resume"


class ScanUnsupported(Exception):
    """The phone's app is too old for the fast `scan` op: walk the folders from the laptop instead."""


def _is_access_error(e: Exception) -> bool:
    status = getattr(e, "status", 0)
    text = str(getattr(e, "message", e)).lower()
    return status in (401, 403, 412) or "permission" in text or "access" in text


def _is_disconnect(e: Exception) -> bool:
    return isinstance(e, (asyncio.TimeoutError, ConnectionError)) or getattr(e, "status", 0) in (503, 504)


async def walk(backend, key: str, root: str) -> List[Entry]:
    """Scan A: list folders from the laptop, 4 at a time. Any failure fails the whole root (never a partial
    scan: files in an unreadable subfolder would otherwise look deleted)."""
    out: List[Entry] = []
    sem = asyncio.Semaphore(4)

    async def visit(path: str):
        async with sem:
            listing = await backend.list_dir(key, path)
        subs = []
        for e in listing["items"]:
            if skip(e["name"], e["is_dir"]):
                continue
            if e["is_dir"]:
                if not e.get("is_link"):
                    subs.append(e["path"])
            else:
                out.append((e["path"], int(e["size"]), int(e["mtime"])))
        await asyncio.gather(*(visit(s) for s in subs))

    await visit(root)
    return out


class BackupJob:
    """One "Sync now" for one phone."""

    def __init__(self, store: PhoneStore, backend, key: str, roots: List[str],
                 scanner: Callable[[str], Awaitable[List[Entry]]],
                 progress: Callable[[dict], None], cancel: asyncio.Event):
        self.store, self.backend, self.key, self.roots = store, backend, key, roots
        self.scanner, self.progress, self.cancel = scanner, progress, cancel
        self.fallback_scanner: Optional[Callable[[str], Awaitable[List[Entry]]]] = None
        self.min_interval = 0.5
        self.scan_mode = getattr(scanner, "mode", "laptop")  # "phone" = fast scan B, "laptop" = walk A
        self._last_emit = 0.0
        self.state = {"phase": "scanning", "files_done": 0, "files_total": 0, "bytes_done": 0,
                      "bytes_total": 0, "current": "", "message": ""}

    def _emit(self, force: bool = False, **changes):
        self.state.update(changes)
        now = time.monotonic()
        if force or now - self._last_emit >= self.min_interval:
            self._last_emit = now
            try:
                self.progress(dict(self.state))
            except Exception:
                pass

    async def _scan(self, root: str) -> List[Entry]:
        try:
            return await self.scanner(root)
        except ScanUnsupported:
            self.scanner = self.fallback_scanner or (lambda r: walk(self.backend, self.key, r))
            self.scan_mode = "laptop"
            return await self.scanner(root)

    async def run(self) -> dict:
        s = {"copied": 0, "bytes": 0, "kept_deleted": 0, "errors": [], "skipped_roots": [],
             "status": "done", "message": "", "started": time.time()}
        manifest = self.store.load_manifest()
        self.store.dir.mkdir(parents=True, exist_ok=True)

        def err(msg: str):
            if msg not in s["errors"] and len(s["errors"]) < 50:
                s["errors"].append(msg)

        def under(path: str, tops: List[str]) -> bool:
            return any(path == t or path.startswith(t + "/") for t in tops)

        try:
            # 1. scan
            self._emit(force=True, phase="scanning")
            found: Dict[str, Tuple[int, int]] = {}
            covered: List[str] = []
            for root in self.roots:
                if self.cancel.is_set():
                    raise asyncio.CancelledError
                self._emit(current=root)
                try:
                    st = await self.backend.stat(self.key, root)
                    if st["is_dir"]:
                        entries = await self._scan(root)
                    else:
                        entries = [(root, int(st["size"]), int(st["mtime"]))]
                except (StorageError, OSError, asyncio.TimeoutError) as e:
                    if _is_disconnect(e):
                        raise
                    if getattr(e, "status", 0) == 404 and not any(under(p, [root]) for p in manifest):
                        continue  # folder simply isn't on this phone (e.g. old WhatsApp location)
                    s["skipped_roots"].append(root)
                    err(ACCESS_HINT if _is_access_error(e) else f"{root}: {getattr(e, 'message', e)}")
                    continue
                had = sum(1 for p, m in manifest.items() if m.get("state") == "present" and under(p, [root]))
                if not entries and had:
                    # an existing folder that suddenly lists nothing is far more likely an Android/OEM
                    # access quirk than the user deleting everything: never mass-move on that
                    s["skipped_roots"].append(root)
                    err(f"{root}: the phone listed no files here although {had} were backed up — skipped to be safe")
                    continue
                covered.append(root)
                for p, size, mtime in entries:
                    found[p] = (size, mtime)

            # 2. diff
            gone = [p for p, m in manifest.items() if m.get("state") == "present" and under(p, covered) and p not in found]
            todo = []
            for p, (size, mtime) in found.items():
                m = manifest.get(p)
                try:
                    dest = self.store.dest_for(p)
                except BackupError as e:
                    err(e.message)
                    continue
                if m is None or m.get("state") != "present" or m.get("size") != size \
                        or m.get("mtime") != mtime or not dest.exists():
                    todo.append((p, size, mtime, dest))

            # 3. keep what was deleted on the phone
            for p in gone:
                try:
                    src = self.store.dest_for(p)
                    if src.exists():
                        target = self.store.deleted_dest(p)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(src, target)
                except (BackupError, OSError) as e:
                    err(f"Could not keep {p}: {e}")
                    continue
                manifest[p] = {**manifest[p], "state": "deleted"}
                s["kept_deleted"] += 1

            # 4. copy
            total = sum(t[1] for t in todo)
            self._emit(force=True, phase="copying", files_total=len(todo), bytes_total=total, files_done=0, bytes_done=0)
            for i, (p, size, mtime, dest) in enumerate(todo):
                if self.cancel.is_set():
                    raise asyncio.CancelledError
                self._emit(current=p)
                try:
                    await self._copy(p, size, mtime, dest)
                except Exception as e:
                    if _is_disconnect(e) or (isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EROFS)):
                        raise
                    if not isinstance(e, (StorageError, OSError, BackupError)):
                        raise
                    err(f"{p}: {getattr(e, 'message', None) or str(e) or type(e).__name__}")
                    print(f"[Backup] {p}: {e!r}")
                    continue
                manifest[p] = {"size": size, "mtime": mtime, "state": "present"}
                s["copied"] += 1
                s["bytes"] += size
                if s["copied"] % 50 == 0:
                    self.store.save_manifest(manifest)
                self._emit(files_done=i + 1)
        except asyncio.CancelledError:
            s["status"], s["message"] = "cancelled", "Cancelled"
        except Exception as e:
            s["status"] = "error"
            if _is_disconnect(e):
                s["message"] = DISCONNECTED
            elif isinstance(e, OSError) and e.errno == errno.ENOSPC:
                s["message"] = "Laptop disk full"
            elif isinstance(e, OSError) and e.errno == errno.EROFS:
                s["message"] = "The backup folder is read-only"
            elif isinstance(e, (StorageError, BackupError)):
                s["message"] = str(e.message)
            elif isinstance(e, OSError):
                s["message"] = f"Laptop error: {e}"
            else:
                s["message"] = f"Unexpected error: {e!r}"
                import traceback
                traceback.print_exc()
        finally:
            try:
                self.store.save_manifest(manifest)
            except Exception as e:
                print(f"[Backup] could not save the manifest: {e}")
        if s["status"] == "done":
            s["message"] = f"{s['copied']} copied · {s['kept_deleted']} kept from phone deletions · {len(s['errors'])} errors"
        s["finished"] = time.time()
        s["scan"] = self.scan_mode
        try:
            self.store.save_last_run(s)
        except Exception as e:
            print(f"[Backup] could not save the run summary: {e}")
        self._emit(force=True, phase=s["status"], message=s["message"], current="")
        return s

    async def _copy(self, p: str, size: int, mtime: int, dest: Path):
        part = self.store.part_for(p)
        tag = part.with_name(part.name + ".json")
        part.parent.mkdir(parents=True, exist_ok=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        start = part.stat().st_size if part.exists() else 0
        if start and (_read_json(tag, {}) != {"size": size, "mtime": mtime} or start > size):
            start = 0  # left over from another version of the file: never resume from it
        _write_json(tag, {"size": size, "mtime": mtime})
        with open(part, "ab" if start else "wb") as f:
            if size > start:
                async for chunk in self.backend.read_range(self.key, p, start, size - start):
                    f.write(chunk)
                    self._emit(bytes_done=self.state["bytes_done"] + len(chunk))
                    if self.cancel.is_set():
                        raise asyncio.CancelledError  # part kept: the next run resumes it
        got = part.stat().st_size
        if got < size:
            # The stream ended early. Over USB an unplugged cable looks exactly like this.
            st = await self.backend.stat(self.key, p)  # raises the disconnect if the phone is gone
            if int(st.get("size", -1)) == size and int(st.get("mtime", -1)) == mtime:
                raise StorageError(DISCONNECTED, 503)  # same file, connection dropped: keep the part
        if got != size:
            part.unlink(missing_ok=True)
            tag.unlink(missing_ok=True)
            raise StorageError(f"changed while copying ({got} of {size} bytes) — will retry next sync", 409)
        os.utime(part, (mtime, mtime))
        os.replace(part, dest)
        tag.unlink(missing_ok=True)


MAX_SCAN_PAGES = 2000  # 10M files: far beyond any phone


def phone_scanner(relay_storage, rid: str) -> Callable[[str], Awaitable[List[Entry]]]:
    """Scan B: the phone walks the folder itself and returns the whole tree in pages."""
    async def scan(root: str) -> List[Entry]:
        if os.getenv("ACC_BACKUP_NO_SCAN") == "1":
            raise ScanUnsupported()
        out: List[Entry] = []
        page: Optional[int] = 0
        scan_id = None
        while page is not None:
            if page >= MAX_SCAN_PAGES:
                raise StorageError(f"{root}: the phone kept sending scan pages — skipped", 502)
            try:
                data = await relay_storage.scan(rid, root, page, scan_id)
            except StorageError as e:
                if "unknown storage operation" in str(e.message).lower():
                    raise ScanUnsupported() from e
                raise
            for item in data.get("entries", []):
                rel, size, mtime = str(item[0]), int(item[1]), int(item[2])
                parts = rel.split("/")
                if not rel or rel.startswith("/") or ".." in parts or "" in parts or \
                        any(c in rel for c in "\x00\n\r"):
                    print(f"[Backup] ignored odd path from phone {rid}: {rel!r}")
                    continue
                if skip(parts[-1], False) or any(skip(d, True) for d in parts[:-1]):
                    continue
                out.append((f"{root}/{rel}", size, mtime))
            scan_id = data.get("scan_id", scan_id)
            nxt = data.get("next")
            page = int(nxt) if nxt is not None and int(nxt) == page + 1 else None if nxt is None else MAX_SCAN_PAGES
        return out
    scan.mode = "phone"
    return scan


# ---------------------------------------------------------------- service

class BackupService:
    """Owns one backup job per phone (phones run in parallel, each in its own folder)."""

    def __init__(self, device_manager, storage, relay_storage, relays,
                 broadcast: Optional[Callable[[dict], Awaitable[None]]] = None):
        self.dm, self.storage, self.relay_storage, self.relays = device_manager, storage, relay_storage, relays
        self.broadcast = broadcast
        self.tasks: Dict[str, asyncio.Task] = {}
        self.cancels: Dict[str, asyncio.Event] = {}
        self.live: Dict[str, dict] = {}

    # --- helpers
    def _device(self, device_id: str):
        dev = self.dm.get_device(device_id)
        if not dev:
            raise BackupError(f"Phone '{device_id}' not found", 404)
        return dev

    def store_for(self, dev) -> PhoneStore:
        # brand + model, not dev.name: the USB entry's name carries the serial, the relay entry's doesn't
        name = f"{str(getattr(dev, 'brand', '') or '').title()} {getattr(dev, 'model', '') or ''}".strip() or dev.name
        # a USB phone is also known by its serial: keep one folder whichever id is readable at the time
        aliases = [f"adb:{dev.serial}"] if dev.relay_id and dev.transport == "adb" else []
        return PhoneStore(backup_root(), phone_key(dev), name, aliases)

    def _route(self, dev):
        """(backend, key, scanner, transport): USB/ADB when plugged in, otherwise the companion relay."""
        on_adb = dev.transport == "adb" and dev.status != "offline"
        relay_up = bool(dev.relay_id) and self.relays.get(dev.relay_id) is not None
        if on_adb and not (relay_up and os.getenv("ACC_PREFER_RELAY") == "1"):
            be, key = self.storage, dev.serial
            return be, key, (lambda r: walk(be, key, r)), "usb"
        if relay_up:
            return self.relay_storage, dev.relay_id, phone_scanner(self.relay_storage, dev.relay_id), "relay"
        raise BackupError("Phone is offline: connect it by USB or open Operator Companion on it", 409)

    def _transport(self, dev) -> Optional[str]:
        try:
            return self._route(dev)[3]
        except BackupError:
            return None

    def _progress(self, device_id: str, key: str, p: dict):
        self.live[key] = p
        if self.broadcast:
            asyncio.ensure_future(self.broadcast({"type": "BACKUP_PROGRESS", "device_id": device_id, "payload": p}))

    # --- API
    def status(self, device_id: str) -> dict:
        dev = self._device(device_id)
        store, key = self.store_for(dev), phone_key(dev)
        return {"device_id": dev.id, "phone_key": key, "name": store.dir.name, "folder": str(store.dir),
                "online": self._transport(dev) is not None, "transport": self._transport(dev),
                "selection": store.load_selection(), "running": key in self.tasks,
                "progress": self.live.get(key) if key in self.tasks else None, "last_run": store.load_last_run()}

    def phones(self) -> List[dict]:
        out, seen, folders = [], set(), set()
        for dev in list(self.dm.devices.values()):
            key = phone_key(dev)
            if key in seen:
                continue
            # a phone on USB and on the relay appears twice: show the USB entry
            if dev.transport != "adb" and any(d.relay_id == dev.relay_id and d.id != dev.id
                                              for d in self.dm.devices.values()):
                continue
            seen.add(key)
            st = self.status(dev.id)
            folders.add(Path(st["folder"]).name)
            out.append(st)
        index = _read_json(backup_root() / ".phones.json", {})
        for key, folder in index.items():
            if key not in seen and folder not in folders:
                folders.add(folder)
                store_dir = backup_root() / folder
                out.append({"device_id": None, "phone_key": key, "name": folder, "folder": str(store_dir),
                            "online": False, "transport": None, "running": False, "progress": None,
                            "selection": _read_json(store_dir / META_DIR / "selection.json", {"presets": [], "custom": []}),
                            "last_run": _read_json(store_dir / META_DIR / "last_run.json", None)})
        return out

    def set_selection(self, device_id: str, sel: dict) -> dict:
        presets = list(dict.fromkeys(sel.get("presets", [])))
        bad = [p for p in presets if p not in PRESETS]
        if bad:
            raise BackupError(f"Unknown preset: {', '.join(bad)}")
        custom = self._clean_custom(sel.get("custom", []))
        store = self.store_for(self._device(device_id))
        store.save_selection({"presets": presets, "custom": custom})
        return store.load_selection()

    def add_custom(self, device_id: str, paths: List[str]) -> dict:
        store = self.store_for(self._device(device_id))
        sel = store.load_selection()
        sel["custom"] = self._clean_custom(sel["custom"] + list(paths))
        store.save_selection(sel)
        return sel

    @staticmethod
    def _clean_custom(paths: List[str]) -> List[str]:
        from .storage_service import normalize
        out = []
        for p in paths:
            try:
                n = normalize(str(p))
            except StorageError as e:
                raise BackupError(e.message)
            if n == "/":
                raise BackupError("Choose a folder, not the whole phone")
            if n not in out:
                out.append(n)
        return out

    def start(self, device_id: str) -> dict:
        dev = self._device(device_id)
        key = phone_key(dev)
        if key in self.tasks:
            return self.status(device_id)
        backend, bkey, scanner, _ = self._route(dev)
        store = self.store_for(dev)
        roots = selected_paths(store.load_selection())
        if not roots:
            raise BackupError("Choose at least one folder to back up first")
        cancel = asyncio.Event()
        job = BackupJob(store, backend, bkey, roots, scanner,
                        lambda p, i=dev.id, k=key: self._progress(i, k, p), cancel)
        job.fallback_scanner = lambda r: walk(backend, bkey, r)
        self.cancels[key] = cancel
        self.tasks[key] = asyncio.ensure_future(self._run(key, job))
        print(f"[Backup] {dev.name}: sync started ({len(roots)} folders) → {store.dir}")
        return self.status(device_id)

    async def _run(self, key: str, job: BackupJob):
        try:
            s = await job.run()
            print(f"[Backup] {job.store.dir.name}: {s['status']} — {s['message']}")
        except Exception as e:  # never leave a phone stuck as "running"
            print(f"[Backup] {job.store.dir.name}: failed — {e}")
        finally:
            self.tasks.pop(key, None)
            self.cancels.pop(key, None)

    def cancel(self, device_id: str) -> dict:
        key = phone_key(self._device(device_id))
        if key in self.cancels:
            self.cancels[key].set()
        return self.status(device_id)
