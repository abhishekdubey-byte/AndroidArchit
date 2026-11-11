import asyncio
import unittest
from types import SimpleNamespace

from app.services.media_packets import KIND_CONFIG, KIND_KEY, unpack
from app.services.scrcpy_session import MirrorUnavailable, VideoPacket
from app.websocket import hub as hub_mod
from app.websocket.hub import WebSocketHub


class FakeWS:
    def __init__(self):
        self.binary, self.json = [], []

    async def send_bytes(self, data):
        self.binary.append(unpack(data))

    async def send_json(self, msg):
        self.json.append(msg)


class FakeDM:
    def __init__(self):
        self.dev = SimpleNamespace(id="d", serial="s", name="Phone", transport="adb", status="online", relay_id="")

    def add_listener(self, fn):
        pass

    def get_device(self, device_id):
        return self.dev

    def get_all_devices(self):
        return [self.dev]


class FakeAdb:
    adb_path = "adb"

    def __init__(self):
        self.captures = 0

    async def capture_screen(self, serial, scale_width=540, quality=70):
        self.captures += 1
        return b"jpeg", "anBlZw=="


class FakeSession:
    instances = []
    fail = None

    def __init__(self, adb_path, serial, **kw):
        self.kw, self.stopped = kw, False
        self.frame_size = (720, 1440)
        self.width, self.height = 720, 1440
        FakeSession.instances.append(self)

    async def start(self):
        if FakeSession.fail:
            raise MirrorUnavailable(FakeSession.fail)

    async def packets(self):
        yield VideoPacket(0, True, False, b"cfg")
        yield VideoPacket(1, False, True, b"key")
        while not self.stopped:
            await asyncio.sleep(0.01)

    async def stop(self):
        self.stopped = True


class TestHubMirror(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig = hub_mod.ScrcpySession
        hub_mod.ScrcpySession = FakeSession
        FakeSession.instances, FakeSession.fail = [], None
        self.hub = WebSocketHub(FakeDM(), FakeAdb(), SimpleNamespace(add_listener=lambda fn: None))
        self.ws = FakeWS()
        self.hub.dashboard_clients.add(self.ws)

    def tearDown(self):
        hub_mod.ScrcpySession = self.orig

    async def start_stream(self):
        await self.hub._process_dashboard_message(self.ws, {"type": "START_SCREEN_STREAM", "device_id": "d"})

    async def test_h264_viewer_gets_packets_and_stop_ends_session(self):
        await self.hub._process_dashboard_message(self.ws, {"type": "CLIENT_CAPS", "payload": {"h264": True}})
        await self.start_stream()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(self.ws.binary) >= 2:
                break
        self.assertEqual([b[0] for b in self.ws.binary[:2]], [KIND_CONFIG, KIND_KEY])
        self.assertIn("d", self.hub.mirror_sessions)
        await self.hub._process_dashboard_message(self.ws, {"type": "STOP_SCREEN_STREAM", "device_id": "d"})
        await asyncio.wait_for(self.hub.streaming_tasks["d"], 3)
        self.assertTrue(FakeSession.instances[0].stopped)
        self.assertNotIn("d", self.hub.mirror_sessions)

    async def test_failure_falls_back_to_jpeg_with_notice(self):
        FakeSession.fail = "no encoder"
        await self.hub._process_dashboard_message(self.ws, {"type": "CLIENT_CAPS", "payload": {"h264": True}})
        await self.start_stream()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(m.get("type") == "SCREEN_FRAME" for m in self.ws.json):
                break
        notices = [m for m in self.ws.json if m.get("type") == "SCREEN_UNAVAILABLE"]
        self.assertTrue(any("Fast mirroring unavailable: no encoder" in m["payload"]["reason"] for m in notices))
        self.assertTrue(any(m.get("type") == "SCREEN_FRAME" for m in self.ws.json))
        self.hub.active_streaming_devices.discard("d")
        await asyncio.wait_for(self.hub.streaming_tasks["d"], 5)

    async def test_phone_not_ready_after_replug_is_retried_within_seconds(self):
        """Seen on the phone: right after plugging in, adb says "device not found" for a moment; that must not
        lock the dashboard into 60 s of slow snapshots."""
        FakeSession.fail = "could not copy the mirror server to the phone (adb: error: failed to get feature set: device 's' not found)"
        await self.hub._process_dashboard_message(self.ws, {"type": "CLIENT_CAPS", "payload": {"h264": True}})
        await self.start_stream()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if FakeSession.instances:
                break
        self.assertEqual(self.hub._fast_mirror_retry["d"], 3.0)
        notices = [m["payload"]["reason"] for m in self.ws.json if m.get("type") == "SCREEN_UNAVAILABLE"]
        self.assertTrue(any("starting the fast mirror" in n for n in notices), notices)
        FakeSession.fail = None                                  # adb ready now
        self.hub._fast_mirror_failed["d"] -= 3.1                 # 3 s later
        for _ in range(300):
            await asyncio.sleep(0.01)
            if "d" in self.hub.mirror_sessions:
                break
        self.assertIn("d", self.hub.mirror_sessions)
        self.assertNotIn("d", self.hub._fast_mirror_retry)
        self.hub.active_streaming_devices.discard("d")
        await self.hub._stop_unwatched("d")

    def test_retry_backoff(self):
        h = self.hub
        self.assertTrue(h._mirror_failed("d", "device offline"))
        self.assertTrue(h._mirror_failed("d", "device offline"))
        self.assertEqual(h._fast_mirror_retry["d"], 6.0)
        self.assertFalse(h._mirror_failed("d", "no encoder"))
        self.assertEqual(h._fast_mirror_retry["d"], 60.0)

    async def test_jpeg_only_dashboard_uses_snapshots(self):
        await self.start_stream()  # no CLIENT_CAPS: an old/remote browser
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(m.get("type") == "SCREEN_FRAME" for m in self.ws.json):
                break
        self.assertEqual(FakeSession.instances, [])
        self.assertTrue(any(m.get("type") == "SCREEN_FRAME" for m in self.ws.json))
        self.hub.active_streaming_devices.discard("d")
        await asyncio.wait_for(self.hub.streaming_tasks["d"], 5)

    async def test_last_dashboard_leaving_stops_session(self):
        await self.hub._process_dashboard_message(self.ws, {"type": "CLIENT_CAPS", "payload": {"h264": True}})
        await self.start_stream()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "d" in self.hub.mirror_sessions:
                break
        await self.hub.dashboard_left(self.ws)
        await asyncio.wait_for(self.hub.streaming_tasks["d"], 3)
        self.assertTrue(FakeSession.instances[0].stopped)


if __name__ == "__main__":
    unittest.main()
