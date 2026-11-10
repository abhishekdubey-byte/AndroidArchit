import asyncio
import os
import tempfile
import unittest
from pathlib import Path


class _Root(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.environ["ACC_BACKUP_ROOT"] = str(self.root)

    def tearDown(self):
        os.environ.pop("ACC_BACKUP_ROOT", None)
        self._tmp.cleanup()


class TestStore(_Root):
    def test_folder_is_stable_and_unique(self):
        from app.services.backup import PhoneStore
        a = PhoneStore(self.root, "android_rmx3031_8fc4f390aaaa", "Realme RMX3031")
        b = PhoneStore(self.root, "android_rmx3031_1111222233", "Realme RMX3031")
        self.assertNotEqual(a.dir, b.dir)
        self.assertTrue(a.dir.is_relative_to(self.root) and b.dir.is_relative_to(self.root))
        a.save_selection({"presets": ["camera"], "custom": []})  # first save fixes the folder name
        again = PhoneStore(self.root, "android_rmx3031_8fc4f390aaaa", "Renamed Phone")
        self.assertEqual(again.dir, a.dir)

    def test_rel_path(self):
        from app.services.backup import rel_path
        self.assertEqual(rel_path("/sdcard/DCIM/a.jpg"), "DCIM/a.jpg")
        self.assertEqual(rel_path("/storage/emulated/0/DCIM/a.jpg"), "DCIM/a.jpg")
        self.assertEqual(rel_path("/storage/1A2B-3C4D/x.mp4"), "_volumes/1A2B-3C4D/x.mp4")

    def test_dest_refuses_escape(self):
        from app.services.backup import PhoneStore, BackupError
        s = PhoneStore(self.root, "android_x_1234567890", "X")
        try:
            d = s.dest_for("/sdcard/../../etc/passwd")
            self.assertTrue(d.resolve().is_relative_to(s.dir.resolve()))
        except BackupError:
            pass
        with self.assertRaises(BackupError):
            s.dest_for("/sdcard/a\nb")

    def test_deleted_dest_collision(self):
        from app.services.backup import PhoneStore
        s = PhoneStore(self.root, "android_x_1234567890", "X")
        first = s.deleted_dest("/sdcard/DCIM/a.jpg")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"old")
        self.assertEqual(first.name, "a.jpg")
        self.assertEqual(s.deleted_dest("/sdcard/DCIM/a.jpg").name, "a (2).jpg")

    def test_manifest_roundtrip_atomic(self):
        from app.services.backup import PhoneStore
        s = PhoneStore(self.root, "android_x_1234567890", "X")
        m = {"/sdcard/a.jpg": {"size": 3, "mtime": 10, "state": "present"}}
        s.save_manifest(m)
        self.assertEqual(s.load_manifest(), m)
        self.assertEqual(list(s.dir.rglob("*.tmp")), [])

    def test_reserved_names_refused(self):
        from app.services.backup import PhoneStore, BackupError
        s = PhoneStore(self.root, "android_x_1234567890", "X")
        for p in ("/sdcard/Deleted on phone/DCIM/a.jpg", "/sdcard/_volumes/1A2B-3C4D/x", "/sdcard/_root/etc/x"):
            with self.assertRaises(BackupError, msg=p):
                s.dest_for(p)

    def test_skip(self):
        from app.services.backup import skip
        self.assertTrue(skip(".trashed-1-x.jpg", False))
        self.assertTrue(skip(".pending-2-y.jpg", False))
        self.assertTrue(skip(".thumbnails", True))
        self.assertFalse(skip(".nomedia", False))
        self.assertFalse(skip("IMG_1.jpg", False))


