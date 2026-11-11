"""Phase 2 review fixes: per-viewer subscriptions, immediate resync, uncapped replay, resync throttle."""
import asyncio
import unittest
from types import SimpleNamespace

from app.services.media_packets import KIND_CONFIG, KIND_KEY, unpack
from app.services.scrcpy_session import MirrorUnavailable, VideoPacket
from app.websocket import hub as hub_mod
from app.websocket.hub import WebSocketHub
from app.websocket.media_fanout import MediaFanout


class WS:
    def __init__(self):
        self.binary, self.json = [], []

    async def send_bytes(self, data):
        self.binary.append(unpack(data))

    async def send_json(self, m):
        self.json.append(m)


def cfg():
    return VideoPacket(0, True, False, b"\x00\x00\x00\x01\x67c")


def key(p):
    return VideoPacket(p, False, True, b"K")


def delta(p):
    return VideoPacket(p, False, False, b"D")


class TestSubscriptions(unittest.IsolatedAsyncioTestCase):
    async def test_only_subscribers_receive_their_stream(self):
        f = MediaFanout()
        a, b = WS(), WS()
        f.add(a, True)
        f.add(b, True)
        f.subscribe(a, "phone1")
        f.subscribe(b, "phone2")
        for stream in ("phone1", "phone2"):
            await f.publish(stream, cfg())
            await f.publish(stream, key(1))
        await f.flush()
        self.assertEqual({p[1] for p in a.binary}, {"phone1"})
        self.assertEqual({p[1] for p in b.binary}, {"phone2"})
        self.assertEqual((f.subscribers("phone1"), f.h264_viewers("phone1")), (1, 1))
        await f.close()

    async def test_unsubscribed_page_is_not_a_jpeg_viewer(self):
        f = MediaFanout()
        page = WS()                          # e.g. the PowerBoosters page: connected, never started a mirror
        f.add(page, False)
        self.assertEqual(f.jpeg_viewers("phone1"), 0)
        f.subscribe(page, "phone1")
        self.assertEqual(f.jpeg_viewers("phone1"), 1)
        await f.close()

    async def test_subscribe_resyncs_immediately_on_a_static_screen(self):
        f = MediaFanout()
        first = WS()
        f.add(first, True)
        f.subscribe(first, "p")
        await f.publish("p", cfg())
        await f.publish("p", key(1))
        for i in range(2, 102):              # a long group: 100 deltas, then the screen goes static
            await f.publish("p", delta(i))
        late = WS()
        f.add(late, True)
        f.subscribe(late, "p")               # no new packet will ever arrive
        await f.flush()
        self.assertEqual([p[2] for p in late.binary], [0, 1] + list(range(2, 102)))
        await f.close()

    async def test_request_resync_is_immediate_and_throttled(self):
        f = MediaFanout()
        v = WS()
        f.add(v, True)
        f.subscribe(v, "p")
        await f.publish("p", cfg())
        await f.publish("p", key(1))
        await f.flush()
        v.binary.clear()
        self.assertTrue(f.request_resync(v, "p"))
        await f.flush()
        self.assertEqual([p[0] for p in v.binary], [KIND_CONFIG, KIND_KEY])
        self.assertFalse(f.request_resync(v, "p"))   # a looping decoder can't flood the server
        await f.close()


class FakeSession:
    instances = []
    frames = 2

    def __init__(self, adb_path, serial, **kw):
        self.stopped, self.frame_size, self.width, self.height = False, (720, 1440), 720, 1440
        FakeSession.instances.append(self)

    async def start(self):
        pass

    async def packets(self):
        yield VideoPacket(0, True, False, b"cfg")
        for i in range(FakeSession.frames):
            yield VideoPacket(i + 1, False, i == 0, b"f")
        while not self.stopped and FakeSession.frames:
            await asyncio.sleep(0.01)

    async def stop(self):
        self.stopped = True


class Adb:
    adb_path = "adb"

    def __init__(self):
        self.captures = 0

    async def capture_screen(self, serial, scale_width=540, quality=70):
        self.captures += 1
        return b"j", "ag=="


class TestHubSubscriptions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig = hub_mod.ScrcpySession
        hub_mod.ScrcpySession = FakeSession
        FakeSession.instances, FakeSession.frames = [], 2
        dev = SimpleNamespace(id="d", serial="s", name="P", transport="adb", status="online", relay_id="")
        dm = SimpleNamespace(add_listener=lambda fn: None, get_device=lambda i: dev, get_all_devices=lambda: [dev])
        self.adb = Adb()
        self.hub = WebSocketHub(dm, self.adb, SimpleNamespace(add_listener=lambda fn: None))

    def tearDown(self):
        hub_mod.ScrcpySession = self.orig

    async def join(self, h264=True, start=True):
        ws = WS()
        self.hub.dashboard_clients.add(ws)
        await self.hub._process_dashboard_message(ws, {"type": "CLIENT_CAPS", "payload": {"h264": h264}})
        if start:
            await self.hub._process_dashboard_message(ws, {"type": "START_SCREEN_STREAM", "device_id": "d"})
        return ws

    async def wait(self, cond, t=3.0):
        for _ in range(int(t / 0.02)):
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False

    async def test_one_tab_stopping_keeps_the_other_mirroring(self):
        a = await self.join()
        b = await self.join()
        self.assertTrue(await self.wait(lambda: "d" in self.hub.mirror_sessions))
        await self.hub._process_dashboard_message(a, {"type": "STOP_SCREEN_STREAM", "device_id": "d"})
        await asyncio.sleep(0.1)
        self.assertIn("d", self.hub.mirror_sessions)
        self.assertFalse(FakeSession.instances[0].stopped)
        await self.hub._process_dashboard_message(b, {"type": "STOP_SCREEN_STREAM", "device_id": "d"})
        self.assertTrue(await self.wait(lambda: FakeSession.instances[0].stopped))

    async def test_page_that_never_started_a_mirror_triggers_no_snapshots(self):
        await self.join(h264=True)
        await self.join(h264=False, start=False)          # PowerBoosters / Files page
        self.assertTrue(await self.wait(lambda: "d" in self.hub.mirror_sessions))
        await asyncio.sleep(1.5)
        self.assertEqual(self.adb.captures, 0)
        self.hub.active_streaming_devices.discard("d")
        for s in FakeSession.instances:
            await s.stop()

    async def test_stream_that_dies_without_frames_falls_back_with_notice(self):
        FakeSession.frames = 0                             # encoder died right after the header
        ws = await self.join()
        self.assertTrue(await self.wait(lambda: any(m.get("type") == "SCREEN_FRAME" for m in ws.json), 4))
        self.assertTrue(any(m.get("type") == "SCREEN_UNAVAILABLE" and "Fast mirroring" in m["payload"]["reason"]
                            for m in ws.json))
        self.assertIn("d", self.hub._fast_mirror_failed)
        self.hub.active_streaming_devices.discard("d")

    async def test_reconnect_clears_fast_mirror_failure(self):
        self.hub._fast_mirror_failed["d"] = 1e18
        await self.hub._handle_device_event("DEVICE_CONNECTED", {"id": "d"})
        self.assertNotIn("d", self.hub._fast_mirror_failed)


if __name__ == "__main__":
    unittest.main()
