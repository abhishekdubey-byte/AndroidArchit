"""Regression tests for the Phase 1 review findings (behaviour / honesty / smoothness)."""
import asyncio
import threading
import time
import unittest
from types import SimpleNamespace

from app.services.adb_bridge import ADBBridge, InputRejected, UiDumpUnavailable, UnsupportedText, normalize_text
from app.services.remote_control import DeviceControl


def run(coro):
    return asyncio.run(coro)


class TestAppWideErrors(unittest.TestCase):
    def test_handlers_registered(self):
        from app.main import app
        for exc, code in ((UnsupportedText("x"), 422), (UiDumpUnavailable("x"), 503), (InputRejected("x"), 409)):
            handler = app.exception_handlers[type(exc)]
            resp = run(handler(None, exc))
            self.assertEqual(resp.status_code, code)


class Client:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    async def request(self, op, args, timeout=20.0):
        self.calls.append((op, args.get("text", args.get("key"))))
        return {"ok": self.ok}, b""


def control(client, usb=True):
    dev = SimpleNamespace(transport="adb" if usb else "companion_ws", status="online", relay_id="rid", name="P")
    ctl = DeviceControl(SimpleNamespace(get_device=lambda s: dev), SimpleNamespace(get=lambda rid: client), "adb")

    async def shell(serial, command):
        ctl.adb_commands.append(command)
        return ""
    ctl.adb_commands = []
    ctl.run_device_shell = shell
    return ctl


class TestTextNewlinesAndNormalising(unittest.TestCase):
    def test_companion_newline_becomes_enter(self):
        c = Client()
        run(control(c).inject_text("s", "café\nok"))
        self.assertEqual(c.calls, [("ctl.text", "café"), ("ctl.key", "ENTER"), ("ctl.text", "ok")])

    def test_normalize_text(self):
        self.assertEqual(normalize_text("a\r\nb\rc\td"), "a\nb\nc d")

    def test_windows_line_endings_typed_over_adb(self):
        ctl = control(Client(), usb=True)
        run(ctl.inject_text("s", "line1\r\nline2\tend"))   # plain ASCII from a Windows client: no 422
        self.assertIn("input text line1", ctl.adb_commands)
        self.assertIn("input text line2%send", ctl.adb_commands)

    def test_companion_refusal_raises(self):
        with self.assertRaises(InputRejected):
            run(control(Client(ok=False), usb=False).inject_text("s", "hello"))


class SlowRecorder:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        time.sleep(0.5)
        self.closed.set()
        return "D:\\x\\live.mp4"


class FakeDM:
    def __init__(self, dev):
        self.dev = dev

    def add_listener(self, fn):
        pass

    def get_device(self, device_id):
        return self.dev

    def get_all_devices(self):
        return [self.dev]


class FakeWS:
    def __init__(self):
        self.json = []

    async def send_json(self, m):
        self.json.append(m)


def make_hub(adb):
    from app.websocket.hub import WebSocketHub
    dev = SimpleNamespace(id="d", serial="s", name="P", transport="adb", status="online", relay_id="")
    hub = WebSocketHub(FakeDM(dev), adb, SimpleNamespace(add_listener=lambda fn: None))
    ws = FakeWS()
    hub.dashboard_clients.add(ws)
    return hub, ws


class TestRecordingDoesNotBlockSocket(unittest.IsolatedAsyncioTestCase):
    async def test_stop_save_returns_immediately(self):
        hub, ws = make_hub(SimpleNamespace(adb_path="adb"))
        rec = SlowRecorder()
        hub.cam_recorders["d"] = rec
        t = time.perf_counter()
        await hub._process_dashboard_message(ws, {"type": "STOP_CAMERA_SAVE", "device_id": "d"})
        self.assertLess(time.perf_counter() - t, 0.1)          # the socket keeps reading input
        self.assertNotIn("d", hub.cam_recorders)
        for _ in range(100):
            if any(m.get("type") == "CAMERA_SAVE_STATE" for m in ws.json):
                break
            await asyncio.sleep(0.02)
        saved = [m for m in ws.json if m.get("type") == "CAMERA_SAVE_STATE"]
        self.assertEqual(saved[0]["payload"]["name"], "live.mp4")


