import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
SCREENSHOT_DIR = STORAGE_DIR / "screenshots"
DOWNLOADS_DIR = STORAGE_DIR / "downloads"
UPLOADS_DIR = STORAGE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"

for directory in [STORAGE_DIR, SCREENSHOT_DIR, DOWNLOADS_DIR, UPLOADS_DIR, STATIC_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

SERVER_HOST = os.getenv("ACC_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("ACC_PORT", "8000"))
DEBUG = os.getenv("ACC_DEBUG", "True").lower() == "true"
ADB_PATH = os.getenv("ADB_PATH", "/usr/bin/adb")


# ---- Per-phone capture folders: storage/downloads/<Brand_Model_ID>/ ----
# Each phone gets its own folder (created when it connects) so captures from different phones never mix.
import re as _re
_DEVICE_DIRS: dict = {}   # device id / serial / relay id -> folder name


def _safe_dir_name(text: str) -> str:
    name = _re.sub(r"[^A-Za-z0-9]+", "_", str(text or "")).strip("_")
    return name[:60] or "Unknown_phone"


def register_device_dir(dev) -> Path:
    """Create (once) and remember the capture folder of a connected phone."""
    tail = (str(getattr(dev, "serial", "") or getattr(dev, "id", "")))[-6:]
    label = f"{(getattr(dev, 'brand', '') or '').title()} {getattr(dev, 'model', '') or ''}".strip() or "Phone"
    folder = _safe_dir_name(f"{label} {tail}")
    for key in (getattr(dev, "id", None), getattr(dev, "serial", None), getattr(dev, "relay_id", None)):
        if key:
            _DEVICE_DIRS.setdefault(str(key), folder)
    path = DOWNLOADS_DIR / _DEVICE_DIRS.get(str(getattr(dev, "id", "")), folder)
    path.mkdir(parents=True, exist_ok=True)
    return path


def device_capture_dir(key) -> Path:
    """Folder for captures of the phone known by `key` (device id, serial, relay id or a Device)."""
    if key is not None and not isinstance(key, str):
        return register_device_dir(key)
    folder = _DEVICE_DIRS.get(str(key or "")) or _safe_dir_name(key or "Unknown_phone")
    path = DOWNLOADS_DIR / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_url(path) -> str:
    """Public URL of a file saved under storage/downloads (works for per-phone sub-folders)."""
    rel = Path(path).resolve().relative_to(DOWNLOADS_DIR.resolve()).as_posix()
    return "/storage/downloads/" + "/".join(__import__("urllib.parse").parse.quote(p) for p in rel.split("/"))
