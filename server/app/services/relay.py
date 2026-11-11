"""Relay transport: storage access for phones on other networks.

The companion app keeps an outbound WebSocket to this server (works through
NAT / mobile data / any Wi-Fi), so the laptop never needs to reach the phone.

Wire format
  text   : {"type": "FS_REQ", "req_id", "op", "args"}  /  {"type": "FS_RES", "req_id", "ok", "data", "error", "status"}
  binary : 2-byte big-endian header length + UTF-8 JSON header (same fields) + raw payload
Reads are pull-based (offset/length), so seeking (HTTP Range) and back-pressure come for free.
"""
import asyncio
import collections
import gzip
import hashlib
import hmac
import json
import secrets
import posixpath
import shutil
import struct
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple

from fastapi import WebSocket

from ..config import STORAGE_DIR, UPLOADS_DIR
from .storage_service import StorageError, normalize, removable, safe_upload_name, valid_name, writable_dir

CHUNK = 512 * 1024
PIPELINE = 4


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header).encode()
    return struct.pack(">H", len(h)) + h + payload


def unpack(frame: bytes) -> Tuple[dict, bytes]:
    (n,) = struct.unpack(">H", frame[:2])
    return json.loads(frame[2:2 + n]), frame[2 + n:]


class RelayClient:
    def __init__(self, ws: WebSocket, relay_id: str):
        self.ws = ws
        self.relay_id = relay_id
        self.pending: Dict[str, asyncio.Future] = {}
        self.closed = False

    async def request(self, op: str, args: dict, payload: Optional[bytes] = None,
                      timeout: float = 60.0) -> Tuple[dict, bytes]:
        if self.closed:
            raise StorageError("Remote device disconnected", 503)
        req_id = uuid.uuid4().hex[:16]
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        msg = {"type": "FS_REQ", "req_id": req_id, "op": op, "args": args}
        try:
            if payload is None:
                await self.ws.send_text(json.dumps(msg))
            else:
                await self.ws.send_bytes(pack(msg, payload))
            header, data = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._cancel_remote(req_id)
            raise StorageError("Remote device did not answer in time (slow or lost network)", 504)
        except asyncio.CancelledError:
            self._cancel_remote(req_id)  # e.g. browser closed / video seeked: stop the phone's work too
            raise
        except StorageError:
            raise
        except Exception as e:  # socket closed while sending
            raise StorageError(f"Remote device unreachable: {e}", 503)
        finally:
            self.pending.pop(req_id, None)
        if not header.get("ok"):
            raise StorageError(header.get("error") or "Operation failed on device", int(header.get("status") or 500))
        return header.get("data") or {}, data

    def _cancel_remote(self, req_id: str):
        if self.closed:
            return

        async def send():
            try:
                await self.ws.send_text(json.dumps({"type": "FS_CANCEL", "req_id": req_id}))
            except Exception:
                pass
        asyncio.ensure_future(send())

    def resolve(self, header: dict, payload: bytes = b""):
        fut = self.pending.get(header.get("req_id", ""))
        if fut and not fut.done():
            fut.set_result((header, payload))

    def close(self):
        self.closed = True
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(StorageError("Remote device disconnected", 503))
        self.pending.clear()


BINDINGS_FILE = STORAGE_DIR / "relay_bindings.json"


