"""ADB-backed device storage access (browse / download / upload / manage).

Works on Android 5.0 through 15+ without any companion app or runtime
permission: the adb `shell` user can read and write shared storage on every
version, scoped storage does not apply to it.  Version differences handled:

* Android < 7 adb shell has no exit codes and merges stderr into stdout
  (and uses CRLF) -> every command appends an explicit return-code marker.
* Android 5 ships toolbox (no `stat`) -> `ls -la` parser fallback.
* Android <= 10 does not auto-index files written over adb -> media scan
  broadcast after uploads / deletes.
"""
import asyncio
import mimetypes
import posixpath
import re
import shlex
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote

from ..config import ADB_PATH, UPLOADS_DIR

RC_MARK = "__ACC_RC="
STAT_FMT = "%A|%s|%Y|%n"
SHARED_ROOTS = ("/sdcard", "/mnt/sdcard", "/storage", "/data/local/tmp")
# Deleting/renaming a direct child of these would remove a whole volume.
VOLUME_PARENTS = {"/", "/storage", "/storage/emulated", "/storage/self", "/mnt", "/data/local"}
VOLUME_ROOTS = {"/sdcard", "/mnt/sdcard", "/storage/emulated/0", "/storage/self/primary", "/data/local/tmp"}
INTERNAL_ALIASES = {"emulated", "self", "enc_emulated", "sdcard0", "legacy", "knox-emulated", "container"}
SHORTCUTS = [
    ("Camera", "DCIM/Camera"), ("Screenshots", "Pictures/Screenshots"), ("Screenshots", "DCIM/Screenshots"),
    ("Downloads", "Download"), ("Pictures", "Pictures"), ("Movies", "Movies"), ("Music", "Music"),
    ("Documents", "Documents"), ("WhatsApp", "Android/media/com.whatsapp/WhatsApp/Media"),
    ("WhatsApp", "WhatsApp/Media"),
]
_LS_DATE = re.compile(r"\s(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(?: [+-]\d{4})? (.+)$")


