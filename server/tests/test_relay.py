import json
import tempfile
import unittest
from pathlib import Path


class TestRefusedPhones(unittest.TestCase):
    def setUp(self):
        from app.services import relay
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = relay.BINDINGS_FILE
        relay.BINDINGS_FILE = Path(self._tmp.name) / "b.json"
        self.r = relay.RelayRegistry()

    def tearDown(self):
        from app.services import relay
        relay.BINDINGS_FILE = self._orig
        self._tmp.cleanup()

    def lock_out(self, rid):
        ok, sec = self.r.authorize(rid, None)
        self.r.confirm(rid)
        self.assertFalse(self.r.authorize(rid, None)[0])  # reinstalled app: no secret any more

    def test_refused_phones_listed(self):
        self.lock_out("android_v2130_9751b166a3744f9c")
        self.assertEqual([p["relay_id"] for p in self.r.refused_phones()], ["android_v2130_9751b166a3744f9c"])

    def test_forget_clears_refused_and_phone_can_bind_again(self):
        rid = "android_v2130_9751b166a3744f9c"
        self.lock_out(rid)
        self.r.forget(rid)
        self.assertEqual(self.r.refused_phones(), [])
        ok, new = self.r.authorize(rid, None)
        self.assertTrue(ok and new)


if __name__ == "__main__":
    unittest.main()
