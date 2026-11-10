import asyncio
import mimetypes
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Literal, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..config import SERVER_PORT, UPLOADS_DIR
from ..services.device_manager import DeviceManager
from ..services import thumbs
from ..services.relay import RelayRegistry, RelayStorage
from ..services.storage_service import StorageError, StorageService


ACTIVE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
                "text/javascript", "application/javascript"}


class MkdirRequest(BaseModel):
    parent: str
    name: str


class PathsRequest(BaseModel):
    paths: List[str]


class RenameRequest(BaseModel):
    path: str
    new_name: str


class TransferRequest(BaseModel):
    paths: List[str]
    dest_dir: str
    mode: Literal["move", "copy"] = "move"


def _disposition(name: str, inline: bool) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def create_storage_router(device_manager: DeviceManager, storage: StorageService,
                          relays: RelayRegistry, relay_storage: RelayStorage) -> APIRouter:
    router = APIRouter(prefix="/api/devices/{device_id}/fs", tags=["storage"])
    adb_thumb_slots = {}

    async def backend(device_id: str, via: Optional[str] = None) -> Tuple[object, str, object]:
        """USB/Wi-Fi ADB when available (fastest), otherwise the companion relay (any network)."""
        dev = device_manager.get_device(device_id)
        if not dev:
            raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
        adb_ok = dev.transport == "adb" and dev.status != "offline"
        relay_ok = relays.get(dev.relay_id) is not None
        if not relay_ok and (via == "relay" or not adb_ok):
            relay_ok = await relays.wait_for(dev.relay_id, 10) is not None  # phone switching networks
        if via == "relay" and relay_ok:
            return relay_storage, dev.relay_id, dev
        if adb_ok:
            return storage, dev.serial, dev
        if relay_ok:
            return relay_storage, dev.relay_id, dev
        raise StorageError("Device is offline: not on USB/ADB and its companion app is not connected", 503)

    @router.get("/roots")
    async def roots(device_id: str, via: Optional[str] = None):
        be, key, dev = await backend(device_id, via)
        data = await be.roots(key)
        data["transport"] = "relay" if be is relay_storage else "adb"
        return data

    @router.get("/list")
    async def list_dir(device_id: str, path: str = Query("/sdcard"), via: Optional[str] = None):
        be, key, _ = await backend(device_id, via)
        return await be.list_dir(key, path)

    @router.get("/download")
    async def download(device_id: str, request: Request, path: List[str] = Query(...), inline: bool = False,
                       via: Optional[str] = None):
        be, key, dev = await backend(device_id, via)
        if len(path) == 1:
            entry = await be.stat(key, path[0])
            if not entry["is_dir"]:
                size = entry["size"]
                media = mimetypes.guess_type(entry["name"])[0] or "application/octet-stream"
                headers = {"Content-Disposition": _disposition(entry["name"], inline),
                           "Accept-Ranges": "bytes",
                           "Cache-Control": "no-store",
                           "X-Content-Type-Options": "nosniff"}
                if inline:
                    # Files come from the phone (e.g. WhatsApp downloads): never let them script this origin.
                    if media in ACTIVE_TYPES or media.endswith("+xml"):
                        media = "text/plain; charset=utf-8"
                    if media != "application/pdf":
                        headers["Content-Security-Policy"] = "sandbox"
                start, end, status = 0, size - 1, 200
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", (request.headers.get("range") or "").strip())
                if m and (m.group(1) or m.group(2)) and size:
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:
                        start = max(0, size - int(m.group(2)))
                    if start > end:
                        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
                    status = 206
                    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                length = max(0, end - start + 1)
                headers["Content-Length"] = str(length)
                return StreamingResponse(be.read_range(key, entry["path"], start, length),
                                         status_code=status, media_type=media, headers=headers)
            zip_name = f"{entry['name'] or 'root'}.zip"
        else:
            zip_name = f"{dev.name.replace(' ', '_')}_files_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path, tmp = await be.pull_as_zip(key, path)
        return FileResponse(zip_path, filename=zip_name, media_type="application/zip",
                            background=BackgroundTask(shutil.rmtree, tmp, True))

    @router.get("/thumb")
    async def thumb(device_id: str, path: str, mtime: int = 0, size: int = 256, via: Optional[str] = None):
        size = max(64, min(size, 640))
        be, key, dev = await backend(device_id, via)
        cp = thumbs.cache_path(dev.id, path, mtime, size)
        data = thumbs.read_cached(cp) if mtime else None
        if data == b"":
            raise StorageError("No preview available", 415)
        if data is None:
            try:
                if be is relay_storage:
                    data = await relay_storage.thumbnail(key, path, size)
                else:
                    mime = mimetypes.guess_type(path)[0] or ""
                    async with adb_thumb_slots.setdefault(key, asyncio.Semaphore(3)):
                        if mime.startswith("image/"):
                            entry = await storage.stat(key, path)
                            if entry["size"] > thumbs.MAX_IMAGE_BYTES:
                                raise StorageError("Image too large to preview", 415)
                            raw = await storage.read_all(key, entry["path"], entry["size"])
                            data = await asyncio.to_thread(thumbs.image_thumb, raw, size)
                        elif mime.startswith("video/"):
                            src = (f"http://127.0.0.1:{SERVER_PORT}/api/devices/{quote(device_id, safe='')}/fs/download"
                                   f"?path={quote(path, safe='')}&inline=true")
                            data = await thumbs.video_thumb(src, size)
                        else:
                            raise StorageError("No preview for this file type", 415)
            except StorageError as e:
                if e.status == 415 and mtime:
                    thumbs.write_cached(cp, b"")
                raise
            if mtime:
                thumbs.write_cached(cp, data)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    @router.post("/upload")
    async def upload(device_id: str, dir: str = Form(...), file: UploadFile = File(...),
                     relpath: Optional[str] = Form(None), overwrite: bool = Form(False), via: Optional[str] = None):
        be, key, _ = await backend(device_id, via)
        fd, tmp_name = tempfile.mkstemp(prefix="up_", dir=UPLOADS_DIR)
        try:
            with os.fdopen(fd, "wb") as out:
                await asyncio.to_thread(shutil.copyfileobj, file.file, out, 1024 * 1024)
            remote = await be.push(key, Path(tmp_name), dir, relpath or file.filename or "upload.bin", overwrite)
            return {"status": "success", "path": remote}
        finally:
            await file.close()
            Path(tmp_name).unlink(missing_ok=True)

    @router.post("/mkdir")
    async def mkdir(device_id: str, req: MkdirRequest, via: Optional[str] = None):
        be, key, _ = await backend(device_id, via)
        return {"path": await be.mkdir(key, req.parent, req.name)}

    @router.post("/delete")
    async def delete(device_id: str, req: PathsRequest, via: Optional[str] = None):
        be, key, _ = await backend(device_id, via)
        return {"deleted": await be.delete(key, req.paths)}

    @router.post("/rename")
    async def rename(device_id: str, req: RenameRequest, via: Optional[str] = None):
        be, key, _ = await backend(device_id, via)
        return {"path": await be.rename(key, req.path, req.new_name)}

    @router.post("/transfer")
    async def transfer(device_id: str, req: TransferRequest, via: Optional[str] = None):
        be, key, _ = await backend(device_id, via)
        return {"results": await be.transfer(key, req.paths, req.dest_dir, req.mode == "copy")}

    return router
