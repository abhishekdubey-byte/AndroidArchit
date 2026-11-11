import ntpath
import os
import posixpath
import shutil
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

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
def _sdk_adb(base: str) -> str:
    """platform-tools/adb inside an SDK folder, in that folder's own path style (Windows or POSIX)."""
    if "\\" in base or ntpath.splitdrive(base)[0]:
        return ntpath.join(base, "platform-tools", "adb.exe")
    return posixpath.join(base, "platform-tools", "adb")


def _absolute(path: Optional[str]) -> bool:
    return bool(path) and (ntpath.isabs(path) or posixpath.isabs(path))


def resolve_adb_path(env: Mapping[str, str] = os.environ,
                     which: Callable[[str], Optional[str]] = shutil.which,
                     exists: Callable[[str], bool] = os.path.isfile,
                     windows: bool = os.name == "nt") -> str:
    """Find adb on Windows, Linux and macOS: ADB_PATH, then PATH, then the usual SDK folders."""
    exe = "adb.exe" if windows else "adb"  # never a .bat/.cmd wrapper (run through cmd.exe on Windows)
    explicit = env.get("ADB_PATH")
    if explicit:
        if not any(sep in explicit for sep in ("/", "\\")):   # a bare name such as "adb": look it up on PATH
            found = which(explicit)
            if _absolute(found):
                return found
        if not exists(explicit):
            print(f"[Startup] ADB_PATH={explicit} does not exist; using it anyway", file=sys.stderr)
        return explicit
    on_path = which(exe)
    if _absolute(on_path):  # Python on Windows also searches the current directory: ignore such hits
        return on_path
    candidates = [_sdk_adb(env[k]) for k in ("ANDROID_HOME", "ANDROID_SDK_ROOT") if env.get(k)]
    if env.get("LOCALAPPDATA"):
        candidates.append(_sdk_adb(ntpath.join(env["LOCALAPPDATA"], "Android", "Sdk")))
    if env.get("HOME"):
        candidates.append(_sdk_adb(posixpath.join(env["HOME"], "Android", "Sdk")))
        candidates.append(_sdk_adb(posixpath.join(env["HOME"], "Library", "Android", "sdk")))
    for path in candidates:
        if exists(path):
            return path
    return "/usr/bin/adb"


ADB_PATH = resolve_adb_path()

# Fast USB mirroring (hardware H.264 via the scrcpy server); ACC_FAST_MIRROR=0 falls back to snapshots
FAST_MIRROR = os.getenv("ACC_FAST_MIRROR", "1") != "0"
MIRROR_MAX_SIZE = int(os.getenv("ACC_MIRROR_MAX_SIZE", "1280"))
MIRROR_BITRATE = int(os.getenv("ACC_MIRROR_BITRATE", "8000000"))
MIRROR_FPS = int(os.getenv("ACC_MIRROR_FPS", "60"))


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
