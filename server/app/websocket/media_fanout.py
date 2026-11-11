"""Fan-out of encoded video packets to dashboards, per stream, with per-viewer backpressure.

A dashboard receives a stream (a phone's screen '<device_id>', or its live camera '<device_id>#cam') only after
it subscribed to it (START_SCREEN_STREAM / START_CAMERA_STREAM). Each H.264 viewer has its own queue and sender
task, so one slow browser never delays the others. The codec config and the current picture group (key frame +
deltas since) are cached per stream: a viewer that subscribes late, asks to resync, or overflows its live queue
is restarted from config + that group at once — the replay is not limited by the live queue bound, because on a
static screen the next key frame may be minutes away. Viewers that can't decode H.264 get no binary packets.

Audio (AAC, kinds 4/5) travels in its own lane: the audio config is cached per stream and sent to a viewer before
any audio frame (late joiners too); audio frames never enter the video picture group, and a video overflow or
resync never clears queued audio. The audio lane is small and drops its oldest frame when full - after a network
stall the browser hears the present, not a burst of the past. Audio is sent ahead of queued video (it is tiny and
the ear notices gaps first).
"""
import asyncio
import collections
import time
from typing import Dict, List, Optional

from ..services.media_packets import KIND_AUDIO, KIND_AUDIO_CONFIG, KIND_CONFIG, KIND_DELTA, KIND_KEY, pack


AUDIO_QUEUE_MAX = 24             # ~0.5 s of AAC frames per viewer; older ones are dropped first


class _Client:
    def __init__(self, ws, h264: bool):
        self.ws, self.h264 = ws, h264
        self.vq: collections.deque = collections.deque()                       # video lane
        self.aq: collections.deque = collections.deque(maxlen=AUDIO_QUEUE_MAX)  # audio frames (drop-oldest)
        self.aconf: collections.deque = collections.deque()                     # audio configs (never dropped)
        self.wake = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.subs = set()            # stream ids this viewer started
        self.synced = set()          # streams whose queued packets it can decode from
        self.audio_synced = set()    # streams whose audio config it has been sent
        self.last_resync: Dict[str, float] = {}
        self.task: Optional[asyncio.Task] = None