class TestDrainKillsStuckEncoder(unittest.TestCase):
    def test_drain_kills_ffmpeg_when_writer_is_stuck(self):
        from app.services.cam_record import CamRecorder
        rec = CamRecorder.__new__(CamRecorder)
        rec._closed = False
        rec.DRAIN_TIMEOUT = 0.3
        unblock = threading.Event()
        rec._proc = SimpleNamespace(kill=unblock.set)        # killing ffmpeg breaks the blocked pipe write
        rec._start_worker()
        rec.write = lambda b, at=None: unblock.wait(30)
        rec.submit(b"x")
        t = time.perf_counter()
        rec.drain()
        self.assertLess(time.perf_counter() - t, 3)
        self.assertTrue(unblock.is_set())


class NoCompanionAdb(ADBBridge):
    def __init__(self):
        super().__init__("adb")
        self.captures = 0

    async def run_device_shell(self, serial, command):
        return ""                                            # pm list packages: not installed

    async def capture_headless_photo_adb(self, *a, **k):
        self.captures += 1
        await asyncio.sleep(30)


class TestDashboardCameraFastFail(unittest.IsolatedAsyncioTestCase):
    async def test_photo_button_fails_fast_without_companion(self):
        adb = NoCompanionAdb()
        hub, ws = make_hub(adb)
        await hub._process_dashboard_message(ws, {"type": "CAPTURE_PHOTO", "device_id": "d", "payload": {"lens": "back"}})
        for _ in range(100):
            if any(m.get("type") == "PHOTO_CAPTURED" for m in ws.json):
                break
            await asyncio.sleep(0.02)
        got = [m for m in ws.json if m.get("type") == "PHOTO_CAPTURED"]
        self.assertTrue(got and not got[0]["payload"]["success"])
        self.assertIn("Install the Operator Companion app", got[0]["payload"]["error"])
        self.assertEqual(adb.captures, 0)

    async def test_headless_photo_api_fails_fast_without_companion(self):
        adb = NoCompanionAdb()
        hub, _ = make_hub(adb)
        t = time.perf_counter()
        res = await hub.capture_headless_photo("d")
        self.assertLess(time.perf_counter() - t, 2)
        self.assertFalse(res["success"])
        self.assertIn("Install the Operator Companion app", res["error"])


class TestAgentForegroundCheck(unittest.TestCase):
    def test_slow_foreground_query_is_bounded_and_step_marked_failed(self):
        from app.services.ai_agent import AIAgentOperator

        class Slow(ADBBridge):
            def __init__(self):
                super().__init__("adb")

            async def run_device_shell(self, serial, command):
                if command.startswith("cmd package query-activities"):
                    return "com.android.settings/.MainSettings\n"
                if command.startswith("monkey"):
                    return "Events injected: 1\n"
                if "dumpsys window" in command:
                    await asyncio.sleep(30)                   # wedged dumpsys
                return ""

        dev = SimpleNamespace(id="d", serial="s", name="P", transport="adb", status="online",
                              battery=SimpleNamespace(level=1, charging=False, temperature=30),
                              storage=SimpleNamespace(used_gb=1, total_gb=2, percent=50))
        agent = AIAgentOperator(SimpleNamespace(get_device=lambda i: dev), Slow())
        agent.FOREGROUND_BUDGET = 1.5

        async def go():
            task = await agent.submit_task("open settings", "d")
            t = time.perf_counter()
            while task.status in ("pending", "executing"):
                await asyncio.sleep(0.05)
                if time.perf_counter() - t > 10:
                    break
            return task, time.perf_counter() - t
        task, took = run(go())
        self.assertEqual(task.status, "failed")
        self.assertLess(took, 6)
        self.assertEqual(task.steps[0].status, "failed")
        self.assertIn("locked", task.result_summary)          # USB hint, not the MIUI pop-up permission


if __name__ == "__main__":
    unittest.main()
