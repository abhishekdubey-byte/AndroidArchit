"""Laptop-side thumbnails for ADB devices (remote devices render thumbnails on the phone)."""
import asyncio
import hashlib
import io
import os
import shutil
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

from ..config import STORAGE_DIR
from .storage_service import StorageError

THUMB_DIR = STORAGE_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
FFMPEG = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def cache_path(device_key: str, path: str, mtime: int, size: int) -> Path:
    h = hashlib.sha1(f"{device_key}|{path}|{mtime}|{size}".encode()).hexdigest()
    return THUMB_DIR / h[:2] / f"{h}.jpg"


def image_thumb(data: bytes, size: int) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("RGB", (size * 2, size * 2))  # fast JPEG downscale while decoding
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (24, 28, 40))
                bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
                im = bg
            out = io.BytesIO()
            im.save(out, "JPEG", quality=75, optimize=True)
            return out.getvalue()
    except Exception as e:
        raise StorageError(f"No preview available ({type(e).__name__})", 415)


async def video_thumb(source_url: str, size: int) -> bytes:
    """Grab one frame; ffmpeg reads the file through our own HTTP Range endpoint (seeks, no full copy)."""
    if not Path(FFMPEG).exists():
        raise StorageError("ffmpeg not installed on the laptop", 415)
    for seek in ("1", "0"):
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-v", "error", "-ss", seek, "-i", source_url, "-frames:v", "1",
            "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
            "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 40)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise StorageError("Video preview timed out", 504)
        if out:
            return out
    raise StorageError("No preview available", 415)


def read_cached(p: Path) -> Optional[bytes]:
    try:
        return p.read_bytes()
    except OSError:
        return None


def write_cached(p: Path, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
