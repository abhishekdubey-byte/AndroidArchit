import hashlib
import tempfile
import unittest
from pathlib import Path

from app.services.powerboosters import PBError, PowerBoosters, level_for


class TestPowerBoosters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.pb = PowerBoosters(d / "pb.db", d / "files")
        for dev in ("A", "B"):
            self.pb.ensure_member(dev, dev)

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, dev, data=b"hello world", name="p.jpg", purpose="submission", uid="upload0001"):
        f = self.pb.upload_init(uploader=dev, owner_device=dev, purpose=purpose, name=name, mime="image/jpeg",
                                size=len(data), sha256=hashlib.sha256(data).hexdigest(), upload_id=uid)
        f = self.pb.upload_chunk(f["id"], dev, 0, data[:4])
        f = self.pb.upload_chunk(f["id"], dev, 99, b"x")  # out of order: ignored, resend from received
        self.assertEqual(f["received"], 4)
        f = self.pb.upload_chunk(f["id"], dev, 4, data[4:])
        self.assertEqual(f["status"], "ready")
        return f["id"]

    def mission(self, **kw):
        m = self.pb.save_mission({"title": "Scout", "xp_reward": 100, "max_attempts": 3, **kw})
        self.pb.publish(m["id"], ["A", "B"])
        return m["id"]

    def test_broadcast_independent_assignments_and_targeted_privacy(self):
        mid = self.mission()
        self.assertEqual(len(self.pb.mission(mid)["assignments"]), 2)
        t = self.pb.save_mission({"title": "Secret", "distribution": "targeted", "targets": ["A"]})
        self.pb.publish(t["id"], ["A", "B"])
        self.assertEqual([a["mission"]["title"] for a in self.pb.assignments_for("B")], ["Scout"])
        a_secret = next(a for a in self.pb.assignments_for("A") if a["mission"]["title"] == "Secret")
        with self.assertRaises(PBError):
            self.pb.assignment(a_secret["id"], "B")

    def test_reject_resubmit_approve_pays_once(self):
        mid = self.mission()
        a = next(x for x in self.pb.assignments_for("A") if x["mission_id"] == mid)
        f1 = self.upload("A")
        s1 = self.pb.submit("A", a["id"], "c1", "done", "", [f1])
        self.assertEqual(self.pb.xp_of("A"), 0)  # never before approval
        with self.assertRaises(PBError):
            self.pb.review(s1["id"], "reject", "cmd", "")  # reason required
        self.pb.review(s1["id"], "reject", "cmd", "Missing photos")
        self.assertEqual(self.pb.assignment(a["id"])["status"], "revision_required")
        self.assertEqual(self.pb.submit("A", a["id"], "c1", "x", "", [])["id"], s1["id"])  # retried submit is idempotent
        s2 = self.pb.submit("A", a["id"], "c2", "fixed", "", [f1])
        self.assertEqual(s2["attempt"], 2)
        r = self.pb.review(s2["id"], "approve", "cmd", bonus_xp=25)
        self.assertEqual(r["xp_total"], 125)
        again = self.pb.review(s2["id"], "approve", "cmd")
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.pb.xp_of("A"), 125)
        self.assertEqual(len(self.pb.submissions_for(a["id"])), 2)  # history preserved
        keys = {b["key"] for b in self.pb.profile("A")["achievements"] if b["unlocked_at"]}
        self.assertIn("first_mission", keys)
        self.assertNotIn("perfect_submission", keys)

    def test_attempt_limit_and_revision_keeps_attempt(self):
        mid = self.mission(max_attempts=1)
        a = next(x for x in self.pb.assignments_for("B") if x["mission_id"] == mid)
        s = self.pb.submit("B", a["id"], "k1", "t", "", [])
        self.pb.review(s["id"], "revision", "cmd", "More detail")
        s2 = self.pb.submit("B", a["id"], "k2", "t2", "", [])
        self.pb.review(s2["id"], "reject", "cmd", "No")
        self.assertEqual(self.pb.assignment(a["id"])["status"], "failed")

    def test_other_device_cannot_use_or_read_files(self):
        fid = self.upload("A", purpose="chat")
        with self.assertRaises(PBError):
            self.pb.send_message("B", "member", "hi", "m1", attachment_ids=[fid])
        self.assertFalse(self.pb.can_device_read("B", fid))
        m = self.pb.send_message("A", "member", "hi", "m1", attachment_ids=[fid])
        self.assertEqual(self.pb.send_message("A", "member", "hi", "m1")["id"], m["id"])
        self.assertEqual(self.pb.messages("B"), [])

    def test_checksum_mismatch_restarts(self):
        f = self.pb.upload_init(uploader="A", owner_device="A", purpose="chat", name="a.txt", mime="text/plain",
                                size=3, sha256="0" * 64, upload_id="badsum0001")
        with self.assertRaises(PBError):
            self.pb.upload_chunk(f["id"], "A", 0, b"abc")
        self.assertEqual(self.pb.file(f["id"])["received"], 0)

    def test_manual_bonus_and_levels(self):
        self.pb.manual_bonus("A", 300, "Great help", "cmd", "r1")
        self.pb.manual_bonus("A", 300, "Great help", "cmd", "r1")  # same request id
        self.assertEqual(self.pb.xp_of("A"), 300)
        self.assertEqual(level_for(300)["level"], 3)
        self.pb.set_username("A", "alpha_one")
        self.pb.set_policy({"demo_squad": 0})
        self.assertEqual(self.pb.leaderboard("weekly")[0]["device_id"], "A")

    def test_usernames_and_demo_leaderboard(self):
        self.pb.set_username("A", "arjun_test")
        with self.assertRaises(PBError):
            self.pb.set_username("B", "ARJUN_TEST")  # unique, case-insensitive
        with self.assertRaises(PBError):
            self.pb.set_username("B", "x")
        demo = self.pb._one("SELECT username FROM pb_demo_players LIMIT 1")["username"]
        with self.assertRaises(PBError):
            self.pb.set_username("B", demo)
        board = self.pb.leaderboard("global", 25, "A")
        self.assertEqual(self.pb._one("SELECT COUNT(*) n FROM pb_demo_players")["n"], 370)
        self.assertEqual(board[-1]["name"], "arjun_test")  # "me" appended even when outside the top 25
        self.assertNotIn("B", [r["device_id"] for r in self.pb.leaderboard("global", 500)])  # no username = not listed
        self.assertEqual(self.pb.ledger(), [])  # demo players never touch the XP ledger
        self.pb.set_policy({"demo_squad": 0})
        self.assertEqual([r["name"] for r in self.pb.leaderboard()], ["arjun_test"])


if __name__ == "__main__":
    unittest.main()
