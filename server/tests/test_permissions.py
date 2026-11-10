import asyncio
import unittest
from pathlib import Path

FIXTURE = (Path(__file__).parent / "fixtures" / "adb_permissions_realme_a13.txt").read_text()


def run(coro):
    return asyncio.run(coro)


class TestAdbParse(unittest.TestCase):
    def test_realme_report(self):
        from app.services.permissions import parse_adb_report
        p = parse_adb_report(FIXTURE)
        for k in ("camera", "audio", "phone_state", "answer_calls", "call_phone", "all_files_access", "overlay",
                  "install_updates", "accessibility_enabled", "battery_unrestricted", "app_running"):
            self.assertIs(p[k], True, k)
        self.assertEqual((p["app_version"], p["sdk"], p["installed"]), ("1.8.8", 33, True))

    def test_denied_and_missing(self):
        from app.services.permissions import parse_adb_report
        text = FIXTURE.replace("CAMERA: granted=true", "CAMERA: granted=false") \
            .replace("Uid mode: MANAGE_EXTERNAL_STORAGE: allow\n", "") \
            .replace("SYSTEM_ALERT_WINDOW: allow", "SYSTEM_ALERT_WINDOW: ignore") \
            .replace(":com.operator.companion/.service.CompanionAccessibilityService", "") \
            .replace("__IDLE\n1\n", "__IDLE\n0\n").replace("__PID\n30384\n", "__PID\n")
        p = parse_adb_report(text)
        for k in ("camera", "all_files_access", "overlay", "accessibility_enabled", "battery_unrestricted", "app_running"):
            self.assertIs(p[k], False, k)
        self.assertIs(p["audio"], True)

    def test_lite_edition_from_version_name(self):
        from app.services.permissions import parse_adb_report, rows
        p = parse_adb_report(FIXTURE.replace("versionName=1.8.8", "versionName=1.8.9-lite"))
        self.assertEqual(p["edition"], "lite")
        self.assertEqual({x["key"]: x["state"] for x in rows(p)}["accessibility_enabled"], "n/a")

    def test_not_installed(self):
        from app.services.permissions import parse_adb_report
        p = parse_adb_report("__SDK\n34\n__PKG\n__MES\n__SAW\n__RIP\n__ACC\nnull\n__IDLE\n0\n__PID\n__END\n")
        self.assertIs(p["installed"], False)

    def test_old_android_storage_uses_read_permission(self):
        from app.services.permissions import parse_adb_report
        text = FIXTURE.replace("__SDK\n33", "__SDK\n28").replace(
            "READ_MEDIA_IMAGES: granted=true", "READ_EXTERNAL_STORAGE: granted=true").replace(
            "Uid mode: MANAGE_EXTERNAL_STORAGE: allow\n", "")
        self.assertIs(parse_adb_report(text)["all_files_access"], True)


class TestRows(unittest.TestCase):
    def test_rows_states(self):
        from app.services.permissions import rows
        r = {x["key"]: x for x in rows({"camera": True, "overlay": False, "edition": "full",
                                        "accessibility_enabled": True, "accessibility_service": False})}
        self.assertEqual(r["camera"]["state"], "on")
        self.assertEqual(r["overlay"]["state"], "off")
        self.assertEqual(r["install_updates"]["state"], "unknown")  # older app: not reported
        self.assertEqual(r["accessibility_enabled"]["state"], "off")  # switched on but not running
        self.assertIn("not running", r["accessibility_enabled"]["note"])
        self.assertTrue(all(x["fix"] and x["unlocks"] for x in r.values()))

    def test_lite_has_no_pilot(self):
        from app.services.permissions import rows
        r = {x["key"]: x for x in rows({"edition": "lite"})}
        self.assertEqual(r["accessibility_enabled"]["state"], "n/a")


class FakeDev:
    def __init__(self, id, transport, relay_id=None, permissions=None, serial="S1"):
        self.id, self.transport, self.relay_id, self.serial = id, transport, relay_id, serial
        self.status, self.name, self.permissions = "online", "Phone", permissions or {}
        self.hardware_capabilities, self.power_and_lockscreen, self.manufacturer, self.sdk_int = {}, {}, "", 0


class FakeDM:
    def __init__(self, devs):
        self.devices = {d.id: d for d in devs}

    def get_device(self, i):
        return self.devices.get(i)


class FakeHub:
    def __init__(self, connected, fresh=None):
        self.connected, self.fresh, self.asked = connected, fresh, []

    def companion_connected(self, dev):
        return dev.id in self.connected

    async def request_capabilities(self, dev, timeout=6.0):
        self.asked.append(dev.id)
        if self.fresh is not None:
            dev.permissions = self.fresh
            return True
        return False


class FakeStorage:
    def __init__(self, text):
        self.text, self.calls = text, 0

    async def shell(self, serial, cmd, timeout=30.0):
        self.calls += 1
        return 0, self.text


