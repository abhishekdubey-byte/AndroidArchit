import asyncio
import unittest
from types import SimpleNamespace

from app.services.device_manager import DeviceManager


class FakeAdb:
    def __init__(self):
        self.detail_calls = 0

    async def list_attached_devices(self):
        return [{"serial": "s"}]

    async def get_device_details(self, serial):
        self.detail_calls += 1
        from app.models.schemas import Device
        return Device(id="d", serial="s", name="P", model="M", brand="b", manufacturer="m", android_version="9",
                      sdk_int=28, transport="adb", status="online")

    async def run_device_shell(self, serial, cmd):
        return ""


class TestTelemetryWhileMirroring(unittest.TestCase):
    def test_details_polled_slowly_while_mirroring(self):
        adb = FakeAdb()
        dm = DeviceManager(adb)

        async def go():
            await dm.refresh_devices()            # first sight: full details
            dm.busy_serials.add("s")              # hub: fast mirror running on this phone
            await dm.refresh_devices()
            await dm.refresh_devices()
            dm.busy_serials.discard("s")
            await dm.refresh_devices()            # back to normal polling
        asyncio.run(go())
        self.assertEqual(adb.detail_calls, 2)
        self.assertEqual(dm.devices["d"].status, "online")


if __name__ == "__main__":
    unittest.main()
