"""Fan-out of encoded video packets to dashboards, with per-client backpressure.

Every H.264 viewer has its own bounded queue and sender task, so one slow browser never delays the others.
The codec config and the current picture group (key frame + deltas since) are cached per phone: a viewer
that joins late, or falls behind and overflows its queue, is (re)started from config + that group, which
is always decodable. JPEG-only viewers (no WebCodecs) receive no binary packets.
"""
import asyncio
from typing import Dict, List, Optional

from ..services.media_packets import KIND_CONFIG, KIND_DELTA, KIND_KEY, pack


class _Client:
    def __init__(self, ws, h264: bool, queue_max: int):
        self.ws, self.h264 = ws, h264
        self.q: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self.synced = set()          # phones whose stream this viewer can decode from its queue
        self.task: Optional[asyncio.Task] = None


class MediaFanout:
    GOP_MAX = 240  # deltas kept for late joiners; beyond that they wait for the next key frame

    def __init__(self, queue_max: int = 60):
        self.queue_max = queue_max
        self._clients: Dict[object, _Client] = {}
        self._config: Dict[str, bytes] = {}
        self._gop: Dict[str, Optional[List[bytes]]] = {}

    # ---- viewers ----
    def add(self, ws, wants_h264: bool) -> None:
        old = self._clients.pop(ws, None)
        if old and old.task:
            old.task.cancel()
        self._clients[ws] = _Client(ws, wants_h264, self.queue_max)

    def remove(self, ws) -> None:
        c = self._clients.pop(ws, None)
        if c and c.task:
            c.task.cancel()

    def wants_h264(self, ws) -> bool:
        c = self._clients.get(ws)
        return bool(c and c.h264)

    def h264_viewers(self, device_id: str) -> int:
        return sum(1 for c in self._clients.values() if c.h264)

    def jpeg_viewers(self, device_id: str) -> int:
        return sum(1 for c in self._clients.values() if not c.h264)

    def forget_device(self, device_id: str) -> None:
        """Stream ended: the next session starts with a new config."""
        self._config.pop(device_id, None)
        self._gop.pop(device_id, None)
        for c in self._clients.values():
            c.synced.discard(device_id)

    # ---- packets ----
    async def publish(self, device_id: str, pkt) -> None:
        kind = KIND_CONFIG if pkt.is_config else KIND_KEY if pkt.is_key else KIND_DELTA
        data = pack(kind, device_id, pkt.pts_us, pkt.data)
        if kind == KIND_CONFIG:
            self._config[device_id] = data
            self._gop[device_id] = []
        elif kind == KIND_KEY:
            self._gop[device_id] = [data]
        else:
            gop = self._gop.get(device_id)
            if gop is not None and gop:
                gop.append(data)
                if len(gop) > self.GOP_MAX:
                    self._gop[device_id] = None      # too long to replay: joiners wait for the next key frame
        for c in list(self._clients.values()):
            if not c.h264:
                continue
            if kind == KIND_CONFIG:
                c.synced.discard(device_id)          # everyone restarts from the new config
            if device_id in c.synced:
                if not self._put(c, data):
                    self._overflow(c, device_id)
            else:
                self._resync(c, device_id)

    def _resync(self, c: _Client, device_id: str) -> None:
        config, gop = self._config.get(device_id), self._gop.get(device_id)
        if config is None or gop is None:
            return
        items = [config] + list(gop)
        if len(items) > self.queue_max - c.q.qsize():
            return                                   # would not fit: wait for a shorter group
        for item in items:
            self._put(c, item)
        c.synced.add(device_id)

    def _overflow(self, c: _Client, device_id: str) -> None:
        """Viewer fell behind: drop what it has queued and restart it from config + current group."""
        while not c.q.empty():
            c.q.get_nowait()
            c.q.task_done()
        c.synced.clear()
        self._resync(c, device_id)

    def _put(self, c: _Client, data: bytes) -> bool:
        if c.task is None or c.task.done():
            c.task = asyncio.create_task(self._sender(c))
        try:
            c.q.put_nowait(data)
            return True
        except asyncio.QueueFull:
            return False

    async def _sender(self, c: _Client) -> None:
        while True:
            data = await c.q.get()
            try:
                await c.ws.send_bytes(data)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._clients.get(c.ws) is c:
                    del self._clients[c.ws]
                c.q.task_done()
                while not c.q.empty():
                    c.q.get_nowait()
                    c.q.task_done()
                return
            c.q.task_done()

    async def flush(self) -> None:
        """Wait until every connected viewer has been sent everything queued (tests, shutdown)."""
        for c in list(self._clients.values()):
            if c.task and not c.task.done():
                await c.q.join()

    async def close(self) -> None:
        for c in list(self._clients.values()):
            if c.task:
                c.task.cancel()
        self._clients.clear()
