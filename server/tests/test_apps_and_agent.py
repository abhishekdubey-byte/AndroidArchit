import asyncio
import unittest
from types import SimpleNamespace

from app.services.adb_bridge import ADBBridge, parse_launcher_packages
from app.services.ai_agent import AIAgentOperator

LAUNCHER_CMD = "cmd package query-activities"


def run(coro):
    return asyncio.run(coro)


class FakeBridge(ADBBridge):
    """Scripted phone: launcher activities, third-party packages, foreground app."""

    def __init__(self, launcher=(), third_party=(), fg="com.miui.home", fg_after_launch=None):
        super().__init__("adb")
        self.launcher, self.third_party = list(launcher), list(third_party)
        self.fg, self.fg_after_launch = fg, fg_after_launch
        self.commands = []

    async def run_device_shell(self, serial, command):
        self.commands.append(command)
        if command.startswith(LAUNCHER_CMD):
            return "".join(f"{p}/.Main\n" for p in self.launcher)
        if command.startswith("pm list packages -3"):
            return "".join(f"package:{p}\n" for p in self.third_party)
        if command.startswith("monkey -p"):
            if self.fg_after_launch:
                self.fg = self.fg_after_launch
            return "Events injected: 1\n"
        if command.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{8f7dbfb u0 {self.fg}/{self.fg}.MainActivity}}\n"
        return ""


class FakeDM:
    def __init__(self, dev):
        self.dev = dev

    def get_device(self, device_id):
        return self.dev


def make_agent(fg="com.miui.home", fg_after_launch=None, storage_percent=50, battery_temp=32.0):
    dev = SimpleNamespace(id="d", serial="s", name="Phone",
                          battery=SimpleNamespace(level=90, charging=True, temperature=battery_temp),
                          storage=SimpleNamespace(used_gb=10.0, total_gb=50.0, percent=storage_percent))
    bridge = FakeBridge(launcher=["com.android.settings"], fg=fg, fg_after_launch=fg_after_launch)
    return AIAgentOperator(FakeDM(dev), bridge)


async def run_instruction(agent, text, timeout=30):
    task = await agent.submit_task(text, "d")
    for _ in range(int(timeout * 10)):
        if task.status not in ("pending", "executing"):
            return task
        await asyncio.sleep(0.1)
    raise AssertionError(f"task still {task.status}")


class TestLauncherPackages(unittest.TestCase):
    def test_parse_launcher_packages(self):
        out = "com.android.settings/.MainSettings\ncom.whatsapp/.Main\nsome header\ncom.android.settings/.Other\n"
        self.assertEqual(parse_launcher_packages(out), ["com.android.settings", "com.whatsapp"])

    def test_settings_listed_and_named(self):
        apps = run(FakeBridge(launcher=["com.android.settings", "com.whatsapp"], third_party=["com.whatsapp"])
                   .get_installed_apps("s"))
        by_pkg = {a.package_name: a for a in apps}
        self.assertEqual((by_pkg["com.android.settings"].app_name, by_pkg["com.android.settings"].is_system),
                         ("Settings", True))
        self.assertFalse(by_pkg["com.whatsapp"].is_system)

    def test_facebook_named(self):
        apps = run(FakeBridge(launcher=["com.facebook.katana"], third_party=["com.facebook.katana"]).get_installed_apps("s"))
        self.assertEqual(apps[0].app_name, "Facebook")

    def test_falls_back_to_third_party_list(self):
        apps = run(FakeBridge(launcher=[], third_party=["com.whatsapp"]).get_installed_apps("s"))
        self.assertEqual([a.package_name for a in apps], ["com.whatsapp"])


class TestLaunchAndForeground(unittest.TestCase):
    def test_launch_package_result(self):
        self.assertTrue(run(FakeBridge().launch_package("s", "com.android.settings")))

        class NoActivity(FakeBridge):
            async def run_device_shell(self, serial, command):
                return "** No activities found to run, monkey aborted.\n"
        self.assertFalse(run(NoActivity().launch_package("s", "com.nope")))

    def test_foreground_package(self):
        self.assertEqual(run(FakeBridge(fg="com.android.settings").foreground_package("s")), "com.android.settings")


class TestAgentHonesty(unittest.TestCase):
    def test_open_app_fails_when_other_app_stays_in_front(self):
        task = run(run_instruction(make_agent(fg="com.miui.home"), "open settings"))
        self.assertEqual(task.status, "failed")
        self.assertIn("com.miui.home", task.result_summary)

    def test_open_app_succeeds_when_app_comes_to_front(self):
        task = run(run_instruction(make_agent(fg_after_launch="com.android.settings"), "open settings"))
        self.assertEqual(task.status, "completed")

    def test_report_warns_on_full_storage(self):
        task = run(run_instruction(make_agent(storage_percent=95), "check battery status"))
        self.assertIn("Storage 95% full", task.steps[-1].result)
        self.assertNotIn("audit normal", task.steps[-1].result)

    def test_report_warns_on_hot_battery(self):
        task = run(run_instruction(make_agent(battery_temp=46.0), "check battery status"))
        self.assertIn("Battery hot (46.0°C)", task.steps[-1].result)

    def test_report_normal(self):
        task = run(run_instruction(make_agent(), "check battery status"))
        self.assertEqual(task.steps[-1].result, "System telemetry audit normal.")


if __name__ == "__main__":
    unittest.main()


class TestRemoteForeground(unittest.TestCase):
    def test_foreground_from_companion_ui_tree(self):
        from app.services.remote_control import DeviceControl

        class Client:
            async def request(self, op, args, timeout=20.0):
                assert op == "ctl.ui"
                return {"nodes": [{"index": 0, "text": "", "resource_id": "", "class_name": "FrameLayout",
                                   "package": "com.android.settings", "content_desc": "", "clickable": False,
                                   "selected": False, "checked": False, "bounds": [0, 0, 1, 1], "children": []}]}, b""

        dev = SimpleNamespace(transport="companion_ws", status="online", relay_id="rid", name="Phone")
        relays = SimpleNamespace(get=lambda rid: Client() if rid else None)
        ctl = DeviceControl(FakeDM(dev), relays, adb_path="adb")
        self.assertEqual(run(ctl.foreground_package("s")), "com.android.settings")
