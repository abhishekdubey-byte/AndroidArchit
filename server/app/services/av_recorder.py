"""Timestamp-exact live camera recordings: the phone's H.264 + AAC packets, on the phone's own clock, saved to MP4.

Packets are queued from the event loop (never blocks) and written by one thread as MPEG-TS with the phone's PTS
(`ts_mux`), so the file has the right duration whatever the frame rate did (30 fps, 15 when warm) and the sound stays
locked to the picture. On close the TS is copy-remuxed to MP4 (`ffmpeg -c copy`, well under a second) with the
sensor rotation written as display metadata.
"""
import queue
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import DOWNLOADS_DIR
from .cam_record import FFMPEG
from .media_packets import KIND_AUDIO, KIND_AUDIO_CONFIG, KIND_CONFIG, KIND_KEY
from .ts_mux import PTS_OFFSET, TsMuxer


class AvRecorder:
    QUEUE_MAX = 4000              # ~1 min of video + audio packets if the disk stalls
    DRAIN_TIMEOUT = 60.0
    PREROLL_US = 100_000          # audio up to 0.1 s before the first picture is kept

    def __init__(self, device_name: str, folder: Optional[Path] = None, rotation: int = 0,
                 audio_config: Optional[bytes] = None):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (device_name or "device"))[:40]
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"   # a quick restart never overwrites
        self.path = Path(folder or DOWNLOADS_DIR) / f"live_{safe}_{stamp}.mp4"
        self.ts_path = self.path.with_suffix(".ts")
        self.rotation = int(rotation or 0) % 360
        self.frames = 0
        self.audio_frames = 0
        self.first_pts_us: Optional[int] = None
        self.last_pts_us: Optional[int] = None
        self._config: Optional[bytes] = None
        self._asc: Optional[bytes] = audio_config
        self._mux = TsMuxer(audio=True)
        self._fh = None
        self._q: "queue.Queue" = queue.Queue(maxsize=self.QUEUE_MAX)
        self._accepting = True
        self._waiting_key = False
        self.close_seconds = 0.0
        self._worker = threading.Thread(target=self._run, name="av-record-writer", daemon=True)
        self._worker.start()

    # ---- called from the event loop: never blocks ----
    def submit_video(self, kind: int, pts_us: int, data: bytes) -> None:
        self._offer(("v", kind, pts_us, data), video=True)

    def submit_audio_packet(self, kind: int, pts_us: int, data: bytes) -> None:
        self._offer(("a", kind, pts_us, data), video=False)

    def submit_audio(self, pcm: bytes, rate: int = 16000) -> None:
        """Old apps' 16 kHz PCM has no timestamps; AutoRecorder sends those phones to H264Recorder instead."""

    def submit(self, jpeg: bytes) -> None:
        """JPEG frames belong to the old preview path; ignored here."""

    def _offer(self, item, video: bool) -> None:
        if not self._accepting:
            return
        try:
            self._q.put_nowait(item)
        except queue.Full:
            if video:
                self._waiting_key = True          # a dropped delta would corrupt the picture: resume at a key frame

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
                    self._write_audio(item[1], item[2], item[3])
            except Exception as e:                # a bad packet must not end the recording
                print(f"[AvRecorder] packet skipped: {e}")

    def _pts90(self, pts_us: int) -> int:
        # relative to the first picture; audio pre-roll (< 0.1 s) stays positive thanks to the 0.7 s offset
        return (PTS_OFFSET + (pts_us - self.first_pts_us) * 9 // 100) & ((1 << 33) - 1)

    def _write_video(self, kind: int, pts_us: int, data: bytes):
        if kind == KIND_CONFIG:
            self._config = data
            return
        key = kind == KIND_KEY
        if self._waiting_key:
            if not key:
                return
            self._waiting_key = False
        if self._fh is None:
            if not key or self._config is None:
                return                                # a recording can only start at a key frame
            self._fh = open(self.ts_path, "wb")
            self.first_pts_us = pts_us
        if pts_us < self.first_pts_us:
            return
        au = self._config + data if key else data      # every key frame self-contained (SPS/PPS in front)
        self._fh.write(self._mux.video(self._pts90(pts_us), au, key))
        self.frames += 1
        self.last_pts_us = max(self.last_pts_us or pts_us, pts_us)

    def _write_audio(self, kind: int, pts_us: int, data: bytes):
        if kind == KIND_AUDIO_CONFIG:
            self._asc = data
            return
        if kind != KIND_AUDIO or self._fh is None or self._asc is None or not data:
            return                                    # audio starts with the picture
        if pts_us < self.first_pts_us - self.PREROLL_US:
            return
        pts90 = max(0, self._pts90(pts_us))
        self._fh.write(self._mux.audio_frame(pts90, data, self._asc))
        self.audio_frames += 1

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

    def close(self) -> Optional[str]:
        started = time.monotonic()
        self._drain()
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        ok = False
        if self.frames > 0 and FFMPEG and self.ts_path.exists():
            rotate = ["-display_rotation:v:0", str(-self.rotation)] if self.rotation else []   # counter-clockwise
            maps = ["-map", "0:v:0"] + (["-map", "0:a:0?"] if self.audio_frames else [])
            try:
                r = subprocess.run(
                    [FFMPEG, "-y", "-loglevel", "error", "-probesize", "32M", "-analyzeduration", "30M", *rotate,
                     "-i", str(self.ts_path), *maps, "-c", "copy",
                     str(self.path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
                ok = r.returncode == 0 and self.path.exists() and self.path.stat().st_size > 0
                if not ok:
                    print(f"[AvRecorder] remux failed: {r.stderr.decode(errors='replace')[-300:]}")
            except Exception as e:
                print(f"[AvRecorder] remux failed: {e}")
        try:
            if self.ts_path.exists():
                if ok or self.frames == 0:
                    self.ts_path.unlink()
                else:                                  # remux failed: keep what was filmed (plays in VLC)
                    self.close_seconds = time.monotonic() - started
                    return str(self.ts_path)
        except Exception:
            pass
        self.close_seconds = time.monotonic() - started
        return str(self.path) if ok else None