class FakeBackend:
    """In-memory phone: files {path: (bytes, mtime)}; behaves like StorageService/RelayStorage."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.denied = set()          # roots that raise a permission error
        self.fail_after = None       # (path, nbytes): raise once after sending nbytes of that file
        self.short_after = None      # (path, nbytes): end the stream early once, then go offline (USB unplug)
        self.conn_error = None       # path whose read raises ConnectionError
        self.offline = False
        self.reads = []              # (path, start)

    def _err(self, msg, status):
        from app.services.storage_service import StorageError
        return StorageError(msg, status)

    def _dirs(self):
        d = set()
        for p in self.files:
            parts = p.split("/")
            for i in range(2, len(parts)):
                d.add("/".join(parts[:i]))
        return d

    async def list_dir(self, key, path):
        if any(path == r or path.startswith(r + "/") for r in self.denied):
            raise self._err("Permission denied", 403)
        if path not in self._dirs():
            raise self._err("No such folder", 404)
        items, seen = [], set()
        for p, (data, mtime) in self.files.items():
            if p.startswith(path + "/"):
                rest = p[len(path) + 1:]
                name = rest.split("/")[0]
                if name in seen:
                    continue
                seen.add(name)
                is_dir = "/" in rest
                items.append({"name": name, "path": f"{path}/{name}", "is_dir": is_dir,
                              "size": 0 if is_dir else len(data), "mtime": 0 if is_dir else mtime})
        return {"path": path, "items": items}

    async def stat(self, key, path):
        if self.offline:
            raise self._err("Remote device disconnected", 503)
        if path in self.files:
            data, mtime = self.files[path]
            return {"name": path.rsplit("/", 1)[-1], "path": path, "is_dir": False, "size": len(data), "mtime": mtime}
        if path in self._dirs():
            return {"name": path.rsplit("/", 1)[-1], "path": path, "is_dir": True, "size": 0, "mtime": 0}
        raise self._err("Not found", 404)

    async def read_range(self, key, path, start, length):
        self.reads.append((path, start))
        data = self.files[path][0][start:start + length]
        if self.conn_error == path:
            raise ConnectionError("socket closed")
        if self.short_after and self.short_after[0] == path:
            n = self.short_after[1]
            self.short_after = None
            self.offline = True
            yield data[:n]
            return
        if self.fail_after and self.fail_after[0] == path:
            n = self.fail_after[1]
            self.fail_after = None
            yield data[:n]
            raise self._err("Remote device disconnected", 503)
        for i in range(0, len(data), 700):
            yield data[i:i + 700]


def run(coro):
    return asyncio.run(coro)


class TestEngine(_Root):
    ROOT = "/sdcard/DCIM/Camera"

    def make(self, backend, roots=None, progress=None):
        from app.services.backup import PhoneStore, BackupJob, walk
        store = PhoneStore(self.root, "android_x_1234567890", "X")
        cancel = asyncio.Event()

        async def scanner(root):
            return await walk(backend, "k", root)
        job = BackupJob(store, backend, "k", roots or [self.ROOT], scanner, progress or (lambda p: None), cancel)
        return store, job, cancel

    def phone(self):
        return FakeBackend({f"{self.ROOT}/a.jpg": (b"A" * 1500, 1700000000),
                            f"{self.ROOT}/sub/b.jpg": (b"B" * 10, 1700000100),
                            f"{self.ROOT}/c.mp4": (bytes(range(256)) * 20, 1700000200),
                            f"{self.ROOT}/.trashed-1-x.jpg": (b"T", 1)})

    def test_first_run_copies_all_and_preserves_mtime(self):
        be = self.phone()
        store, job, _ = self.make(be)
        s = run(job.run())
        self.assertEqual((s["status"], s["copied"]), ("done", 3))
        for p, (data, mtime) in be.files.items():
            if ".trashed-" in p:
                self.assertFalse(store.dest_for(p).exists())
                continue
            self.assertEqual(store.dest_for(p).read_bytes(), data)
            self.assertEqual(int(store.dest_for(p).stat().st_mtime), mtime)

    def test_second_run_copies_nothing(self):
        be = self.phone()
        run(self.make(be)[1].run())
        self.assertEqual(run(self.make(be)[1].run())["copied"], 0)

    def test_changed_file_replaced(self):
        be = self.phone()
        run(self.make(be)[1].run())
        be.files[f"{self.ROOT}/a.jpg"] = (b"NEW", 1700000999)
        store, job, _ = self.make(be)
        self.assertEqual(run(job.run())["copied"], 1)
        self.assertEqual(store.dest_for(f"{self.ROOT}/a.jpg").read_bytes(), b"NEW")

    def test_deleted_on_phone_kept(self):
        be = self.phone()
        run(self.make(be)[1].run())
        del be.files[f"{self.ROOT}/sub/b.jpg"]
        store, job, _ = self.make(be)
        s = run(job.run())
        self.assertEqual(s["kept_deleted"], 1)
        self.assertFalse(store.dest_for(f"{self.ROOT}/sub/b.jpg").exists())
        kept = store.dir / "Deleted on phone" / "DCIM/Camera/sub/b.jpg"
        self.assertEqual(kept.read_bytes(), b"B" * 10)
        self.assertEqual(store.load_manifest()[f"{self.ROOT}/sub/b.jpg"]["state"], "deleted")

    def test_failed_root_not_treated_as_deleted(self):
        be = self.phone()
        run(self.make(be)[1].run())
        be.denied.add(self.ROOT)
        store, job, _ = self.make(be)
        s = run(job.run())
        self.assertEqual(s["skipped_roots"], [self.ROOT])
        self.assertEqual(s["kept_deleted"], 0)
        self.assertTrue(store.dest_for(f"{self.ROOT}/a.jpg").exists())
        self.assertTrue(any("All files access" in e for e in s["errors"]))

    def test_resume_part_file(self):
        big = f"{self.ROOT}/c.mp4"
        be = self.phone()
        be.fail_after = (big, 1000)
        store, job, _ = self.make(be)
        s = run(job.run())
        self.assertEqual(s["status"], "error")
        self.assertIn("press Sync now to resume", s["message"])
        part = store.part_for(big)
        self.assertEqual(part.stat().st_size, 1000)
        store, job, _ = self.make(be)
        self.assertEqual(run(job.run())["status"], "done")
        self.assertEqual(store.dest_for(big).read_bytes(), be.files[big][0])
        self.assertIn((big, 1000), be.reads)
        self.assertFalse(part.exists())

    def test_reappeared_file(self):
        p = f"{self.ROOT}/a.jpg"
        be = self.phone()
        run(self.make(be)[1].run())
        saved = be.files.pop(p)
        run(self.make(be)[1].run())
        be.files[p] = saved
        store, job, _ = self.make(be)
        self.assertEqual(run(job.run())["copied"], 1)
        self.assertTrue(store.dest_for(p).exists())
        self.assertTrue((store.dir / "Deleted on phone" / "DCIM/Camera/a.jpg").exists())

    def test_cancel(self):
        holder = {}

        def progress(p):
            if p.get("files_done", 0) >= 1:
                holder["cancel"].set()
        be = self.phone()
        store, job, cancel = self.make(be, progress=progress)
        job.min_interval = 0  # report every file so the test can cancel after the first
        holder["cancel"] = cancel
        s = run(job.run())
        self.assertEqual(s["status"], "cancelled")
        m = store.load_manifest()
        self.assertGreaterEqual(len([v for v in m.values() if v["state"] == "present"]), 1)
        self.assertLess(s["copied"], 3)

    def test_foreign_part_file_not_used(self):
        """Phone has both a.zip and a.zip.part: one must never be resumed from the other."""
        be = FakeBackend({f"{self.ROOT}/a.zip.part": (b"P" * 300, 1700000000),
                          f"{self.ROOT}/a.zip": (b"Z" * 1000, 1700000001)})
        store, job, _ = self.make(be)
        self.assertEqual(run(job.run())["copied"], 2)
        self.assertEqual(store.dest_for(f"{self.ROOT}/a.zip").read_bytes(), b"Z" * 1000)
        self.assertEqual(store.dest_for(f"{self.ROOT}/a.zip.part").read_bytes(), b"P" * 300)

    def test_stale_part_discarded_when_file_changed(self):
        big = f"{self.ROOT}/c.mp4"
        be = self.phone()
        be.fail_after = (big, 1000)
        run(self.make(be)[1].run())
        be.files[big] = (b"N" * 5120, 1700009999)  # edited on the phone before the next sync
        store, job, _ = self.make(be)
        self.assertEqual(run(job.run())["status"], "done")
        self.assertEqual(store.dest_for(big).read_bytes(), b"N" * 5120)

    def test_usb_unplug_short_read_is_disconnect(self):
        big = f"{self.ROOT}/c.mp4"
        be = self.phone()
        be.short_after = (big, 1000)
        store, job, _ = self.make(be, roots=[big, f"{self.ROOT}/a.jpg"])
        s = run(job.run())
        self.assertEqual(s["status"], "error")
        self.assertIn("press Sync now to resume", s["message"])
        be.offline = False
        store, job, _ = self.make(be, roots=[big, f"{self.ROOT}/a.jpg"])
        self.assertEqual(run(job.run())["status"], "done")
        self.assertIn((big, 1000), be.reads)
        self.assertEqual(store.dest_for(big).read_bytes(), be.files[big][0])

    def test_connection_error_is_disconnect(self):
        be = self.phone()
        be.conn_error = f"{self.ROOT}/a.jpg"
        s = run(self.make(be)[1].run())
        self.assertEqual(s["status"], "error")
        self.assertIn("press Sync now to resume", s["message"])

    def test_laptop_error_on_one_file_does_not_stop_run(self):
        be = self.phone()
        store, job, _ = self.make(be)
        store.dest_for(f"{self.ROOT}/a.jpg").mkdir(parents=True)  # a folder where the file must go
        s = run(job.run())
        self.assertEqual((s["status"], s["copied"]), ("done", 2))
        self.assertTrue(any("a.jpg" in e for e in s["errors"]))

    def test_unexpected_error_still_reports(self):
        be = self.phone()

        async def bad_list(key, path):
            return {"items": [{"name": "x.jpg", "path": f"{path}/x.jpg", "is_dir": False}]}  # no size
        be.list_dir = bad_list
        store, job, _ = self.make(be)
        s = run(job.run())
        self.assertEqual(s["status"], "error")
        self.assertEqual(store.load_last_run()["status"], "error")

    def test_empty_root_with_backed_up_files_is_not_mass_deleted(self):
        be = self.phone()
        run(self.make(be)[1].run())
        # the folder still exists (stat says dir) but Android suddenly lists nothing in it

        async def empty_list(key, path):
            return {"items": []}
        be.list_dir = empty_list
        store, job, _ = self.make(be)
        s = run(job.run())
        self.assertEqual(s["kept_deleted"], 0)
        self.assertEqual(s["skipped_roots"], [self.ROOT])
        self.assertTrue(store.dest_for(f"{self.ROOT}/a.jpg").exists())

    def test_single_file_selection(self):
        be = self.phone()
        p = f"{self.ROOT}/sub/b.jpg"
        store, job, _ = self.make(be, roots=[p])
        s = run(job.run())
        self.assertEqual(s["copied"], 1)
        self.assertTrue(store.dest_for(p).exists())


class FakeRelayStorage(FakeBackend):
    """FakeBackend that also answers the phone-side `scan` op in pages."""

    def __init__(self, files=None, pages=None, unsupported=False):
        super().__init__(files)
        self.pages = pages
        self.unsupported = unsupported
        self.scans = 0

    async def scan(self, rid, path, page, scan_id=None):
        self.scans += 1
        if self.unsupported:
            raise self._err("Unknown storage operation: scan", 400)
        if self.pages is not None:
            return self.pages[page]
        entries = [[p[len(path) + 1:], len(d), m] for p, (d, m) in self.files.items()
                   if p.startswith(path + "/") and ".trashed-" not in p]
        return {"entries": entries, "next": None}


class TestPhoneScan(_Root):
    ROOT = "/sdcard/DCIM/Camera"

    def test_pages_joined(self):
        from app.services.backup import phone_scanner
        pages = [{"entries": [[f"f{i}.jpg", 1, 1] for i in range(5000)], "next": 1},
                 {"entries": [["x/y.jpg", 2, 3], ["z.jpg", 4, 5]], "next": None}]
        out = run(phone_scanner(FakeRelayStorage(pages=pages), "rid")(self.ROOT))
        self.assertEqual(len(out), 5002)
        self.assertIn((f"{self.ROOT}/x/y.jpg", 2, 3), out)

    def test_bad_rel_rejected(self):
        from app.services.backup import phone_scanner
        pages = [{"entries": [["../x", 1, 1], ["/etc/x", 1, 1], ["a\nb", 1, 1], ["d/../../e", 1, 1], ["ok.jpg", 1, 1]],
                  "next": None}]
        out = run(phone_scanner(FakeRelayStorage(pages=pages), "rid")(self.ROOT))
        self.assertEqual(out, [(f"{self.ROOT}/ok.jpg", 1, 1)])

    def test_unsupported_falls_back(self):
        from app.services.backup import PhoneStore, BackupJob, phone_scanner
        be = FakeRelayStorage({f"{self.ROOT}/a.jpg": (b"abc", 1700000000)}, unsupported=True)
        store = PhoneStore(self.root, "android_x_1234567890", "X")
        job = BackupJob(store, be, "rid", [self.ROOT], phone_scanner(be, "rid"), lambda p: None, asyncio.Event())
        s = run(job.run())
        self.assertEqual((s["status"], s["copied"]), ("done", 1))
        self.assertEqual(be.scans, 1)
        self.assertEqual(s["scan"], "laptop")

    def test_phone_scan_used(self):
        from app.services.backup import PhoneStore, BackupJob, phone_scanner
        be = FakeRelayStorage({f"{self.ROOT}/a.jpg": (b"abc", 1700000000)})
        store = PhoneStore(self.root, "android_x_1234567890", "X")
        s = run(BackupJob(store, be, "rid", [self.ROOT], phone_scanner(be, "rid"), lambda p: None, asyncio.Event()).run())
        self.assertEqual((s["copied"], s["scan"]), (1, "phone"))

    def test_scan_id_passed_to_later_pages(self):
        from app.services.backup import phone_scanner
        pages = [{"entries": [["a", 1, 1]], "next": 1, "scan_id": "w42"}, {"entries": [["b", 1, 1]], "next": None}]
        be = FakeRelayStorage(pages=pages)
        seen = []
        orig = be.scan

        async def spy(rid, path, page, scan_id=None):
            seen.append((page, scan_id))
            return await orig(rid, path, page)
        be.scan = spy
        run(phone_scanner(be, "rid")(self.ROOT))
        self.assertEqual(seen, [(0, None), (1, "w42")])

    def test_runaway_paging_stops(self):
        from app.services.backup import phone_scanner
        from app.services.storage_service import StorageError
        be = FakeRelayStorage(pages=None)

        async def endless(rid, path, page, scan_id=None):
            return {"entries": [], "next": page + 1}
        be.scan = endless
        with self.assertRaises(StorageError):
            run(phone_scanner(be, "rid")(self.ROOT))

    def test_no_scan_env_forces_walk(self):
        from app.services.backup import phone_scanner
        be = FakeRelayStorage({f"{self.ROOT}/a.jpg": (b"abc", 1700000000)})
        os.environ["ACC_BACKUP_NO_SCAN"] = "1"
        try:
            from app.services.backup import ScanUnsupported
            with self.assertRaises(ScanUnsupported):
                run(phone_scanner(be, "rid")(self.ROOT))
        finally:
            os.environ.pop("ACC_BACKUP_NO_SCAN")
        self.assertEqual(be.scans, 0)


class FakeDev:
    def __init__(self, id, serial, name, relay_id=None, transport="adb", status="online", brand="realme", model="RMX3031"):
        self.id, self.serial, self.name, self.relay_id = id, serial, name, relay_id
        self.brand, self.model = brand, model
        self.transport, self.status = transport, status


class FakeDM:
    def __init__(self, devs):
        self.devices = {d.id: d for d in devs}

    def get_device(self, i):
        return self.devices.get(i)


class FakeRelays:
    def __init__(self, online=()):
        self.online = set(online)

    def get(self, rid):
        return object() if rid in self.online else None


class TestService(_Root):
    def service(self, devs, usb_files, relay_files=None, online=()):
        from app.services.backup import BackupService
        self.sent = []

        async def broadcast(msg):
            self.sent.append(msg)
        self.usb = FakeBackend(usb_files)
        self.rel = FakeRelayStorage(relay_files or {})
        return BackupService(FakeDM(devs), self.usb, self.rel, FakeRelays(online), broadcast)

    def test_two_phones_parallel_separate_folders(self):
        a = FakeDev("realme_a", "SERIAL_A", "Realme RMX3031", relay_id="android_rmx3031_aaaaaaaa11")
        b = FakeDev("android_rmx3031_bbbbbbbb22", "android_rmx3031_bbbbbbbb22", "Realme RMX3031",
                    relay_id="android_rmx3031_bbbbbbbb22", transport="companion_ws")
        svc = self.service([a, b], {"/sdcard/Download/a.txt": (b"from A", 1)},
                           {"/sdcard/Download/b.txt": (b"from B", 2)}, online=[b.relay_id])

        async def go():
            for d in (a, b):
                svc.set_selection(d.id, {"presets": ["download"], "custom": []})
                svc.start(d.id)
            await asyncio.gather(*svc.tasks.values())
        run(go())
        da, db = svc.store_for(a).dir, svc.store_for(b).dir
        self.assertNotEqual(da, db)
        self.assertEqual((da / "Download/a.txt").read_bytes(), b"from A")
        self.assertEqual((db / "Download/b.txt").read_bytes(), b"from B")
        self.assertFalse((da / "Download/b.txt").exists())
        self.assertFalse((db / "Download/a.txt").exists())
        self.assertTrue(any(m["type"] == "BACKUP_PROGRESS" for m in self.sent))

    def test_start_twice_is_noop(self):
        a = FakeDev("realme_a", "SERIAL_A", "Realme", relay_id="android_x_aaaaaaaa11")
        svc = self.service([a], {"/sdcard/Download/a.txt": (b"x", 1)})

        async def go():
            svc.set_selection(a.id, {"presets": ["download"], "custom": []})
            svc.start(a.id)
            first = dict(svc.tasks)
            st = svc.start(a.id)
            self.assertEqual(svc.tasks, first)
            self.assertTrue(st["running"])
            await asyncio.gather(*svc.tasks.values())
        run(go())

    def test_offline_run_409(self):
        from app.services.backup import BackupError
        a = FakeDev("r", "r", "Remote", relay_id="android_x_aaaaaaaa11", transport="companion_ws", status="offline")
        svc = self.service([a], {})
        svc.set_selection(a.id, {"presets": ["download"], "custom": []})
        with self.assertRaises(BackupError) as cm:
            run(self._start(svc, a.id))
        self.assertEqual(cm.exception.status, 409)

    async def _start(self, svc, i):
        return svc.start(i)

    def test_viewing_does_not_claim_folder_and_name_is_brand_model(self):
        a = FakeDev("realme_a", "SERIAL_A", "Realme RMX3031 (SERIAL...)", relay_id="android_rmx3031_8fc4f39077")
        svc = self.service([a], {})
        svc.phones()
        self.assertFalse((self.root / ".phones.json").exists())
        svc.set_selection(a.id, {"presets": ["camera"], "custom": []})
        self.assertEqual(svc.store_for(a).dir.name, "Realme RMX3031 · 8fc4f390")
        self.assertTrue((self.root / ".phones.json").exists())

    def test_same_folder_before_and_after_relay_id_known(self):
        a = FakeDev("realme_a", "SERIAL_A", "Realme", relay_id=None)
        svc = self.service([a], {})
        svc.set_selection(a.id, {"presets": ["camera"], "custom": []})
        first = svc.store_for(a).dir
        a.relay_id = "android_rmx3031_8fc4f39077"
        self.assertEqual(svc.store_for(a).dir, first)
        self.assertEqual(svc.store_for(a).load_selection()["presets"], ["camera"])

    def test_same_folder_when_relay_id_temporarily_unreadable(self):
        a = FakeDev("realme_a", "SERIAL_A", "Realme", relay_id="android_rmx3031_8fc4f39077")
        svc = self.service([a], {})
        svc.set_selection(a.id, {"presets": ["camera"], "custom": []})
        first = svc.store_for(a).dir
        a.relay_id = None
        self.assertEqual(svc.store_for(a).dir, first)

    def test_add_custom_validates(self):
        from app.services.backup import BackupError
        a = FakeDev("realme_a", "SERIAL_A", "Realme", relay_id="android_x_aaaaaaaa11")
        svc = self.service([a], {})
        sel = svc.add_custom(a.id, ["/sdcard/Notes", "/sdcard/Notes"])
        self.assertEqual(sel["custom"], ["/sdcard/Notes"])
        with self.assertRaises(BackupError):
            svc.add_custom(a.id, ["relative/path"])
        with self.assertRaises(BackupError):
            svc.set_selection(a.id, {"presets": ["nope"], "custom": []})


class TestAdbStream(unittest.TestCase):
    """USB unplug: adb's process is already gone when the stream is closed."""

    def test_dead_adb_process_does_not_raise(self):
        from unittest import mock
        from app.services.storage_service import StorageService

        class Out:
            def __init__(self):
                self.chunks = [b"abc", b""]

            async def read(self, n):
                return self.chunks.pop(0)

        class Proc:
            returncode = None
            stdout = Out()

            def kill(self):
                raise ProcessLookupError()

            async def wait(self):
                return 0

        async def fake_exec(*a, **k):
            return Proc()

        async def go(start):
            got = b""
            async for c in StorageService().read_range("S", "/sdcard/x", start, 10):
                got += c
            return got
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            self.assertEqual(run(go(0)), b"abc")
            Proc.stdout = Out()
            self.assertEqual(len(run(go(70000))), 0)  # dd path: first 4464 bytes dropped from a 3-byte read

    def test_timed_out_adb_whose_process_died_reports_504(self):
        from unittest import mock
        from app.services.storage_service import StorageService, StorageError

        class Proc:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(5)

            def kill(self):
                raise ProcessLookupError()

            async def wait(self):
                return 0

        async def fake_exec(*a, **k):
            return Proc()
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            with self.assertRaises(StorageError) as cm:
                run(StorageService()._exec(["shell", "true"], 0.01))
        self.assertEqual(cm.exception.status, 504)


if __name__ == "__main__":
    unittest.main()
