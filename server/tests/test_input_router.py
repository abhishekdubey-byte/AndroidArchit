import asyncio
import struct
import unittest
from types import SimpleNamespace

from app.services.adb_bridge import UnsupportedText
from app.services.input_router import InputRouter
from app.services.scrcpy_protocol import ACTION_DOWN, ACTION_MOVE, ACTION_UP


class FakeSession:
    def __init__(self, frame=(720, 1440), fail=False):
        self.frame_size, self.fail, self.sent, self.stopped = frame, fail, [], False

    async def send(self, msg):
        if self.fail:
            raise ConnectionError("socket closed")
        self.sent.append(msg)

    async def stop(self):
        self.stopped = True


class FakeControl:
    """The existing ADB/companion path (DeviceControl)."""

    def __init__(self, companion=False):
        self.companion, self.calls = companion, []

    async def inject_tap(self, serial, x, y):
        self.calls.append(("tap", x, y))

    async def inject_swipe(self, serial, x1, y1, x2, y2, d):
        self.calls.append(("swipe", x1, y1, x2, y2, d))

    async def inject_key(self, serial, key):
        self.calls.append(("key", key))

    async def inject_text(self, serial, text):
        if not text.isascii() and not self.companion:
            raise UnsupportedText("Non-English text and emoji need the companion app connected")
        self.calls.append(("text", text))


def device(transport="adb"):
    return SimpleNamespace(id="d", serial="s", name="Phone", transport=transport, status="online",
                           display=SimpleNamespace(width=1080, height=2160))


def router(session=None, companion=False, transport="adb"):
    dev = device(transport)
    dm = SimpleNamespace(get_device=lambda device_id: dev)
    sessions = {"d": session} if session else {}
    r = InputRouter(FakeControl(companion), dm, sessions, fast_input=False)
    return r


def touches(session):
    out = []
    for m in session.sent:
        if m[0] == 2:
            _, action, _, x, y, w, h, _, _, _ = struct.unpack(">BBQiiHHHii", m)
            out.append((action, x, y, w, h))
    return out


def run(coro):
    return asyncio.run(coro)


class TestInputRouter(unittest.TestCase):
    def test_tap_scaled_to_frame(self):
        s = FakeSession()
        r = router(s)
        self.assertEqual(run(r.tap("d", 540, 1080)), "scrcpy")
        self.assertEqual(touches(s), [(ACTION_DOWN, 360, 720, 720, 1440), (ACTION_UP, 360, 720, 720, 1440)])

    def test_tap_landscape_frame(self):
        s = FakeSession(frame=(1280, 640))
        r = router(s)
        run(r.tap("d", 1080, 0))       # right edge, top of the (rotated) picture
        self.assertEqual(touches(s)[0][1:3], (1280, 0))

    def test_swipe_interpolated(self):
        s = FakeSession()
        r = router(s)
        self.assertEqual(run(r.swipe("d", 540, 1800, 540, 600, 300)), "scrcpy")
        t = touches(s)
        self.assertEqual(t[0][0], ACTION_DOWN)
        self.assertEqual(t[-1][0], ACTION_UP)
        self.assertGreaterEqual(sum(1 for a in t if a[0] == ACTION_MOVE), 15)
        self.assertEqual(t[-1][1:3], (360, 400))
        ys = [a[2] for a in t]
        self.assertEqual(ys, sorted(ys, reverse=True))

    def test_key_home(self):
        s = FakeSession()
        r = router(s)
        self.assertEqual(run(r.key("d", "HOME")), "scrcpy")
        self.assertEqual([(m[0], m[1], int.from_bytes(m[2:6], "big")) for m in s.sent], [(0, 0, 3), (0, 1, 3)])

    def test_unknown_key_uses_adb(self):
        s = FakeSession()
        r = router(s)
        self.assertEqual(run(r.key("d", "CAMERA_SHUTTER_X")), "adb")
        self.assertEqual(r.control.calls, [("key", "CAMERA_SHUTTER_X")])

    def test_text_ascii_and_accents_via_scrcpy(self):
        s = FakeSession()
        r = router(s)
        self.assertEqual(run(r.text("d", "café ok")), "scrcpy")
        self.assertEqual(s.sent[0][5:].decode("utf-8"), "café ok")

    def test_devanagari_goes_to_companion(self):
        s = FakeSession()
        r = router(s, companion=True)
        self.assertEqual(run(r.text("d", "नमस्ते")), "companion")
        self.assertEqual(s.sent, [])
        self.assertEqual(r.control.calls, [("text", "नमस्ते")])

    def test_devanagari_without_companion_raises(self):
        r = router(FakeSession(), companion=False)
        with self.assertRaises(UnsupportedText):
            run(r.text("d", "😀"))

    def test_session_error_falls_back_to_adb(self):
        s = FakeSession(fail=True)
        r = router(s)
        self.assertEqual(run(r.tap("d", 10, 20)), "adb")
        self.assertEqual(r.control.calls, [("tap", 10, 20)])

    def test_no_session_uses_existing_path(self):
        r = router(None)
        self.assertEqual(run(r.tap("d", 10, 20)), "adb")
        self.assertEqual(run(r.key("d", "BACK")), "adb")

    def test_remote_phone_uses_existing_path(self):
        r = router(FakeSession(), transport="companion_ws")
        self.assertEqual(run(r.tap("d", 10, 20)), "companion")


if __name__ == "__main__":
    unittest.main()
