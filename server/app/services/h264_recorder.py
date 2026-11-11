"""Record the phone's hardware-H.264 live camera to MP4 on the laptop without re-encoding (instant save).

Packets are written unbuffered to `ffmpeg -f h264 -i pipe:0 -c copy` as they arrive (fragmented MP4, valid while
it grows); timestamps are arrival times (the camera's frame rate varies: 30 fps, lower when the phone is hot).
Phone mic PCM is AAC-encoded live by a second ffmpeg, so stopping only closes the pipes and copy-muxes the two
streams. The sensor rotation is written as display metadata. `AutoRecorder` picks H.264 or the JPEG recorder from
the first frame it sees, and starts a new (upright) file when the lens — and so the rotation — changes.
"""
import collections
import queue
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import DOWNLOADS_DIR
from .cam_record import FFMPEG, CamRecorder, clamp_sample_rate
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
    QUEUE_MAX = 1200
    DRAIN_TIMEOUT = 60.0

    def __init__(self, device_name: str, folder: Optional[Path] = None, rotation: int = 0):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (device_name or "device"))[:40]
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"   # a quick restart never overwrites
        self.path = Path(folder or DOWNLOADS_DIR) / f"live_{safe}_{stamp}.mp4"
        self.video_path = self.path.with_suffix(".video.mp4")
        self.audio_path = self.path.with_suffix(".aac")
        self.rotation = int(rotation or 0) % 360
        self.frames = 0
        self._config: Optional[bytes] = None
        self._proc: Optional[subprocess.Popen] = None
        self._aproc: Optional[subprocess.Popen] = None
        self.audio_rate = 16000
        self.audio_bytes = 0
        self.t0: Optional[float] = None
        self._q: "queue.Queue" = queue.Queue(maxsize=self.QUEUE_MAX)
        self._accepting = True
        self._waiting_key = False
        self._worker = threading.Thread(target=self._run, name="h264-record-writer", daemon=True)
        self._worker.start()

    # ---- called from the event loop: never blocks ----
    def submit_video(self, kind: int, pts_us: int, data: bytes) -> None:
        if not self._accepting:
            return
        try:
            self._q.put_nowait(("v", kind, data, time.monotonic()))
        except queue.Full:
            self._waiting_key = True          # a dropped delta would corrupt the picture: resume at a key frame

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
            if self._proc:
                self._pipe(self._proc, data)
            return
        if self._waiting_key:
            if kind != KIND_KEY:
                return
            self._waiting_key = False
        if self._proc is None:
            if kind != KIND_KEY or self._config is None or not FFMPEG:
                return                              # a recording can only start at a key frame
            rotate = ["-display_rotation", str(-self.rotation)] if self.rotation else []   # counter-clockwise
            self._proc = subprocess.Popen(
                [FFMPEG, "-y", "-loglevel", "error", "-use_wallclock_as_timestamps", "1", *rotate, "-f", "h264",
                 "-i", "pipe:0", "-c:v", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                 str(self.video_path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                bufsize=0)                          # unbuffered: ffmpeg must stamp each frame when it arrives
            self.t0 = at
            self._pipe(self._proc, self._config)
        if self._pipe(self._proc, data):
            self.frames += 1

    @staticmethod
    def _pipe(proc, data: bytes) -> bool:
        try:
            proc.stdin.write(data)
            return True
        except (BrokenPipeError, OSError, ValueError, AttributeError):
            return False

    def _write_audio(self, pcm: bytes, rate: int, at: float):
        if self.t0 is None or not pcm or not FFMPEG:
            return                                  # audio starts with the video
        if self._aproc is None:
            self.audio_rate = rate
            self._aproc = subprocess.Popen(          # AAC encoded live: closing never re-encodes the whole clip
                [FFMPEG, "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                 "-c:a", "aac", "-b:a", "96k", "-f", "adts", str(self.audio_path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        expect = int((at - self.t0) * self.audio_rate) * 2 - len(pcm)
        gap = expect - self.audio_bytes
        if -gap > self.audio_rate // 5 * 2:         # > 200 ms ahead: drop the burst
            return
        if gap > self.audio_rate // 5 * 2:          # > 200 ms behind: pad with silence to stay aligned
            gap -= gap % 2
            self._pipe(self._aproc, b"\0" * gap)
            self.audio_bytes += gap
        if self._pipe(self._aproc, pcm):
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
        if self._worker.is_alive():
            for p in (self._proc, self._aproc):
                if p is not None:
                    try:
                        p.kill()
                    except Exception:
                        pass
            self._worker.join(timeout=2)

    @staticmethod
    def _finish(proc):
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()

    def close(self) -> Optional[str]:
        self._drain()
        self._finish(self._proc)
        self._finish(self._aproc)
        ok = self.frames > 0 and self.video_path.exists() and self.video_path.stat().st_size > 0
        if ok:
            muxed = False
            if self.audio_bytes > 0 and self.audio_path.exists() and self.audio_path.stat().st_size > 0:
                try:
                    r = subprocess.run(
                        [FFMPEG, "-y", "-loglevel", "error", "-i", str(self.video_path), "-i", str(self.audio_path),
                         "-map", "0:v", "-map", "1:a", "-c", "copy", str(self.path)],
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


class AutoRecorder:
    """The recorder a dashboard's Save button gets: picks H.264 (copy) or JPEG (re-encode) from the first frame,
    so pressing Save before the stream's first frame still records; a lens switch (new rotation) closes the current
    file and continues in a new, upright one.

    H.264 goes to `AvRecorder` (phone timestamps, AAC from app 1.9.29+); a phone that still sends 16 kHz PCM
    (`legacy_audio`) gets the older arrival-time `H264Recorder` so its sound is not lost."""

    def __init__(self, device_name: str, folder: Optional[Path] = None, config: Optional[bytes] = None,
                 rotation: int = 0, h264_factory=None, jpeg_factory=CamRecorder,
                 audio_config: Optional[bytes] = None, legacy_audio: bool = False, legacy_factory=H264Recorder):
        self.device_name, self.folder = device_name, folder
        self.config, self.rotation = config, int(rotation or 0) % 360
        self.audio_config, self.legacy_audio = audio_config, legacy_audio
        if h264_factory is None:
            from .av_recorder import AvRecorder
            h264_factory = AvRecorder
        self.h264_factory, self.jpeg_factory, self.legacy_factory = h264_factory, jpeg_factory, legacy_factory
        self.inner = None
        self.kind: Optional[str] = None
        self._video_frames = 0
        self._pending_audio = collections.deque(maxlen=300)   # audio that arrived before the first frame
        self._rolled: List[threading.Thread] = []
        self._paths: List[Optional[str]] = []
        self.closed = False

    def _start(self, kind: str):
        self.kind = kind
        if kind == "h264" and self.legacy_audio:
            self.inner = self.legacy_factory(self.device_name, folder=self.folder, rotation=self.rotation)
            if self.config is not None:
                self.inner.submit_video(KIND_CONFIG, 0, self.config)
        elif kind == "h264":
            self.inner = self.h264_factory(self.device_name, folder=self.folder, rotation=self.rotation,
                                           audio_config=self.audio_config)
            if self.config is not None:
                self.inner.submit_video(KIND_CONFIG, 0, self.config)
        else:
            self.inner = self.jpeg_factory(self.device_name, folder=self.folder)
        while self._pending_audio:
            pcm, rate = self._pending_audio.popleft()
            self.inner.submit_audio(pcm, rate)

    def submit(self, jpeg: bytes) -> None:
        if self.closed:
            return
        if self.inner is None:
            self._start("jpeg")
        if self.kind == "jpeg":
            self.inner.submit(jpeg)

    def submit_video(self, kind: int, pts_us: int, data: bytes) -> None:
        if self.closed:
            return
        if kind == KIND_CONFIG:
            self.config = data
            if self.kind == "h264":
                self.inner.submit_video(kind, pts_us, data)
            return
        if self.inner is None:
            self._start("h264")
        if self.kind == "h264":
            self.inner.submit_video(kind, pts_us, data)
            self._video_frames += 1

    def submit_audio_packet(self, kind: int, pts_us: int, data: bytes) -> None:
        """AAC from the phone (kind 4 config / 5 frame), timestamped on the video's clock."""
        if self.closed:
            return
        if kind == 4:
            self.audio_config = data
        if self.inner is not None and hasattr(self.inner, "submit_audio_packet"):
            self.inner.submit_audio_packet(kind, pts_us, data)

    def submit_audio(self, pcm: bytes, rate: int = 16000) -> None:
        if self.closed:
            return
        if self.inner is None:
            self._pending_audio.append((pcm, rate))
        else:
            self.inner.submit_audio(pcm, rate)

    def set_rotation(self, rotation: int) -> None:
        rotation = int(rotation or 0) % 360
        if rotation == self.rotation or self.closed:
            return
        self.rotation = rotation
        if self.kind == "h264" and self._video_frames:
            old = self.inner                         # a file has one orientation: finish it, start an upright one
            t = threading.Thread(target=lambda: self._paths.append(old.close()), daemon=True)
            t.start()
            self._rolled.append(t)
            self.inner, self.kind, self._video_frames = None, None, 0
        elif self.kind == "h264":
            self.inner.rotation = rotation

    def close_all(self) -> List[str]:
        """Close every file this recording produced; returns their paths in order."""
        self.closed = True
        for t in self._rolled:
            t.join(timeout=120)
        last = self.inner.close() if self.inner is not None else None
        return [p for p in self._paths + [last] if p]

    def close(self) -> Optional[str]:
        paths = self.close_all()
        self.paths = paths
        return paths[-1] if paths else None
