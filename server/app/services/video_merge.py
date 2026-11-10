"""Join the clips of a recording whose camera was switched mid-way (rear ↔ front) into one MP4.

Each clip carries its own rotation (front and rear sensors are mounted 180° apart), so the clips are
re-encoded: ffmpeg applies each clip's rotation, then every clip is scaled/padded to the first clip's
upright size and concatenated. A clip without sound gets silence so the audio track stays continuous.
"""
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from .cam_record import FFMPEG


def _probe(path: Path):
    """(width, height after rotation, has_audio, duration seconds) from ffmpeg's banner (no ffprobe needed)."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=30).stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (720, 1280)
    rot = re.search(r"rotate\s*:\s*(-?\d+)|rotation of (-?\d+)", out)
    deg = abs(int(next(g for g in rot.groups() if g))) if rot else 0
    if deg in (90, 270):
        w, h = h, w
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 1.0
    return w, h, "Audio:" in out, dur


def merge_parts(parts: List[Path], dest: Path) -> Optional[Path]:
    parts = [p for p in parts if p.exists() and p.stat().st_size > 0]
    if not parts or not FFMPEG:
        return None
    if len(parts) == 1:
        parts[0].replace(dest)
        return dest
    info = [_probe(p) for p in parts]
    W, H = info[0][0] - info[0][0] % 2, info[0][1] - info[0][1] % 2
    args, chains, labels = [FFMPEG, "-y"], [], ""
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    for i, (w, h, has_audio, dur) in enumerate(info):
        chains.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]")
        if has_audio:
            chains.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=mono[a{i}]")
        else:
            chains.append(f"anullsrc=r=44100:cl=mono,atrim=0:{dur:.3f}[a{i}]")
        labels += f"[v{i}][a{i}]"
    chains.append(f"{labels}concat=n={n}:v=1:a=1[v][a]")
    args += ["-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(dest)]
    try:
        r = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
        ok = r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
        if not ok:
            print(f"[video_merge] ffmpeg failed: {r.stderr[-400:]!r}")
    except Exception as e:
        print(f"[video_merge] {e}")
        ok = False
    if not ok:
        parts[0].replace(dest)   # better the first clip than nothing
    for p in parts:
        p.unlink(missing_ok=True)
    return dest
