"""Record a live camera preview to an MP4 on the laptop.

Incoming JPEG frames are normalised to one size with Pillow (so a mid-recording lens switch or a
resolution change can't break the encoder) and piped to ffmpeg (image2pipe → H.264). Frames are
duplicated/dropped against the wall clock so the video runs in real time; the phone's mic audio
(PCM16 mono) is buffered to a side file, silence-padded to the same clock, and muxed in on close.
Saved under storage/downloads, same place as the other captures.
"""
import io
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from ..config import DOWNLOADS_DIR

FFMPEG = shutil.which("ffmpeg")
FPS = 8


class CamRecorder:
    def __init__(self, device_name: str, fps: int = FPS, folder: Optional[Path] = None):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (device_name or "device"))[:40]
        self.path = Path(folder or DOWNLOADS_DIR) / f"live_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        self.fps = fps
        self.size = None            # locked to the first frame; later frames are letterboxed to it
        self.frames = 0
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._closed = False
        self.t0 = time.monotonic()
        self.video_path = self.path.with_suffix(".video.mp4")
        self.audio_path = self.path.with_suffix(".pcm")
        self._audio = None
        self.audio_rate = 16000
        self.audio_bytes = 0

    def _start(self, w: int, h: int):
        w -= w % 2
        h -= h % 2  # H.264 needs even dimensions
        self.size = (max(2, w), max(2, h))
        self._proc = subprocess.Popen(
            [FFMPEG, "-y", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(self.fps), "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-vf", f"scale={self.size[0]}:{self.size[1]}", "-movflags", "+faststart", str(self.video_path)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def write(self, jpeg: bytes):
        if self._closed or not FFMPEG:
            return
        with self._lock:
            try:
                if self.size is None:
                    im = Image.open(io.BytesIO(jpeg))
                    self._start(im.width, im.height)
                    jpeg = self._fit(im)
                else:
                    im = Image.open(io.BytesIO(jpeg))
                    if (im.width, im.height) != self.size:
                        jpeg = self._fit(im)   # normalise odd/other-sized frames to the locked size
                if self._proc and self._proc.stdin:
                    # keep the video on the wall clock: repeat this frame up to "now" (or drop it if ahead)
                    due = int((time.monotonic() - self.t0) * self.fps) + 1
                    for _ in range(max(0, min(due - self.frames, self.fps * 5))):
                        self._proc.stdin.write(jpeg)
                        self.frames += 1
            except (BrokenPipeError, ValueError, OSError):
                pass
            except Exception:
                pass

    def write_audio(self, pcm: bytes, rate: int = 16000):
        if self._closed or not pcm:
            return
        with self._lock:
            try:
                if self._audio is None:
                    self.audio_rate = rate
                    self._audio = open(self.audio_path, "wb")
                # pad gaps (late start / dropped chunks) with silence so audio stays aligned with video
                expect = int((time.monotonic() - self.t0) * self.audio_rate) * 2 - len(pcm)
                gap = expect - self.audio_bytes
                if -gap > self.audio_rate // 5 * 2:     # > 200 ms ahead (burst after a stall) → drop it
                    return
                if gap > self.audio_rate // 5 * 2:      # > 200 ms behind
                    gap -= gap % 2
                    self._audio.write(b"\0" * gap)
                    self.audio_bytes += gap
                self._audio.write(pcm)
                self.audio_bytes += len(pcm)
            except Exception:
                pass

    def _fit(self, im: Image.Image) -> bytes:
        """Letterbox the frame into the locked size, preserving aspect (keeps lens-switch frames valid)."""
        tw, th = self.size
        im = im.convert("RGB")
        scale = min(tw / im.width, th / im.height)
        nw, nh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
        canvas = Image.new("RGB", (tw, th), (0, 0, 0))
        canvas.paste(im.resize((nw, nh)), ((tw - nw) // 2, (th - nh) // 2))
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=80)
        return buf.getvalue()

    def close(self) -> Optional[str]:
        with self._lock:
            if self._closed:
                return str(self.path) if self.path.exists() else None
            self._closed = True
            p = self._proc
            if self._audio:
                try:
                    self._audio.close()
                except Exception:
                    pass
        if p:
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout=15)
            except Exception:
                p.kill()
        ok = self.frames > 0 and self.video_path.exists() and self.video_path.stat().st_size > 0
        if ok:
            muxed = False
            if self.audio_bytes > 0:
                try:
                    r = subprocess.run(
                        [FFMPEG, "-y", "-i", str(self.video_path), "-f", "s16le", "-ar", str(self.audio_rate),
                         "-ac", "1", "-i", str(self.audio_path), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(self.path)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                    muxed = r.returncode == 0 and self.path.exists() and self.path.stat().st_size > 0
                except Exception:
                    muxed = False
            if not muxed:
                self.video_path.replace(self.path)   # no/failed audio → keep the silent video
        for f in (self.video_path, self.audio_path):
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
        if ok and self.path.exists():
            return str(self.path)
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass
        return None
