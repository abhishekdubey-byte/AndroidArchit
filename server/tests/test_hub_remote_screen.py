"""Phase 5: the remote screen as H.264 from the companion app (MediaProjection) for phones not on USB."""
import asyncio
import json
import struct
import unittest
from types import SimpleNamespace

from app.services.media_packets import KIND_CONFIG, KIND_KEY, unpack
from app.websocket import hub as hub_mod
from app.websocket.hub import WebSocketHub
from tests.test_hub_mirror import FakeSession


class WS:
    def __init__(self):
        self.json, self.binary = [], []

    async def send_json(self, m):
        self.json.append(m)

    async def send_bytes(self, b):
        self.binary.append(unpack(b))


class FakeAdb:
    adb_path = "adb"

    def __init__(self):
        self.captures = 0

    async def capture_screen(self, serial, scale_width=540, quality=70):
        self.captures += 1
        return b"jpeg", "anBlZw=="


def pkt(kind, pts, data=b"x"):
    h = json.dumps({"type": "SCREEN_PKT", "k": kind, "pts": pts}).encode()
    return struct.pack(">H", len(h)) + h + data


class TestRemoteScreen(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dev = SimpleNamespace(id="ph", serial="rid", name="P", transport="companion_ws", status="online",
                                   relay_id="rid")
        dm = SimpleNamespace(add_listener=lambda fn: None, get_device=lambda i: self.dev,
                             get_all_devices=lambda: [self.dev])
        self.adb = FakeAdb()
        self.hub = WebSocketHub(dm, self.adb, SimpleNamespace(add_listener=lambda fn: None))
        self.phone = WS()
        self.hub.relays = SimpleNamespace(
            for_socket=lambda ws: SimpleNamespace(relay_id="rid") if ws is self.phone else None,
            get=lambda rid: self.phone)
        self.hub._get_companion_socket = lambda d: self.phone
        self.hub._on_usb = lambda d: d.transport == "adb"
        self.hub._phone_media["rid"] = {"aac", "screen_h264"}
        self.orig_session = hub_mod.ScrcpySession
        hub_mod.ScrcpySession = FakeSession
        FakeSession.instances, FakeSession.fail = [], None

    def tearDown(self):
        hub_mod.ScrcpySession = self.orig_session

    async def viewer(self, h264=True):
        ws = WS()
        self.hub.dashboard_clients.add(ws)
        await self.hub._process_dashboard_message(ws, {"type": "CLIENT_CAPS", "payload": {"h264": h264}})
        await self.hub._process_dashboard_message(ws, {"type": "START_SCREEN_STREAM", "device_id": "ph"})
        return ws

    async def until(self, cond, timeout=3.0):
        end = asyncio.get_event_loop().time() + timeout
        while not cond():
            if asyncio.get_event_loop().time() > end:
                raise AssertionError("condition not reached")
            await asyncio.sleep(0.01)

    def sent(self, t):
        return [m for m in self.phone.json if m["type"] == t]

    async def stop(self, ws):
        await self.hub._process_dashboard_message(ws, {"type": "STOP_SCREEN_STREAM", "device_id": "ph"})
        await asyncio.sleep(1.2)

    async def test_remote_phone_streams_h264_on_its_mirror_stream(self):
        ws = await self.viewer()
        await self.until(lambda: self.sent("START_SCREEN_PKT"))
        await self.hub.handle_device_binary(self.phone, pkt(1, 0, b"cfg"))
        await self.hub.handle_device_binary(self.phone, pkt(2, 10, b"key"))
        await self.hub.fanout.flush()
        self.assertEqual([(k, s) for k, s, _, _ in ws.binary], [(KIND_CONFIG, "ph"), (KIND_KEY, "ph")])
        late = await self.viewer()                         # a second dashboard: no second consent prompt
        await self.hub.fanout.flush()
        self.assertEqual(len(self.sent("START_SCREEN_PKT")), 1)
        self.assertEqual([k for k, *_ in late.binary], [KIND_CONFIG, KIND_KEY])
        self.assertEqual(self.adb.captures, 0, "no accessibility screenshots while H.264 flows")
        await self.stop(ws)
        await self.stop(late)
        self.assertEqual(len(self.sent("STOP_SCREEN_PKT")), 1)

    async def test_consent_refused_falls_back_to_screenshots_with_notice(self):
        ws = await self.viewer()
        await self.until(lambda: self.sent("START_SCREEN_PKT"))
        await self.hub._process_dashboard_message(self.phone, {"type": "SCREEN_STATE", "device_id": "rid", "payload": {
            "active": False, "error": "Screen sharing was not allowed on the phone"}})
        await self.until(lambda: self.adb.captures > 0)
        notices = [m["payload"]["reason"] for m in ws.json if m["type"] == "SCREEN_UNAVAILABLE"]
        self.assertTrue(any("not allowed" in n for n in notices), notices)
        self.assertEqual(len(self.sent("START_SCREEN_PKT")), 1, "must not nag the phone again right away")
        await self.stop(ws)

    async def test_phone_plugged_in_hands_over_to_usb_mirror(self):
        ws = await self.viewer()
        await self.until(lambda: self.sent("START_SCREEN_PKT"))
        await self.hub.handle_device_binary(self.phone, pkt(2, 10, b"key"))
        self.dev.transport = "adb"                           # USB cable plugged in
        await self.until(lambda: self.sent("STOP_SCREEN_PKT"))
        await self.until(lambda: FakeSession.instances)       # scrcpy took over
        self.assertNotIn("ph", self.hub._remote_screen)
        await self.stop(ws)

    async def test_old_app_keeps_accessibility_screenshots(self):
        self.hub._phone_media["rid"] = set()
        ws = await self.viewer()
        await self.until(lambda: self.adb.captures > 0)
        self.assertEqual(self.sent("START_SCREEN_PKT"), [])
        await self.stop(ws)

    async def test_unrequested_screen_packets_are_dropped(self):
        ws = WS()
        self.hub.dashboard_clients.add(ws)
        await self.hub._process_dashboard_message(ws, {"type": "CLIENT_CAPS", "payload": {"h264": True}})
        self.hub.fanout.subscribe(ws, "ph")
        self.assertTrue(await self.hub.handle_device_binary(self.phone, pkt(2, 10, b"key")))
        await self.hub.fanout.flush()
        self.assertEqual(ws.binary, [])


if __name__ == "__main__":
    unittest.main()
