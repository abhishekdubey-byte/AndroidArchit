"""Phase 2 review fixes for InputRouter (control-only sessions, gesture timing, clamping, pointer/scroll)."""
import asyncio
import struct
import time
import unittest
from types import SimpleNamespace

from app.services.input_router import InputRouter
from app.services.scrcpy_protocol import ACTION_DOWN, ACTION_MOVE, ACTION_UP, scroll_event, scrcpy_can_type


class Session:
    def __init__(self, frame=(720, 1440), fail_after=None):
        self.frame_size, self.sent, self.fail_after, self.stopped = frame, [], fail_after, False

    async def start(self):
        pass

    async def send(self, msg):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionError("phone side closed")
        self.sent.append(msg)

    async def stop(self):
        self.stopped = True


class Control:
    def __init__(self):
        self.calls = []

    async def inject_tap(self, serial, x, y):
        self.calls.append(("tap", x, y))

    async def inject_swipe(self, serial, x1, y1, x2, y2, d):
        self.calls.append(("swipe", x1, y1, x2, y2, d))

    async def inject_key(self, serial, key):
        self.calls.append(("key", key))

    async def inject_text(self, serial, text):
        self.calls.append(("text", text))


def device():
    return SimpleNamespace(id="d", serial="s", name="P", transport="adb", status="online",
                           display=SimpleNamespace(width=1080, height=2160))


def router(mirror=None, factory_session=None):
    dev = device()
    r = InputRouter(Control(), SimpleNamespace(get_device=lambda i: dev), {"d": mirror} if mirror else {},
                    fast_input=True, session_factory=lambda serial: factory_session or Session(frame=(0, 0)))
    return r


def touches(s):
    out = []
    for m in s.sent:
        if m[0] == 2:
            _, a, _, x, y, w, h, _, _, _ = struct.unpack(">BBQiiHHHii", m)
            out.append((a, x, y, w, h))
    return out


def run(coro):
    return asyncio.run(coro)


class TestControlOnlySession(unittest.TestCase):
    def test_tap_without_mirror_goes_through_adb(self):
        r = router()                                     # control-only session: frame size unknown (0x0)
        self.assertEqual(run(r.tap("d", 540, 1080)), "adb")
        self.assertEqual(r.control.calls, [("tap", 540, 1080)])

    def test_keys_still_fast_without_mirror(self):
        s = Session(frame=(0, 0))
        r = router(factory_session=s)
        self.assertEqual(run(r.key("d", "HOME")), "scrcpy")
        self.assertEqual(len(s.sent), 2)


class TestGestures(unittest.TestCase):
    def test_swipe_takes_its_real_duration(self):
        s = Session()
        r = router(mirror=s)
        t = time.perf_counter()
        run(r.swipe("d", 540, 1800, 540, 600, 300))
        took = (time.perf_counter() - t) * 1000
        self.assertGreater(took, 250)
        self.assertLess(took, 420)                       # Windows timer granularity must not double it
        self.assertEqual(touches(s)[-1][1:3], (360, 400))

    def test_coordinates_clamped_inside_frame(self):
        s = Session()
        r = router(mirror=s)
        run(r.tap("d", 1080, 2160))                      # bottom-right pixel of the phone
        self.assertEqual(touches(s)[0][1:3], (719, 1439))

    def test_duration_clamped(self):
        self.assertEqual(InputRouter.clamp_duration(10 ** 9), 10_000)
        self.assertEqual(InputRouter.clamp_duration(-5), 1)

    def test_half_sent_gesture_is_not_replayed(self):
        s = Session(fail_after=1)                        # DOWN goes out, then the socket dies
        r = router(mirror=s)
        self.assertEqual(run(r.tap("d", 10, 10)), "interrupted")
        self.assertEqual(r.control.calls, [])            # no second tap through adb


class TestPointerAndScroll(unittest.TestCase):
    def test_pointer_events_streamed_live(self):
        s = Session()
        r = router(mirror=s)
        run(r.pointer("d", "down", 540, 1800))
        run(r.pointer("d", "move", 540, 1500))
        run(r.pointer("d", "up", 540, 1200))
        self.assertEqual([a[0] for a in touches(s)], [ACTION_DOWN, ACTION_MOVE, ACTION_UP])

    def test_pointer_fallback_turns_into_swipe(self):
        r = router()                                     # no usable frame size: adb
        run(r.pointer("d", "down", 540, 1800))
        run(r.pointer("d", "move", 540, 1500))
        run(r.pointer("d", "up", 540, 1200))
        self.assertEqual(r.control.calls[0][0], "swipe")
        self.assertEqual(r.control.calls[0][1:5], (540, 1800, 540, 1200))

    def test_pointer_fallback_short_press_is_tap(self):
        r = router()
        run(r.pointer("d", "down", 100, 100))
        run(r.pointer("d", "up", 102, 101))
        self.assertEqual(r.control.calls, [("tap", 102, 101)])

    def test_scroll_message_layout(self):
        m = scroll_event(10, 20, 720, 1440, 0.0, -1.0)
        self.assertEqual(len(m), 21)
        self.assertEqual(m[0], 3)
        self.assertEqual(struct.unpack(">hh", m[13:17]), (0, -32768))

    def test_wheel_scrolls(self):
        s = Session()
        r = router(mirror=s)
        self.assertEqual(run(r.scroll("d", 540, 1080, -1.0)), "scrcpy")
        self.assertEqual(s.sent[0][0], 3)


class TestTypableLatin(unittest.TestCase):
    def test_only_composable_latin(self):
        self.assertTrue(scrcpy_can_type("Café, ü ß ç Ñ"))
        self.assertFalse(scrcpy_can_type("Bjørn"))
        self.assertFalse(scrcpy_can_type("Ærø å µ"))


if __name__ == "__main__":
    unittest.main()
