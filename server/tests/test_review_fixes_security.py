"""Regression tests for the Phase 1 review findings (security + Windows encoding)."""
import asyncio
import json
import shlex
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.models.schemas import LaunchAppAction
from app.services.adb_bridge import ADBBridge


def run(coro):
    return asyncio.run(coro)


class Recorder(ADBBridge):
    def __init__(self):
        super().__init__("adb")
        self.commands = []

    async def run_device_shell(self, serial, command):
        self.commands.append(command)
        return "Events injected: 1\n"


class TestShellInjection(unittest.TestCase):
    def test_launch_schema_rejects_shell_metacharacters(self):
        with self.assertRaises(ValidationError):
            LaunchAppAction(package_name="x;pm uninstall --user 0 com.operator.companion;#")
        self.assertEqual(LaunchAppAction(package_name="com.android.settings").package_name, "com.android.settings")

    def test_launch_package_quotes_name(self):
        b = Recorder()
        run(b.launch_package("s", "a;reboot"))
        self.assertIn(shlex.quote("a;reboot"), b.commands[0])
        self.assertNotIn(" a;reboot ", b.commands[0])

    def test_list_files_quotes_path(self):
        b = Recorder()
        run(b.list_files("s", "x';reboot;'"))
        self.assertEqual(b.commands[0], "ls -la " + shlex.quote("x';reboot;'"))


class TestRelayIdCache(unittest.TestCase):
    def test_cache_pruned_when_serial_disconnects(self):
        from app.services.device_manager import DeviceManager
        dm = DeviceManager.__new__(DeviceManager)
        dm._relay_id_cache = {"192.168.1.50:5555": ("android_phone_a", 1.0), "keep": ("android_b", 1.0)}
        dm.prune_relay_id_cache({"keep"})
        self.assertEqual(list(dm._relay_id_cache), ["keep"])


class TestAudioRateClamp(unittest.TestCase):
    def test_sample_rate_clamped(self):
        from app.services.cam_record import clamp_sample_rate
        self.assertEqual(clamp_sample_rate(100_000_000), 48000)
        self.assertEqual(clamp_sample_rate(0), 8000)
        self.assertEqual(clamp_sample_rate(16000), 16000)


class TestUtf8Files(unittest.TestCase):
    """Windows defaults to cp1252 for read_text/write_text: state files must be UTF-8 on every OS."""

    def test_backup_json_round_trip_with_arrow(self):
        from app.services import backup
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "last_run.json"
            backup._write_json(p, {"summary": "sync started → folder · नमस्ते"})
            self.assertEqual(json.loads(p.read_bytes().decode("utf-8"))["summary"], "sync started → folder · नमस्ते")
            self.assertEqual(backup._read_json(p, {})["summary"], "sync started → folder · नमस्ते")


if __name__ == "__main__":
    unittest.main()
