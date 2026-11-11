import asyncio
import unittest
from types import SimpleNamespace

from app.services.adb_bridge import ADBBridge, UiDumpUnavailable, UnsupportedText, adb_typable
from app.services.remote_control import DeviceControl


def run(coro):
    return asyncio.run(coro)


class FakeBridge(ADBBridge):
    def __init__(self, output=""):
        super().__init__("adb")
        self.output = output
        self.commands = []

    async def run_device_shell(self, serial, command):
        self.commands.append(command)
        return self.output


class FakeClient:
    def __init__(self):
        self.calls = []

    async def request(self, op, args, timeout=20.0):
        self.calls.append((op, args))
        return {"ok": True}, b""


class FakeRelays:
    def __init__(self, client):
        self.client = client

    def get(self, rid):
        return self.client if rid else None


class FakeDM:
    def __init__(self, dev):
        self.dev = dev

    def get_device(self, serial):
        return self.dev


class FakeDeviceControl(DeviceControl):
    def __init__(self, usb=True, companion=True):
        dev = SimpleNamespace(transport="adb" if usb else "companion_ws", status="online",
                              relay_id="rid" if companion else "", name="Phone")
        self.client = FakeClient() if companion else None
        super().__init__(FakeDM(dev), FakeRelays(self.client), adb_path="adb")
        self.adb_commands = []

    async def run_device_shell(self, serial, command):
        self.adb_commands.append(command)
        return ""

    @property
    def companion_texts(self):
        return [a["text"] for op, a in self.client.calls if op == "ctl.text"]


class TestAdbTypable(unittest.TestCase):
    def test_adb_typable(self):
        self.assertTrue(adb_typable("battery & saver (test) 50%\nok"))
        self.assertFalse(adb_typable("café"))
        self.assertFalse(adb_typable("😀"))
        self.assertFalse(adb_typable("नमस्ते"))


class TestAdbTextInput(unittest.TestCase):
    def test_unicode_raises_and_sends_nothing(self):
        b = FakeBridge()
        with self.assertRaises(UnsupportedText):
            run(b.inject_text("s", "नमस्ते"))
        self.assertEqual(b.commands, [])

    def test_ascii_still_typed_over_adb(self):
        b = FakeBridge()
        run(b.inject_text("s", "hello world"))
        self.assertEqual(b.commands, ["input text hello%sworld"])


class TestUiDump(unittest.TestCase):
    def test_ui_dump_error_output_raises(self):
        b = FakeBridge(output="ERROR: could not get idle state.")
        with self.assertRaises(UiDumpUnavailable):
            run(b.dump_ui_hierarchy("s"))

    def test_ui_dump_timeout_raises(self):
        class Slow(FakeBridge):
            async def run_device_shell(self, serial, command):
                if "uiautomator dump" in command:
                    await asyncio.sleep(60)
                return ""
        b = Slow()
        b.UI_DUMP_TIMEOUT = 0.05
        with self.assertRaises(UiDumpUnavailable):
            run(b.dump_ui_hierarchy("s"))


class TestDeviceControlText(unittest.TestCase):
    def test_mixed_text_goes_to_companion_when_connected(self):
        ctl = FakeDeviceControl(usb=True, companion=True)
        run(ctl.inject_text("s", "hi café\nok"))
        self.assertEqual(ctl.companion_texts, ["hi café\nok"])
        self.assertEqual(ctl.adb_commands, [])

    def test_ascii_on_usb_stays_on_adb(self):
        ctl = FakeDeviceControl(usb=True, companion=True)
        run(ctl.inject_text("s", "hello"))
        self.assertEqual(ctl.companion_texts, [])
        self.assertEqual(ctl.adb_commands, ["input text hello"])

    def test_unicode_on_usb_without_companion_raises(self):
        ctl = FakeDeviceControl(usb=True, companion=False)
        with self.assertRaises(UnsupportedText):
            run(ctl.inject_text("s", "😀"))


if __name__ == "__main__":
    unittest.main()