class RelayRegistry:
    def __init__(self):
        self.clients: Dict[str, RelayClient] = {}
        try:
            raw = json.loads(BINDINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        # {relay_id: {"h": sha256(secret), "ok": confirmed}}; old format (plain hash) = confirmed
        self.bindings: Dict[str, dict] = {k: (v if isinstance(v, dict) else {"h": v, "ok": True}) for k, v in raw.items()}
        self._seen: Dict[str, float] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self.refused: Dict[str, float] = {}  # relay_id -> last time its connection was refused

    def _save_bindings(self):
        BINDINGS_FILE.write_text(json.dumps(self.bindings), encoding="utf-8")
        BINDINGS_FILE.chmod(0o600)

    def authorize(self, relay_id: str, secret: Optional[str], ws=None) -> Tuple[bool, Optional[str]]:
        """Each phone id is bound to a secret issued on its first connection.
        The binding stays provisional until the phone confirms it stored the secret (a socket can
        drop right after the secret is sent; the phone must not be locked out by that).
        Returns (allowed, new_secret_to_send_or_None)."""
        known = self.bindings.get(relay_id)
        matches = bool(known and secret) and hmac.compare_digest(known["h"], hashlib.sha256(secret.encode()).hexdigest())
        if matches:
            self.refused.pop(relay_id, None)
            if not known["ok"]:
                self.confirm(relay_id)
            return True, None
        live = self.clients.get(relay_id)
        if known is not None and live is not None and live.ws is not ws:
            self.refused[relay_id] = time.time()
            return False, None  # pending binding of a phone that is connected right now: no takeover
        if known is None or not known["ok"]:
            new = secrets.token_urlsafe(24)
            self.bindings[relay_id] = {"h": hashlib.sha256(new.encode()).hexdigest(), "ok": False}
            self._save_bindings()
            return True, new
        self.refused[relay_id] = time.time()
        return False, None

    def refused_phones(self, window: float = 86400) -> list:
        """Phones the laptop is turning away (usually: app reinstalled). Shown on the pair page with "Allow"."""
        now = time.time()
        return [{"relay_id": rid, "last": t} for rid, t in sorted(self.refused.items(), key=lambda kv: -kv[1])
                if now - t < window and rid in self.bindings and rid not in self.clients]

    def recently_refused(self, relay_id: str, window: float = 3600) -> bool:
        return time.time() - self.refused.get(relay_id, 0) < window

    def rebind(self, relay_id: str, secret: str) -> Optional[dict]:
        """Bind a phone id to a secret the laptop handed to it over USB (physical possession, e.g. after
        a reinstall wiped the phone's old secret). Returns the previous binding so a failed hand-over can undo it."""
        prev = self.bindings.get(relay_id)
        self.bindings[relay_id] = {"h": hashlib.sha256(secret.encode()).hexdigest(), "ok": True}
        self._save_bindings()
        return prev

    def restore(self, relay_id: str, prev: Optional[dict], secret: str):
        """Undo rebind() — unless the binding changed meanwhile (e.g. the user pressed Reset)."""
        cur = self.bindings.get(relay_id)
        if not cur or cur["h"] != hashlib.sha256(secret.encode()).hexdigest():
            return
        if prev is None:
            self.bindings.pop(relay_id, None)
        else:
            self.bindings[relay_id] = prev
        self._save_bindings()

    def confirm(self, relay_id: str, secret_hash: Optional[str] = None):
        """Finalise a pending binding — only for the exact secret the phone says it stored."""
        b = self.bindings.get(relay_id)
        if not b or b["ok"]:
            return
        if secret_hash is not None and not hmac.compare_digest(b["h"], str(secret_hash)):
            return
        b["ok"] = True
        self._save_bindings()

    async def wait_for(self, relay_id: Optional[str], timeout: float) -> Optional["RelayClient"]:
        """A phone that was connected moments ago is probably switching networks: wait briefly."""
        if not relay_id:
            return None
        if relay_id in self.clients:
            return self.clients[relay_id]
        if time.time() - self._seen.get(relay_id, 0) > 60:
            return None
        ev = self._events.setdefault(relay_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.clients.get(relay_id)

    def forget(self, relay_id: str):
        self.refused.pop(relay_id, None)
        if self.bindings.pop(relay_id, None) is not None:
            self._save_bindings()

    def attach(self, relay_id: str, ws: WebSocket) -> RelayClient:
        old = self.clients.get(relay_id)
        if old and old.ws is not ws:
            old.close()
        client = RelayClient(ws, relay_id)
        self.clients[relay_id] = client
        self._seen[relay_id] = time.time()
        ev = self._events.pop(relay_id, None)
        if ev:
            ev.set()
        return client

    def detach(self, ws: WebSocket) -> Optional[str]:
        for rid, c in list(self.clients.items()):
            if c.ws is ws:
                c.close()
                del self.clients[rid]
                self._seen[rid] = time.time()
                return rid
        return None

    def for_socket(self, ws: WebSocket) -> Optional[RelayClient]:
        return next((c for c in self.clients.values() if c.ws is ws), None)

    def get(self, relay_id: Optional[str]) -> Optional[RelayClient]:
        return self.clients.get(relay_id) if relay_id else None

    def on_text(self, ws: WebSocket, msg: dict) -> bool:
        """Route FS_RES text frames; returns True if consumed."""
        if msg.get("type") != "FS_RES":
            return False
        c = self.for_socket(ws)
        if c:
            c.resolve(msg)
        return True

    def on_binary(self, ws: WebSocket, frame: bytes):
        c = self.for_socket(ws)
        if c and len(frame) >= 2:
            header, payload = unpack(frame)
            c.resolve(header, payload)


class RelayStorage:
    """Same interface as StorageService, keyed by relay id instead of adb serial."""

    def __init__(self, registry: RelayRegistry):
        self.registry = registry
        self._thumb_slots: Dict[str, asyncio.Semaphore] = {}

    def _c(self, rid: str) -> RelayClient:
        c = self.registry.get(rid)
        if not c:
            raise StorageError("Remote device is not connected (check the companion app on the phone)", 503)
        return c

    async def roots(self, rid: str) -> Dict:
        data, _ = await self._c(rid).request("roots", {})
        return data

    async def list_dir(self, rid: str, path: str) -> Dict:
        data, _ = await self._c(rid).request("list", {"path": normalize(path)}, timeout=120)
        data["items"].sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
        return data

    async def stat(self, rid: str, path: str) -> Dict:
        data, _ = await self._c(rid).request("stat", {"path": normalize(path)})
        return data

    async def scan(self, rid: str, path: str, page: int, scan_id: Optional[str] = None) -> Dict:
        """Whole-tree listing done on the phone (Phone Backup): gzip JSON {"entries": [[rel, size, mtime]], "next",
        "scan_id"}. Later pages must name the scan_id of page 0 so all pages come from the same walk."""
        args = {"path": normalize(path), "page": page}
        if scan_id:
            args["scan_id"] = scan_id
        _, payload = await self._c(rid).request("scan", args, timeout=300)
        return json.loads(gzip.decompress(payload))

    async def read_range(self, rid: str, path: str, start: int, length: int) -> AsyncIterator[bytes]:
        c = self._c(rid)
        offset, end = start, start + length
        inflight: collections.deque = collections.deque()
        try:
            while offset < end or inflight:
                while offset < end and len(inflight) < PIPELINE:
                    n = min(CHUNK, end - offset)
                    inflight.append(asyncio.ensure_future(
                        c.request("read", {"path": path, "offset": offset, "length": n}, timeout=90)))
                    offset += n
                _, data = await inflight.popleft()
                if not data:
                    break
                yield data
        finally:
            for f in inflight:
                f.cancel()

    async def thumbnail(self, rid: str, path: str, size: int) -> bytes:
        async with self._thumb_slots.setdefault(rid, asyncio.Semaphore(4)):
            _, data = await self._c(rid).request("thumb", {"path": normalize(path), "size": size}, timeout=45)
        if not data:
            raise StorageError("No preview available", 415)
        return data

    async def pull_as_zip(self, rid: str, paths: List[str]) -> Tuple[Path, Path]:
        c = self._c(rid)
        data, _ = await c.request("walk", {"paths": [normalize(p) for p in paths]}, timeout=180)
        files = data.get("files", [])
        if not files and not data.get("dirs"):
            raise StorageError("Nothing to download", 404)
        tmp = Path(tempfile.mkdtemp(prefix="relay_", dir=UPLOADS_DIR))
        zip_path = tmp / "bundle.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for d in data.get("dirs", []):
                    zf.writestr(d.rstrip("/") + "/", b"")
                for f in files:
                    with zf.open(f["rel"], "w", force_zip64=True) as out:
                        async for chunk in self.read_range(rid, f["path"], 0, f["size"]):
                            await asyncio.to_thread(out.write, chunk)
            return zip_path, tmp
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    async def mkdir(self, rid: str, parent: str, name: str) -> str:
        p = posixpath.join(writable_dir(parent), valid_name(name))
        data, _ = await self._c(rid).request("mkdir", {"path": p})
        return data.get("path", p)

    async def delete(self, rid: str, paths: List[str]) -> List[str]:
        targets = [removable(p) for p in paths]
        if not targets:
            raise StorageError("Nothing selected")
        await self._c(rid).request("delete", {"paths": targets}, timeout=600)
        return targets

    async def rename(self, rid: str, path: str, new_name: str) -> str:
        src = removable(path)
        data, _ = await self._c(rid).request("rename", {"path": src, "new_name": valid_name(new_name)})
        return data["path"]

    async def transfer(self, rid: str, paths: List[str], dest_dir: str, copy: bool) -> List[Dict]:
        srcs = [normalize(p) if copy else removable(p) for p in paths]
        data, _ = await self._c(rid).request(
            "transfer", {"paths": srcs, "dest_dir": writable_dir(dest_dir), "copy": copy}, timeout=3600)
        return data["results"]

    async def push(self, rid: str, local: Path, dest_dir: str, rel_name: str, overwrite: bool) -> str:
        parts = [safe_upload_name(x) for x in rel_name.replace("\\", "/").split("/") if x not in ("", ".", "..")]
        if not parts:
            raise StorageError("Missing file name")
        c = self._c(rid)
        opened, _ = await c.request("write_open", {"dir": writable_dir(dest_dir), "rel": "/".join(parts),
                                                   "overwrite": overwrite})
        handle = opened["handle"]
        inflight: collections.deque = collections.deque()
        try:
            offset = 0
            with open(local, "rb") as fh:
                while True:
                    chunk = await asyncio.to_thread(fh.read, CHUNK)
                    if not chunk:
                        break
                    inflight.append(asyncio.ensure_future(
                        c.request("write", {"handle": handle, "offset": offset}, payload=chunk, timeout=120)))
                    offset += len(chunk)
                    if len(inflight) >= PIPELINE:
                        await inflight.popleft()
                while inflight:
                    await inflight.popleft()
            done, _ = await c.request("write_close", {"handle": handle, "size": offset}, timeout=120)
            return done["path"]
        except BaseException:
            for f in inflight:
                f.cancel()
            try:
                await c.request("write_abort", {"handle": handle}, timeout=10)
            except Exception:
                pass
            raise
