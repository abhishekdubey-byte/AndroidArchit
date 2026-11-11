"""Record the phone's hardware-H.264 live camera to MP4 on the laptop without re-encoding (instant save).

Packets are written to `ffmpeg -f h264 -i pipe:0 -c copy` as they arrive (fragmented MP4, so the file is valid
while it grows); stopping only closes the pipe. Timestamps come from arrival time (the camera's frame rate varies:
30 fps, 15 when the phone is hot). Phone mic PCM is kept in a side file and muxed in on close (AAC; the video
is copied, never re-encoded). Recording starts at the first key frame; a later config (lens switch) is written
in-band.
"""
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import DOWNLOADS_DIR
from .cam_record import FFMPEG, clamp_sample_rate
from .media_packets import KIND_CONFIG, KIND_DELTA, KIND_KEY

_START = re.compile(b"\x00\x00\x01")


def split_access_units(stream: bytes) -> List[Tuple[int, bytes]]:
    """Split an Annex-B stream into (kind, bytes) units the way the phone's encoder emits them:
    SPS/PPS → one config unit; each IDR slice → key; each other slice → delta (SEI/AUD ride with the next frame)."""
    starts = [m.start() for m in _START.finditer(stream)]
    nals = []
    for i, s in enumerate(starts):
        begin = s - 1 if s > 0 and stream[s - 1] == 0 else s      # keep 4-byte start codes whole
        end = starts[i + 1] - (1 if starts[i + 1] > 0 and stream[starts[i + 1] - 1] == 0 else 0) if i + 1 < len(starts) else len(stream)
        nals.append(stream[begin:end])
    units: List[Tuple[int, bytes]] = []
    pending = b""
    for nal in nals:
        body = nal.lstrip(b"\x00")[1:]
        nal_type = body[0] & 0x1F if body else 0
        if nal_type in (7, 8):
            if units and units[-1][0] == KIND_CONFIG:
                units[-1] = (KIND_CONFIG, units[-1][1] + nal)
            else:
                units.append((KIND_CONFIG, nal))
        elif nal_type == 5:
            units.append((KIND_KEY, pending + nal)); pending = b""
        elif nal_type == 1:
            units.append((KIND_DELTA, pending + nal)); pending = b""
        else:
            pending += nal
    return units


class H264Recorder:
    QUEUE_MAX = 1200          # ~40 s of 30 fps video at worst; oldest deltas are never dropped (would corrupt)
    DRAIN_TIMEOUT = 60.0

    def __init__(self, device_name: str, folder: Optional[Path] = None, rotation: int = 0):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (device_name or "device"))[:40]
        self.path = Path(folder or DOWNLOADS_DIR) / f"live_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        self.video_path = self.path.with_suffix(".video.mp4")
        self.audio_path = self.path.with_suffix(".pcm")
        self.frames = 0
        self.rotation = int(rotation or 0) % 360   # camera sensors are mounted sideways
        self._config: Optional[bytes] = None
        self._proc: Optional[subprocess.Popen] = None
        self._audio = None
        self.audio_rate = 16000
        self.audio_bytes = 0
        self.t0: Optional[float] = None
        self._q: "queue.Queue" = queue.Queue(maxsize=self.QUEUE_MAX)
        self._accepting = True
        self._worker = threading.Thread(target=self._run, name="h264-record-writer", daemon=True)
        self._worker.start()

    # ---- called from the event loop: never blocks ----
    def submit_video(self, kind: int, pts_us: int, data: bytes) -> None:
        if self._accepting:
            try:
                self._q.put_nowait(("v", kind, data, time.monotonic()))
            except queue.Full:
                pass

    def submit_audio(self, pcm: bytes, rate: int = 16000) -> None:
        if self._accepting:
            try:
                self._q.put_nowait(("a", clamp_sample_rate(rate), pcm, time.monotonic()))
            except queue.Full:
                pass

    def submit(self, jpeg: bytes) -> None:
        """JPEG frames belong to the old preview path; ignored here."""

    # ---- writer thread ----
    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                if item[0] == "v":
                    self._write_video(item[1], item[2], item[3])
                else:
                    self._write_audio(item[2], item[1], item[3])
            except Exception:
                pass

    def _write_video(self, kind: int, data: bytes, at: float):
        if kind == KIND_CONFIG:
            self._config = data
            if self._proc:                          # lens switch: new SPS/PPS in-band
                self._pipe(data)
            return
        if self._proc is None:
            if kind != KIND_KEY or self._config is None or not FFMPEG:
                return                              # a recording can only start at a key frame
            rotate = ["-display_rotation", str(-self.rotation)] if self.rotation else []   # counter-clockwise degrees
            self._proc = subprocess.Popen(
                [FFMPEG, "-y", "-loglevel", "error", "-use_wallclock_as_timestamps", "1", *rotate, "-f", "h264", "-i", "pipe:0",
                 "-c:v", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(self.video_path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.t0 = at
            self._pipe(self._config)
        self._pipe(data)
        self.frames += 1

    def _pipe(self, data: bytes):
        try:
            self._proc.stdin.write(data)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _write_audio(self, pcm: bytes, rate: int, at: float):
        if self.t0 is None or not pcm:
            return                                  # audio starts with the video
        if self._audio is None:
            self.audio_rate = rate
            self._audio = open(self.audio_path, "wb")
        expect = int((at - self.t0) * self.audio_rate) * 2 - len(pcm)
        gap = expect - self.audio_bytes
        if -gap > self.audio_rate // 5 * 2:         # > 200 ms ahead: drop the burst
            return
        if gap > self.audio_rate // 5 * 2:          # > 200 ms behind: pad with silence to stay aligned
            gap -= gap % 2
            self._audio.write(b"\0" * gap)
            self.audio_bytes += gap
        self._audio.write(pcm)
        self.audio_bytes += len(pcm)

    # ---- close ----
    def _drain(self):
        if not self._accepting:
            return
        self._accepting = False
        try:
            self._q.put(None, timeout=self.DRAIN_TIMEOUT)
        except queue.Full:
            pass
        self._worker.join(timeout=self.DRAIN_TIMEOUT)
        if self._worker.is_alive() and self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._worker.join(timeout=2)

    def close(self) -> Optional[str]:
        self._drain()
        if self._audio:
            try:
                self._audio.close()
            except Exception:
                pass
        p = self._proc
        if p:
            try:
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
                        [FFMPEG, "-y", "-loglevel", "error", "-i", str(self.video_path), "-f", "s16le",
                         "-ar", str(self.audio_rate), "-ac", "1", "-i", str(self.audio_path), "-map", "0:v", "-map", "1:a",
                         "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", str(self.path)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                    muxed = r.returncode == 0 and self.path.exists() and self.path.stat().st_size > 0
                except Exception:
                    muxed = False
            if not muxed:
                self.video_path.replace(self.path)
        for f in (self.video_path, self.audio_path):
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
        return str(self.path) if ok and self.path.exists() else None
