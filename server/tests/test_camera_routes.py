import asyncio
import inspect
import time
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.routes import create_router
from app.services.adb_bridge import ADBBridge


def run(coro):
    return asyncio.run(coro)


class FakeBridge(ADBBridge):
    def __init__(self, packages=""):
        super().__init__("adb")
        self.packages = packages
        self.commands = []

    async def run_device_shell(self, serial, command):
        self.commands.append(command)
        return self.packages if command.startswith("pm list packages") else ""


class FakeHub:
    def __init__(self):
        self.calls = []

    async def capture_headless_photo(self, device_id, lens="back"):
        self.calls.append("photo")
        await asyncio.sleep(30)

    async def record_headless_video(self, device_id, lens="back", duration_sec=5):
        self.calls.append("video")
        await asyncio.sleep(30)


def endpoint(router, path):
    return next(r.endpoint for r in router.routes if r.path == path)


def make_router(packages, transport="adb"):
    dev = SimpleNamespace(id="d", serial="s", name="Phone", transport=transport, status="online")
    dm = SimpleNamespace(get_device=lambda device_id: dev)
    hub = FakeHub()
    return create_router(dm, FakeBridge(packages), ai_agent=None, ws_hub=hub), hub


class TestCompanionInstalled(unittest.TestCase):
    def test_companion_installed(self):
        self.assertFalse(run(FakeBridge("").companion_installed("s")))
        self.assertTrue(run(FakeBridge("package:com.operator.companion\n").companion_installed("s")))
        self.assertFalse(run(FakeBridge("package:com.operator.companion.other\n").companion_installed("s")))


class TestCameraFastFail(unittest.TestCase):
    def assert_fast_409(self, path, **kwargs):
        router, hub = make_router(packages="")
        t = time.perf_counter()
        with self.assertRaises(HTTPException) as cm:
            run(endpoint(router, path)(device_id="d", **kwargs))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("Install the Operator Companion app", cm.exception.detail)
        self.assertLess(time.perf_counter() - t, 2)
        self.assertEqual(hub.calls, [])

    def test_photo_fails_fast_without_companion(self):
        self.assert_fast_409("/api/devices/{device_id}/camera/photo", lens="back")

    def test_video_fails_fast_without_companion(self):
        self.assert_fast_409("/api/devices/{device_id}/camera/video", lens="back", duration_sec=5)


class TestSimulatedCaller(unittest.TestCase):
    def test_neutral_defaults(self):
        router, _ = make_router(packages="")
        params = inspect.signature(endpoint(router, "/api/devices/{device_id}/call/simulate_incoming")).parameters
        self.assertEqual(params["phone_number"].default, "+00 00000 00000")
        self.assertEqual(params["caller_name"].default, "Test Caller")


if __name__ == "__main__":
    unittest.main()
