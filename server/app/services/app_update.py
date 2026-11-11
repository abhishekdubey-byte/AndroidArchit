"""Serve the companion app's own APKs so installed phones can update themselves over the relay.

Each built APK is published into storage/app/<edition>.apk with a sibling <edition>.json
({"version_code", "version_name"}). The phone asks over its WebSocket for the latest version for
its edition (full/lite) and, if newer than what it runs, pulls the bytes in chunks over the same
relay — so it works on any network, exactly like every other remote feature.
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional

from ..config import STORAGE_DIR

APP_DIR = STORAGE_DIR / "app"
APP_DIR.mkdir(parents=True, exist_ok=True)


def _edition(edition: Optional[str]) -> str:
    return "lite" if edition == "lite" else "full"


def latest(edition: Optional[str]) -> Optional[Dict]:
    ed = _edition(edition)
    apk, meta = APP_DIR / f"{ed}.apk", APP_DIR / f"{ed}.json"
    if not apk.exists() or not meta.exists():
        return None
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
        return {"edition": ed, "path": apk, "version_code": int(m["version_code"]),
                "version_name": str(m["version_name"]), "size": apk.stat().st_size,
                "sha256": str(m.get("sha256", ""))}
    except (ValueError, KeyError, OSError):
        return None


def read_chunk(edition: Optional[str], offset: int, length: int) -> bytes:
    info = latest(edition)
    if not info or offset < 0:
        return b""
    with open(info["path"], "rb") as f:
        f.seek(offset)
        return f.read(max(0, length))


def publish(edition: str, apk_path: Path, version_code: int, version_name: str) -> Dict:
    """Copy a freshly built APK into the serving dir with its version metadata."""
    ed = _edition(edition)
    data = Path(apk_path).read_bytes()
    (APP_DIR / f"{ed}.apk").write_bytes(data)
    meta = {"version_code": int(version_code), "version_name": str(version_name),
            "sha256": hashlib.sha256(data).hexdigest()}
    (APP_DIR / f"{ed}.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"edition": ed, **meta, "size": len(data)}


_our_digest: Optional[str] = None


def _apksigner() -> Optional[str]:
    sdk = Path(os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT") or Path.home() / "android-sdk")
    tools = sorted(sdk.glob("build-tools/*/apksigner"), key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parent.name.split(".")])
    return str(tools[-1]) if tools else None


def signer_digest(apk: Path) -> Optional[str]:
    """SHA-256 of the APK's signing certificate (None if it can't be verified)."""
    tool = _apksigner()
    if not tool or not apk.exists():
        return None
    try:
        out = subprocess.run([tool, "verify", "--print-certs", str(apk)], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})", out)
    return m.group(1) if m else None


def our_signer_digest() -> Optional[str]:
    """Certificate of the release key the published companion builds are signed with."""
    global _our_digest
    if _our_digest is None:
        for ed in ("full", "lite"):
            _our_digest = signer_digest(APP_DIR / f"{ed}.apk")
            if _our_digest:
                break
    return _our_digest