class TestService(unittest.TestCase):
    def test_companion_report_is_fresh(self):
        from app.services.permissions import PermissionService
        d = FakeDev("r1", "companion_ws", "r1")
        svc = PermissionService(FakeDM([d]), FakeHub({"r1"}, fresh={"camera": True, "edition": "full"}), FakeStorage(""))
        rep = run(svc.report("r1"))
        self.assertEqual(rep["source"], "app")
        self.assertEqual({x["key"]: x["state"] for x in rep["rows"]}["camera"], "on")

    def test_usb_without_app_reads_adb(self):
        from app.services.permissions import PermissionService
        d = FakeDev("u1", "adb")
        st = FakeStorage(FIXTURE)
        rep = run(PermissionService(FakeDM([d]), FakeHub(set()), st).report("u1"))
        self.assertEqual(rep["source"], "adb")
        self.assertEqual(st.calls, 1)
        self.assertTrue(rep["app_running"])

    def test_offline_shows_last_known(self):
        from app.services.permissions import PermissionService
        d = FakeDev("r2", "companion_ws", "r2", permissions={"camera": False})
        d.status = "offline"
        rep = run(PermissionService(FakeDM([d]), FakeHub(set()), FakeStorage("")).report("r2"))
        self.assertEqual(rep["source"], "last known")


    def test_usb_adb_failure_falls_back_to_last_known(self):
        from app.services.permissions import PermissionService
        from app.services.storage_service import StorageError
        d = FakeDev("u1", "adb", permissions={"camera": True})

        class BadStorage:
            async def shell(self, serial, cmd, timeout=30.0):
                raise StorageError("USB debugging is not authorized on the device", 503)
        rep = run(PermissionService(FakeDM([d]), FakeHub(set()), BadStorage()).report("u1"))
        self.assertEqual(rep["source"], "last known")
        self.assertIn("not authorized", rep["message"])

    def test_all_checks_phones_in_parallel(self):
        import time as _t
        from app.services.permissions import PermissionService
        devs = [FakeDev(f"r{i}", "companion_ws", f"r{i}") for i in range(4)]

        class SlowHub(FakeHub):
            async def request_capabilities(self, dev, timeout=6.0):
                await asyncio.sleep(0.3)
                return False
        t0 = _t.monotonic()
        out = run(PermissionService(FakeDM(devs), SlowHub({d.id for d in devs}), FakeStorage("")).all())
        self.assertEqual(len(out), 4)
        self.assertLess(_t.monotonic() - t0, 0.8)

    def test_reads_the_device_record_updated_during_the_wait(self):
        from app.services.permissions import PermissionService
        d = FakeDev("u1", "adb", "r1")
        dm = FakeDM([d])

        class ReplacingHub(FakeHub):
            async def request_capabilities(self, dev, timeout=6.0):
                fresh = FakeDev("u1", "adb", "r1", permissions={"camera": True, "edition": "full"})
                dm.devices["u1"] = fresh  # device poll rebuilt the record while we waited
                return True
        rep = run(PermissionService(dm, ReplacingHub({"u1"}), FakeStorage("")).report("u1"))
        self.assertEqual({x["key"]: x["state"] for x in rep["rows"]}["camera"], "on")


class TestHubCapabilities(unittest.TestCase):
    def make(self):
        from app.websocket.hub import WebSocketHub

        class DM:
            def __init__(self):
                self.dev = FakeDev("realme_usb", "adb", "android_rmx_1")

            def add_listener(self, f):
                pass

            def update_device_capabilities(self, comp_id, caps):
                if comp_id == self.dev.relay_id:
                    self.dev.permissions = caps["permissions"]
                    return self.dev

        class AI:
            def add_listener(self, f):
                pass

        class Sock:
            def __init__(self):
                self.sent = []

            async def send_json(self, m):
                self.sent.append(m)
        hub = WebSocketHub(DM(), None, AI())
        sock = Sock()
        hub.device_clients["android_rmx_1"] = sock
        return hub, sock

    def test_request_waits_for_reply(self):
        hub, sock = self.make()
        dev = hub.device_manager.dev

        async def go():
            async def phone_answers():
                await asyncio.sleep(0.05)
                await hub._process_dashboard_message(sock, {"type": "DEVICE_CAPABILITIES", "device_id": "android_rmx_1",
                                                            "payload": {"permissions": {"camera": True}}})
            asyncio.ensure_future(phone_answers())
            return await hub.request_capabilities(dev, timeout=2)
        self.assertTrue(run(go()))
        self.assertEqual(sock.sent[0]["type"], "GET_CAPABILITIES")
        self.assertEqual(dev.permissions, {"camera": True})
        self.assertTrue(hub.companion_connected(dev))

    def test_request_times_out(self):
        hub, sock = self.make()
        self.assertFalse(run(hub.request_capabilities(hub.device_manager.dev, timeout=0.1)))


if __name__ == "__main__":
    unittest.main()