class MediaFanout:
    GOP_MAX = 240                 # packets kept for late joiners; beyond that they wait for the next key frame
    GOP_MAX_BYTES = 32 * 1024 * 1024
    RESYNC_MIN_INTERVAL = 1.0     # seconds between client-requested resyncs of one stream

    def __init__(self, queue_max: int = 60):
        self.queue_max = queue_max    # live packets a viewer may fall behind before it is resynced
        self._clients: Dict[object, _Client] = {}
        self._config: Dict[str, bytes] = {}
        self._gop: Dict[str, Optional[List[bytes]]] = {}
        self._gop_bytes: Dict[str, int] = {}
        self._audio_config: Dict[str, bytes] = {}

    # ---- viewers ----
    def add(self, ws, wants_h264: bool) -> None:
        """Register (or update) a viewer's decoding ability; keeps its subscriptions."""
        c = self._clients.get(ws)
        if c is None:
            self._clients[ws] = _Client(ws, wants_h264)
            return
        if c.h264 != wants_h264:
            c.h264 = wants_h264
            self._clear(c)
            c.aq.clear()
            c.aconf.clear()
            c.synced.clear()
            c.audio_synced.clear()
            if wants_h264:
                for stream in c.subs:
                    self._resync(c, stream)
                    self._audio_sync(c, stream)

    def remove(self, ws) -> None:
        c = self._clients.pop(ws, None)
        if c and c.task:
            c.task.cancel()

    def subscribe(self, ws, stream_id: str) -> None:
        c = self._clients.get(ws)
        if c is None:                 # a browser that never declared H.264 (old page): JPEG
            c = self._clients[ws] = _Client(ws, False)
        c.subs.add(stream_id)
        if c.h264:
            c.synced.discard(stream_id)
            self._resync(c, stream_id)   # cached picture now, not at the phone's next change
            c.audio_synced.discard(stream_id)
            self._audio_sync(c, stream_id)

    def unsubscribe(self, ws, stream_id: str) -> None:
        c = self._clients.get(ws)
        if c is not None:
            c.subs.discard(stream_id)
            c.synced.discard(stream_id)
            c.audio_synced.discard(stream_id)

    def wants_h264(self, ws) -> bool:
        c = self._clients.get(ws)
        return bool(c and c.h264)

    def subscribers(self, stream_id: str) -> int:
        return sum(1 for c in self._clients.values() if stream_id in c.subs)

    def h264_viewers(self, stream_id: str) -> int:
        return sum(1 for c in self._clients.values() if stream_id in c.subs and c.h264)

    def jpeg_viewers(self, stream_id: str) -> int:
        return sum(1 for c in self._clients.values() if stream_id in c.subs and not c.h264)

    def jpeg_clients(self, stream_id: str) -> list:
        return [c.ws for c in self._clients.values() if stream_id in c.subs and not c.h264]

    def subscriber_sockets(self, stream_id: str) -> list:
        return [c.ws for c in self._clients.values() if stream_id in c.subs]

    def request_resync(self, ws, stream_id: str) -> bool:
        """The browser's decoder failed or fell far behind: restart it from config + current group.
        Returns False when throttled (at most once a second per stream)."""
        c = self._clients.get(ws)
        if c is None or not c.h264 or stream_id not in c.subs:
            return False
        now = time.monotonic()
        if now - c.last_resync.get(stream_id, -1e9) < self.RESYNC_MIN_INTERVAL:
            return False
        c.last_resync[stream_id] = now
        self._clear(c)
        c.synced.clear()
        for stream in c.subs:
            self._resync(c, stream)
        return True

    def forget_device(self, stream_id: str) -> None:
        """Stream ended: the next session starts with a new config."""
        self._config.pop(stream_id, None)
        self._gop.pop(stream_id, None)
        self._gop_bytes.pop(stream_id, None)
        self._audio_config.pop(stream_id, None)
        for c in self._clients.values():
            c.synced.discard(stream_id)
            c.audio_synced.discard(stream_id)

    # ---- packets ----
    async def publish(self, stream_id: str, pkt) -> None:
        kind = KIND_CONFIG if pkt.is_config else KIND_KEY if pkt.is_key else KIND_DELTA
        data = pack(kind, stream_id, pkt.pts_us, pkt.data)
        if kind == KIND_CONFIG:
            self._config[stream_id] = data
            self._gop[stream_id], self._gop_bytes[stream_id] = [], 0
        elif kind == KIND_KEY:
            self._gop[stream_id], self._gop_bytes[stream_id] = [data], len(data)
        else:
            gop = self._gop.get(stream_id)
            if gop:
                gop.append(data)
                self._gop_bytes[stream_id] = self._gop_bytes.get(stream_id, 0) + len(data)
                if len(gop) > self.GOP_MAX or self._gop_bytes[stream_id] > self.GOP_MAX_BYTES:
                    self._gop[stream_id] = None   # too long to replay: joiners wait for the next key frame
        for c in list(self._clients.values()):
            if not c.h264 or stream_id not in c.subs:
                continue
            if kind == KIND_CONFIG:
                c.synced.discard(stream_id)      # everyone restarts from the new config
            if stream_id in c.synced:
                if len(c.vq) >= self.queue_max:
                    self._overflow(c, stream_id)
                else:
                    self._put(c, data)
            else:
                self._resync(c, stream_id)

    async def publish_audio(self, stream_id: str, kind: int, pts_us: int, payload: bytes) -> None:
        """An AAC config (kind 4) or frame (kind 5) on its own lane (see the module docstring)."""
        if kind not in (KIND_AUDIO_CONFIG, KIND_AUDIO):
            raise ValueError(f"not an audio kind: {kind}")
        data = pack(kind, stream_id, pts_us, payload)
        if kind == KIND_AUDIO_CONFIG:
            self._audio_config[stream_id] = data
        for c in list(self._clients.values()):
            if not c.h264 or stream_id not in c.subs:
                continue
            if kind == KIND_AUDIO_CONFIG:
                self._put_lane(c, c.aconf, data)
                c.audio_synced.add(stream_id)
                continue
            if stream_id not in c.audio_synced:
                self._audio_sync(c, stream_id)
            if stream_id in c.audio_synced:
                self._put_lane(c, c.aq, data)

    def has_audio(self, stream_id: str) -> bool:
        return stream_id in self._audio_config

    def _audio_sync(self, c: _Client, stream_id: str) -> None:
        config = self._audio_config.get(stream_id)
        if config is not None:
            self._put_lane(c, c.aconf, config)
            c.audio_synced.add(stream_id)

    def _resync(self, c: _Client, stream_id: str) -> None:
        config, gop = self._config.get(stream_id), self._gop.get(stream_id)
        if config is None or gop is None:
            return
        for item in [config] + list(gop):        # replay is bounded by GOP_MAX, not by the live queue
            self._put(c, item)
        c.synced.add(stream_id)

    def _overflow(self, c: _Client, stream_id: str) -> None:
        """Viewer fell behind: drop what it has queued and restart it from config + current group."""
        self._clear(c)
        c.synced.clear()
        for stream in c.subs:
            self._resync(c, stream)

    @staticmethod
    def _clear(c: _Client) -> None:
        """Drop the queued video only (audio has its own lane)."""
        c.vq.clear()

    def _put(self, c: _Client, data: bytes) -> None:
        self._put_lane(c, c.vq, data)

    def _put_lane(self, c: _Client, lane: collections.deque, data: bytes) -> None:
        if c.task is None or c.task.done():
            c.task = asyncio.create_task(self._sender(c))
        lane.append(data)
        c.idle.clear()
        c.wake.set()

    async def _sender(self, c: _Client) -> None:
        while True:
            if c.aconf:
                data = c.aconf.popleft()
            elif c.aq:
                data = c.aq.popleft()
            elif c.vq:
                data = c.vq.popleft()
            else:
                c.idle.set()
                c.wake.clear()
                await c.wake.wait()
                continue
            try:
                await c.ws.send_bytes(data)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._clients.get(c.ws) is c:
                    del self._clients[c.ws]
                c.vq.clear()
                c.aq.clear()
                c.aconf.clear()
                c.idle.set()
                return

    async def flush(self) -> None:
        """Wait until every connected viewer has been sent everything queued (tests, shutdown)."""
        for c in list(self._clients.values()):
            if c.task and not c.task.done():
                await c.idle.wait()

    async def close(self) -> None:
        for c in list(self._clients.values()):
            if c.task:
                c.task.cancel()
        self._clients.clear()