def _kill(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # adb already exited (cable pulled / adb restarted)


class StorageError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def normalize(path: str) -> str:
    if not path or not path.startswith("/") or "\x00" in path or "\n" in path:
        raise StorageError(f"Invalid path: {path!r}")
    return "/" + posixpath.normpath(path).lstrip("/")


def _in_shared(p: str) -> bool:
    return any(p == r or p.startswith(r + "/") for r in SHARED_ROOTS)


def writable_dir(path: str) -> str:
    p = normalize(path)
    if not _in_shared(p) or p in ("/storage", "/storage/emulated", "/storage/self"):
        raise StorageError(f"Writing is only allowed inside shared storage, not {p}", 403)
    return p


def removable(path: str) -> str:
    p = normalize(path)
    if not _in_shared(p) or p in VOLUME_ROOTS or posixpath.dirname(p) in VOLUME_PARENTS:
        raise StorageError(f"Refusing to modify protected location {p}", 403)
    return p


FAT_INVALID = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def valid_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\x00" in name or "\n" in name:
        raise StorageError(f"Invalid name: {name!r}")
    if FAT_INVALID.search(name):
        raise StorageError('Android storage does not allow these characters in names: \\ : * ? " < > |')
    return name


def safe_upload_name(name: str) -> str:
    """Laptop file names may contain characters Android shared storage rejects (e.g. 'Shot 10:22.png')."""
    cleaned = FAT_INVALID.sub("_", (name or "").strip()).rstrip(" .")
    return valid_name(cleaned or "upload")


def q(p: str) -> str:
    return shlex.quote(p)


class StorageService:
    def __init__(self, adb_path: str = ADB_PATH):
        self.adb = adb_path
        self._has_stat: Dict[str, bool] = {}
        self._sdk: Dict[str, int] = {}
        self._transfer_slots: Dict[str, asyncio.Semaphore] = {}

    # ---------- transport ----------
    @staticmethod
    def _raise_transport(err: str):
        low = "\n".join(l for l in err.lower().splitlines() if l.startswith(("error:", "adb:")))
        if "unauthorized" in low:
            raise StorageError("USB debugging is not authorized on the device (accept the prompt on the phone)", 503)
        if "offline" in low or "no devices" in low or ("device '" in low and "not found" in low):
            raise StorageError("Device is disconnected or offline", 503)

    async def _exec(self, args: List[str], timeout: Optional[float]) -> Tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            self.adb, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            _kill(proc)
            await proc.wait()
            raise StorageError("Device did not respond in time", 504)
        except asyncio.CancelledError:
            _kill(proc)
            raise
        return proc.returncode or 0, out, err

    async def shell(self, serial: str, cmd: str, timeout: float = 30.0) -> Tuple[int, str]:
        _, out, err = await self._exec(["-s", serial, "shell", f"{cmd}; echo {RC_MARK}$?"], timeout)
        errtext = err.decode("utf-8", "replace")
        self._raise_transport(errtext)
        text = out.decode("utf-8", "replace").replace("\r\n", "\n")
        idx = text.rfind(RC_MARK)
        if idx == -1:
            self._raise_transport(text)
            raise StorageError(errtext.strip() or "No response from device", 502)
        rc_text = text[idx + len(RC_MARK):].strip()
        return (int(rc_text) if rc_text.isdigit() else 1), text[:idx]

    def _slot(self, serial: str) -> asyncio.Semaphore:
        return self._transfer_slots.setdefault(serial, asyncio.Semaphore(3))

    async def _supports_stat(self, serial: str) -> bool:
        if serial not in self._has_stat:
            rc, out = await self.shell(serial, "stat -L -c '%s' / 2>/dev/null")
            self._has_stat[serial] = rc == 0 and out.strip().isdigit()
        return self._has_stat[serial]

    async def sdk(self, serial: str) -> int:
        if serial not in self._sdk:
            _, out = await self.shell(serial, "getprop ro.build.version.sdk")
            self._sdk[serial] = int(out.strip()) if out.strip().isdigit() else 0
        return self._sdk[serial]

    # ---------- parsing ----------
    @staticmethod
    def _entry(parent: str, name: str, perm: str, size: int, mtime: int) -> Dict:
        return {
            "name": name,
            "path": posixpath.join(parent, name),
            "is_dir": perm.startswith("d"),
            "is_link": perm.startswith("l"),
            "size": size,
            "mtime": mtime,
            "mode": perm,
            "hidden": name.startswith("."),
            "mime": None if perm.startswith("d") else mimetypes.guess_type(name)[0],
        }

    def _parse_stat(self, parent: str, out: str) -> List[Dict]:
        items = []
        for line in out.split("\n"):
            parts = line.split("|", 3)
            if len(parts) != 4 or not parts[1].isdigit():
                continue
            name = posixpath.basename(parts[3].rstrip("/"))
            if name in (".", "..", ""):
                continue
            items.append(self._entry(parent, name, parts[0], int(parts[1]), int(parts[2]) if parts[2].isdigit() else 0))
        return items

    def _parse_ls(self, parent: str, out: str) -> List[Dict]:
        items = []
        for line in out.split("\n"):
            m = _LS_DATE.search(line)
            if not m or line.startswith("total"):
                continue
            head = line[:m.start()].split()
            if not head:
                continue
            perm, name = head[0], m.group(3)
            if perm.startswith("l") and " -> " in name:
                name = name.split(" -> ", 1)[0]
            name = posixpath.basename(name.rstrip("/")) or name
            if name in (".", ".."):
                continue
            size = int(head[-1]) if head[-1].isdigit() and not perm.startswith("d") else 0
            try:
                mtime = int(time.mktime(time.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")))
            except ValueError:
                mtime = 0
            items.append(self._entry(parent, name, perm, size, mtime))
        return items

    # ---------- read ----------
    async def roots(self, serial: str) -> Dict:
        checks = " ".join(q(rel) for _, rel in SHORTCUTS)
        cmd = ("for d in /storage/*; do [ -d \"$d\" ] && [ -r \"$d\" ] && echo \"V:$d\"; done 2>/dev/null; "
               f"for d in {checks}; do [ -d \"/sdcard/$d\" ] && echo \"S:$d\"; done; "
               "echo \"K:$(getprop ro.build.version.sdk)\"; "
               "stat -f -c 'F:%b %a %S' /sdcard/ 2>/dev/null")
        _, out = await self.shell(serial, cmd)
        volumes = [{"label": "Internal storage", "path": "/sdcard"}]
        found, shortcuts, space, sdk = set(), [], None, 0
        for line in out.split("\n"):
            tag, _, val = line.partition(":")
            if tag == "V" and posixpath.basename(val) not in INTERNAL_ALIASES:
                volumes.append({"label": f"External ({posixpath.basename(val)})", "path": val})
            elif tag == "S":
                label = next((lbl for lbl, rel in SHORTCUTS if rel == val), val)
                if label not in found:
                    found.add(label)
                    shortcuts.append({"label": label, "path": f"/sdcard/{val}"})
            elif tag == "K" and val.strip().isdigit():
                sdk = int(val.strip())
                self._sdk[serial] = sdk
            elif tag == "F":
                nums = val.split()
                if len(nums) == 3 and all(n.isdigit() for n in nums):
                    space = {"total": int(nums[0]) * int(nums[2]), "free": int(nums[1]) * int(nums[2])}
        return {"volumes": volumes, "shortcuts": shortcuts, "internal_space": space, "sdk": sdk}

    async def list_dir(self, serial: str, path: str) -> Dict:
        p = normalize(path)
        if await self._supports_stat(serial):
            prefix = "" if p == "/" else q(p)
            listing = f"stat -L -c '{STAT_FMT}' {prefix}/.* {prefix}/* 2>/dev/null"
        else:
            listing = f"ls -la {q(p if p == '/' else p + '/')} 2>/dev/null"
        cmd = (f"if [ ! -e {q(p)} ]; then echo __NOENT; elif [ ! -d {q(p)} ]; then echo __NOTDIR; "
               f"elif [ ! -r {q(p)} ] || [ ! -x {q(p)} ]; then echo __NOPERM; else {listing}; fi")
        _, out = await self.shell(serial, cmd, timeout=60)
        marker = out.lstrip("\n").split("\n", 1)[0]
        if marker == "__NOENT":
            raise StorageError(f"{p} does not exist", 404)
        if marker == "__NOTDIR":
            raise StorageError(f"{p} is not a folder", 409)
        if marker == "__NOPERM":
            raise StorageError(f"Android does not allow access to {p}", 403)
        if self._has_stat.get(serial):
            items = self._parse_stat(p, out)
            if not items:  # empty folder, or too many entries for one glob (ARG_MAX)
                _, out = await self.shell(serial, f"ls -la {q(p if p == '/' else p + '/')} 2>/dev/null", timeout=120)
                items = self._parse_ls(p, out)
        else:
            items = self._parse_ls(p, out)
        items.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
        return {"path": p, "parent": None if p == "/" else posixpath.dirname(p), "items": items}

    async def stat(self, serial: str, path: str) -> Dict:
        p = normalize(path)
        probe = (f"stat -L -c '{STAT_FMT}' {q(p)}" if await self._supports_stat(serial) else f"ls -lad {q(p)}")
        cmd = (f"if [ ! -e {q(p)} ]; then echo __NOENT; elif [ ! -r {q(p)} ]; then echo __NOPERM; "
               f"else [ -d {q(p)} ] && echo __DIR; {probe} 2>/dev/null; fi")
        _, out = await self.shell(serial, cmd)
        lines = out.split("\n")
        if "__NOENT" in lines:
            raise StorageError(f"{p} does not exist", 404)
        if "__NOPERM" in lines:
            raise StorageError(f"Android does not allow reading {p}", 403)
        parent = posixpath.dirname(p)
        rows = self._parse_stat(parent, out) if self._has_stat.get(serial) else self._parse_ls(parent, out)
        entry = rows[0] if rows else self._entry(parent, posixpath.basename(p), "-", 0, 0)
        entry.update(name=posixpath.basename(p) or "/", path=p, is_dir="__DIR" in lines)
        return entry

    async def stream_file(self, serial: str, path: str, limit: int, chunk: int = 256 * 1024) -> AsyncIterator[bytes]:
        """Stream a file via `exec-out cat` (binary-safe, no temp files)."""
        proc = await asyncio.create_subprocess_exec(
            self.adb, "-s", serial, "exec-out", f"cat {q(path)} 2>/dev/null",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        sent = 0
        try:
            while sent < limit:
                data = await proc.stdout.read(min(chunk, limit - sent))
                if not data:
                    break
                sent += len(data)
                yield data
        finally:
            if proc.returncode is None:
                _kill(proc)
            await proc.wait()

    async def read_range(self, serial: str, path: str, start: int, length: int) -> AsyncIterator[bytes]:
        """Byte range of a file. Offset reads use block-aligned `dd` (present on every Android version)."""
        if start == 0:
            async for chunk in self.stream_file(serial, path, length):
                yield chunk
            return
        bs = 64 * 1024
        first = start // bs
        count = (start + length + bs - 1) // bs - first
        proc = await asyncio.create_subprocess_exec(
            self.adb, "-s", serial, "exec-out", f"dd if={q(path)} bs={bs} skip={first} count={count} 2>/dev/null",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        drop, left = start - first * bs, length
        try:
            while left > 0:
                data = await proc.stdout.read(256 * 1024)
                if not data:
                    break
                if drop:
                    cut = min(drop, len(data))
                    data, drop = data[cut:], drop - cut
                    if not data:
                        continue
                data = data[:left]
                left -= len(data)
                yield data
        finally:
            if proc.returncode is None:
                _kill(proc)
            await proc.wait()

    async def read_all(self, serial: str, path: str, limit: int) -> bytes:
        out = bytearray()
        async for chunk in self.stream_file(serial, path, limit):
            out += chunk
        return bytes(out)

    async def pull_as_zip(self, serial: str, paths: List[str]) -> Tuple[Path, Path]:
        """Pull files/folders into a temp dir and zip them. Returns (zip_path, temp_dir)."""
        tmp = Path(tempfile.mkdtemp(prefix="pull_", dir=UPLOADS_DIR))
        try:
            stage = tmp / "stage"
            pulled = []
            for i, raw in enumerate(paths):
                p = normalize(raw)
                dest = stage / str(i)
                dest.mkdir(parents=True)
                async with self._slot(serial):
                    code, out, err = await self._exec(["-s", serial, "pull", p, str(dest)], None)
                self._raise_transport(err.decode("utf-8", "replace"))
                if any(dest.iterdir()):
                    pulled.append(dest)
                elif code != 0:
                    raise StorageError(f"Could not read {p}: {(err or out).decode('utf-8', 'replace').strip()}", 502)
            if not pulled:
                raise StorageError("Nothing to download", 404)
            zip_path = tmp / "bundle.zip"
            await asyncio.to_thread(self._zip_dirs, pulled, zip_path)
            shutil.rmtree(stage, ignore_errors=True)
            return zip_path, tmp
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    @staticmethod
    def _zip_dirs(sources: List[Path], zip_path: Path):
        used = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for src in sources:
                for top in sorted(src.iterdir()):
                    arc, n = top.name, 1
                    while arc in used:
                        arc, n = f"{top.stem} ({n}){top.suffix}", n + 1
                    used.add(arc)
                    if top.is_dir():
                        zf.write(top, arc)
                        for f in sorted(top.rglob("*")):
                            zf.write(f, f"{arc}/{f.relative_to(top).as_posix()}")
                    else:
                        zf.write(top, arc)

    # ---------- write ----------
    async def _run_checked(self, serial: str, cmd: str, timeout: float = 120.0) -> str:
        rc, out = await self.shell(serial, f"{cmd} 2>&1", timeout=timeout)
        if rc != 0:
            raise StorageError(out.strip() or "Operation failed on device", 500)
        return out

    async def _media_scan(self, serial: str, paths: List[str]):
        """Android <= 10 does not index files written over adb; ask MediaScanner (best effort)."""
        try:
            if not 0 < await self.sdk(serial) <= 29:
                return
            for p in paths[:50]:
                await self.shell(serial, "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                                         f"-d {q('file://' + quote(p))} >/dev/null 2>&1", timeout=15)
        except Exception:
            pass

    def media_scan_later(self, serial: str, paths: List[str]):
        asyncio.get_running_loop().create_task(self._media_scan(serial, paths))

    async def mkdir(self, serial: str, parent: str, name: str) -> str:
        p = posixpath.join(writable_dir(parent), valid_name(name))
        await self._run_checked(serial, f"[ ! -e {q(p)} ] || {{ echo 'Already exists'; false; }} && mkdir -p {q(p)}")
        return p

    async def delete(self, serial: str, paths: List[str]) -> List[str]:
        targets = [removable(p) for p in paths]
        if not targets:
            raise StorageError("Nothing selected")
        batch: List[str] = []
        for i, p in enumerate(targets):
            batch.append(q(p))
            if i == len(targets) - 1 or sum(len(b) + 1 for b in batch) + len(q(targets[i + 1])) > 3000:
                await self._run_checked(serial, "rm -rf " + " ".join(batch), timeout=600)
                batch = []
        self.media_scan_later(serial, targets)
        return targets

    async def rename(self, serial: str, path: str, new_name: str) -> str:
        src = removable(path)
        dst = posixpath.join(posixpath.dirname(src), valid_name(new_name))
        if dst != src:
            if dst.casefold() == src.casefold():  # shared storage is case-insensitive
                tmp = f"{src}.acc_rename_{int(time.time())}"
                await self._run_checked(serial, f"mv {q(src)} {q(tmp)} && mv {q(tmp)} {q(dst)}")
            else:
                await self._run_checked(serial, f"[ ! -e {q(dst)} ] || {{ echo 'A file with that name already exists'; false; }} "
                                                f"&& mv {q(src)} {q(dst)}")
            self.media_scan_later(serial, [src, dst])
        return dst

    async def transfer(self, serial: str, paths: List[str], dest_dir: str, copy: bool) -> List[Dict]:
        dest = writable_dir(dest_dir)
        srcs = [normalize(r) if copy else removable(r) for r in paths]
        real = await self._real_dirs(serial, [dest] + srcs)
        real_dest = (real[0] or dest).casefold()
        results = []
        for src, real_src in zip(srcs, real[1:]):
            dst = posixpath.join(dest, posixpath.basename(src))
            try:
                rs = (real_src or src).casefold()
                if dst == src or real_dest == rs or real_dest.startswith(rs + "/"):
                    raise StorageError("Cannot place a folder inside itself")
                guard = f"[ ! -e {q(dst)} ] || {{ echo 'Already exists in destination'; false; }}"
                op = (f"cp -r {q(src)} {q(dst)}" if copy else
                      f"if ! mv {q(src)} {q(dst)} 2>/dev/null; then if [ -e {q(dst)} ]; then "
                      f"echo 'Move stopped part-way; check the destination'; false; "
                      f"else cp -r {q(src)} {q(dst)} && rm -rf {q(src)}; fi; fi")
                await self._run_checked(serial, f"{guard} && {op}", timeout=3600)
                results.append({"path": src, "dest": dst, "ok": True})
            except StorageError as e:
                results.append({"path": src, "dest": dst, "ok": False, "error": e.message})
        self.media_scan_later(serial, [r["dest"] for r in results if r["ok"]])
        return results

    async def _real_dirs(self, serial: str, paths: List[str]) -> List[Optional[str]]:
        """Resolve symlinked aliases (/sdcard == /storage/emulated/0) for directories; None for files."""
        script = "; ".join(f"(cd -P {q(p)} 2>/dev/null && pwd) || echo" for p in paths)
        _, out = await self.shell(serial, script)
        lines = out.split("\n")
        return [(lines[i].strip() or None) if i < len(lines) else None for i in range(len(paths))]

    async def push(self, serial: str, local: Path, dest_dir: str, rel_name: str, overwrite: bool) -> str:
        parts = [safe_upload_name(x) for x in rel_name.replace("\\", "/").split("/") if x not in ("", ".", "..")]
        if not parts:
            raise StorageError("Missing file name")
        target_dir = posixpath.join(writable_dir(dest_dir), *parts[:-1])
        name = parts[-1]
        if overwrite:
            await self._run_checked(serial, f"mkdir -p {q(target_dir)}")
            remote = posixpath.join(target_dir, name)
        else:
            stem, ext = posixpath.splitext(name)
            out = await self._run_checked(
                serial,
                f"if mkdir -p {q(target_dir)}; then d={q(target_dir)}; n={q(stem)}; e={q(ext)}; c=\"$d/$n$e\"; i=1; "
                "while [ -e \"$c\" ]; do c=\"$d/$n ($i)$e\"; i=$((i+1)); done; echo \"$c\"; else false; fi")
            remote = out.rstrip("\n").split("\n")[-1]
        async with self._slot(serial):
            code, out, err = await self._exec(["-s", serial, "push", str(local), remote], None)
        self._raise_transport(err.decode("utf-8", "replace"))
        if code != 0:
            raise StorageError(f"Upload failed: {(err or out).decode('utf-8', 'replace').strip()}", 502)
        self.media_scan_later(serial, [remote])
        return remote
