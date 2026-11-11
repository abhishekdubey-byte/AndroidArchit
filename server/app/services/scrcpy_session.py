"""Live hardware-H.264 screen stream + instant input for a USB phone, via the scrcpy server (v2.7, Apache-2.0).

start(): push the server jar → `adb forward tcp:<port> localabstract:scrcpy_<scid>` → launch the server with
`adb shell` → connect the video socket (read the dummy byte) → connect the control socket → read codec meta.
packets() yields encoded packets; send() writes control messages; stop() undoes everything (idempotent).
"""
import asyncio
import collections
import hashlib
import os
import socket
import struct
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional, Tuple

from ..config import STORAGE_DIR
from .scrcpy_protocol import (DEVICE_JAR, SCRCPY_VERSION, new_scid, parse_codec_meta, parse_frame_header,
                              server_command, socket_name, sps_dimensions)

SCRCPY_SERVER_URL = f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/scrcpy-server-v{SCRCPY_VERSION}"
# Official checksum, published in upstream doc/build.md and install_release.sh at tag v2.7
SCRCPY_SERVER_SHA256 = "a23c5659f36c260f105c022d27bcb3eafffa26070e7baa9eda66d01377a1adba"
MAX_PACKET = 16 * 1024 * 1024


class MirrorUnavailable(Exception):
    """Fast mirroring cannot run on this phone right now (reason in the message)."""


@dataclass
class VideoPacket:
    pts_us: int
    is_config: bool
    is_key: bool
    data: bytes


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=30) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_server_jar(vendor_dir: Path = STORAGE_DIR / "vendor") -> Path:
    """The pinned scrcpy server, downloaded once and checksum-verified."""
    vendor_dir.mkdir(parents=True, exist_ok=True)
    jar = vendor_dir / f"scrcpy-server-v{SCRCPY_VERSION}"
    if jar.exists() and _sha256(jar) == SCRCPY_SERVER_SHA256:
        return jar
    part = jar.with_name(jar.name + ".part")
    try:
        _download(SCRCPY_SERVER_URL, part)
        if _sha256(part) != SCRCPY_SERVER_SHA256:
            raise MirrorUnavailable("downloaded scrcpy server failed its checksum")
        os.replace(part, jar)
        return jar
    except MirrorUnavailable:
        raise
    except Exception as e:
        raise MirrorUnavailable(f"could not download the scrcpy server ({e})") from e
    finally:
        if part.exists():
            part.unlink()


class ScrcpySession:
    CONNECT_TIMEOUT = 3.0  # seconds for the phone-side server to start listening

    def __init__(self, adb_path: str, serial: str, *, video: bool = True, max_size: int = 1280,
                 bit_rate: int = 8_000_000, max_fps: int = 60, jar_path: Optional[Path] = None):
        self.adb_path, self.serial, self.video = adb_path, serial, video
        self.max_size, self.bit_rate, self.max_fps = max_size, bit_rate, max_fps
        self.jar_path = jar_path
        self.scid = new_scid()
        self.port: Optional[int] = None
        self.width = self.height = 0
        self.frame_size: Tuple[int, int] = (0, 0)
        self._video: Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = None
        self._control: Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._log = collections.deque(maxlen=20)
        self._log_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._forwarded = False
        self._stopped = False

    # ---- seams (overridden in tests) ----
    async def _adb(self, *args: str) -> Tuple[int, str, str]:
        p = await asyncio.create_subprocess_exec(self.adb_path, "-s", self.serial, *args,
                                                 stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(), 20)
        except asyncio.TimeoutError:
            p.kill()
            return -1, "", "timed out"
        return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _push_jar(self, jar: Path) -> None:
        rc, _, err = await self._adb("push", str(jar), DEVICE_JAR)
        if rc != 0:
            raise MirrorUnavailable(f"could not copy the mirror server to the phone ({err.strip()[:120]})")

    async def _spawn_server(self, command: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.adb_path, "-s", self.serial, "shell", command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._log_task = asyncio.create_task(self._collect_log())

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    # ---- lifecycle ----
    async def _collect_log(self):
        try:
            while self._proc and self._proc.stdout:
                line = await self._proc.stdout.readline()
                if not line:
                    return
                self._log.append(line.decode(errors="replace").rstrip())
        except Exception:
            pass

    def _reason(self, default: str) -> str:
        errors = [l for l in self._log if "ERROR" in l or "Exception" in l]
        return (errors[-1] if errors else default)[:200]

    async def _connect_first(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CONNECT_TIMEOUT
        while True:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                try:
                    await asyncio.wait_for(reader.readexactly(1), max(0.05, deadline - loop.time()))  # dummy byte
                    return reader, writer
                except Exception:
                    writer.close()
                    raise
            except Exception:
                if loop.time() >= deadline:
                    raise MirrorUnavailable(self._reason("the mirror server did not start on the phone"))
                await asyncio.sleep(0.1)

    async def start(self) -> None:
        try:
            jar = self.jar_path or await asyncio.to_thread(ensure_server_jar)
            await self._push_jar(jar)
            self.port = self._free_port()
            rc, _, err = await self._adb("forward", f"tcp:{self.port}", f"localabstract:{socket_name(self.scid)}")
            if rc != 0:
                raise MirrorUnavailable(f"adb forward failed ({err.strip()[:120]})")
            self._forwarded = True
            await self._spawn_server(server_command(self.scid, video=self.video, max_size=self.max_size,
                                                    bit_rate=self.bit_rate, max_fps=self.max_fps))
            first = await self._connect_first()
            if self.video:
                self._video = first
                self._control = await asyncio.open_connection("127.0.0.1", self.port)
                meta = await asyncio.wait_for(self._video[0].readexactly(12), 10)
                _, self.width, self.height = parse_codec_meta(meta)
                self.frame_size = (self.width, self.height)
            else:
                self._control = first
        except BaseException:
            await self.stop()
            raise

    async def packets(self) -> AsyncIterator[VideoPacket]:
        if not self._video:
            return
        reader = self._video[0]
        while True:
            try:
                pts, is_config, is_key, size = parse_frame_header(await reader.readexactly(12))
                if size > MAX_PACKET:
                    return
                data = await reader.readexactly(size)
            except (asyncio.IncompleteReadError, ConnectionError, OSError, struct.error):
                return
            if is_config:
                try:
                    self.frame_size = sps_dimensions(data)  # changes on rotation
                except Exception:
                    pass
            yield VideoPacket(pts, is_config, is_key, data)

    async def send(self, msg: bytes) -> None:
        if not self._control:
            raise ConnectionError("control socket not open")
        async with self._send_lock:
            writer = self._control[1]
            writer.write(msg)
            await writer.drain()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for pair in (self._video, self._control):
            if pair:
                try:
                    pair[1].close()
                except Exception:
                    pass
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), 2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._log_task:
            self._log_task.cancel()
        if self._forwarded:
            self._forwarded = False
            try:
                await self._adb("forward", "--remove", f"tcp:{self.port}")
            except Exception:
                pass

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()
