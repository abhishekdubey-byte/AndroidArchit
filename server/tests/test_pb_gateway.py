import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

from app.services.pb_gateway import PBGateway
from app.services.powerboosters import PowerBoosters
from app.services.relay import pack, unpack


class FakeWS:
    def __init__(self):
        self.sent, self.bins = [], []

    async def send_json(self, m):
        self.sent.append(m)

    async def send_bytes(self, b):
        self.bins.append(b)

    def last(self, t):
        return next(m for m in reversed(self.sent) if m["type"] == t)


class FakeClient:
    def __init__(self, ws, rid):
        self.ws, self.relay_id = ws, rid


class FakeRelays:
    def __init__(self):
        self.clients, self.bindings = {}, {}

    def get(self, rid):
        return self.clients.get(rid)

    def for_socket(self, ws):
        return next((c for c in self.clients.values() if c.ws is ws), None)


class FakeHub:
    def __init__(self):
        self.device_clients, self.dash = {}, []

    async def broadcast_to_dashboards(self, m):
        self.dash.append(m)


class FakeDM:
    def get_all_devices(self):
        return []

    def add_diagnostic_report(self, r):
        self.report = r


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.relays, self.hub = FakeRelays(), FakeHub()
        self.gw = PBGateway(PowerBoosters(d / "pb.db", d / "f"), self.hub, FakeDM(), self.relays)
        self.a, self.b = FakeWS(), FakeWS()
        for rid, ws in (("A", self.a), ("B", self.b)):
            self.relays.clients[rid] = FakeClient(ws, rid)
            self.hub.device_clients[rid] = ws

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, coro):
        return asyncio.run(coro)

    def phone(self, ws, rid, t, **p):
        self.run_(self.gw.handle_phone(ws, rid, {"type": t, "payload": p}))

    def test_full_mission_flow_over_socket(self):
        self.phone(self.a, "A", "PB_SYNC")
        self.phone(self.b, "B", "PB_SYNC")
        m = self.gw.pb.save_mission({"title": "Photo run", "allowed_formats": "jpg", "xp_reward": 50})
        self.gw.pb.publish(m["id"], self.gw.registered_devices())
        self.run_(self.gw.after_publish(m["id"]))
        aid = self.a.last("PB_ASSIGNMENT")["payload"]["id"]
        self.phone(self.a, "A", "PB_ACCEPT", assignment_id=aid)
        # B cannot touch A's assignment
        self.phone(self.b, "B", "PB_ACCEPT", assignment_id=aid)
        self.assertEqual(self.b.last("PB_ERROR")["payload"]["status"], 404)
        # resumable chunked upload over binary frames
        data = b"\xff\xd8" + b"x" * 300000
        self.phone(self.a, "A", "PB_UPLOAD_INIT", upload_id="upl0000001", name="shot.jpg", mime="image/jpeg",
                   size=len(data), sha256=hashlib.sha256(data).hexdigest(), purpose="submission", assignment_id=aid)
        st = self.a.last("PB_UPLOAD_STATUS")["payload"]
        while st["status"] != "ready":
            off = st["received"]
            frame = pack({"type": "PB_CHUNK", "file_id": st["file_id"], "offset": off, "upload_id": "upl0000001"}, data[off:off + 131072])
            self.assertTrue(self.run_(self.gw.handle_chunk(self.a, "A", frame)))
            st = self.a.last("PB_UPLOAD_STATUS")["payload"]
        # a disallowed format is refused at init
        self.phone(self.a, "A", "PB_UPLOAD_INIT", upload_id="upl0000002", name="x.exe", size=3, purpose="submission", assignment_id=aid)
        self.assertEqual(self.a.last("PB_ERROR")["payload"]["status"], 415)
        # B cannot pull A's file
        self.phone(self.b, "B", "PB_FILE_PULL", file_id=st["file_id"])
        self.assertEqual(self.b.last("PB_ERROR")["payload"]["status"], 404)
        self.phone(self.a, "A", "PB_SUBMIT", client_id="sub1", assignment_id=aid, text="done", files=[st["file_id"]])
        sub = self.a.last("PB_SUBMIT_OK")["payload"]["submission"]
        self.phone(self.a, "A", "PB_SUBMIT", client_id="sub1", assignment_id=aid, text="done", files=[st["file_id"]])  # retry
        self.assertEqual(len([d for d in self.hub.dash if d.get("event") == "submission"]), 1)
        res = self.gw.pb.review(sub["id"], "approve", "Commander@test")
        self.run_(self.gw.after_review(res))
        self.assertEqual(self.a.last("PB_REWARD")["payload"]["xp"], 50)
        self.assertEqual(self.a.last("PB_PROFILE")["payload"]["profile"]["xp"], 50)
        # phone downloads its own file back in chunks
        self.phone(self.a, "A", "PB_FILE_PULL", file_id=st["file_id"], offset=0)
        h, body = unpack(self.a.bins[-1])
        self.assertEqual((h["type"], h["offset"], body[:2]), ("PB_FILE_CHUNK", 0, b"\xff\xd8"))

    def test_chat_privacy_and_receipts(self):
        self.phone(self.a, "A", "PB_MSG_SEND", client_id="m1", text="hello HQ")
        self.phone(self.a, "A", "PB_MSG_SEND", client_id="m1", text="hello HQ")  # offline retry
        self.assertEqual(len(self.gw.pb.messages("A")), 1)
        self.assertEqual(self.gw.pb.messages("B"), [])
        m = self.gw.pb.send_message("A", "commander", "roger")
        self.run_(self.gw.commander_message("A", m))
        self.assertEqual(self.a.last("PB_MSG")["payload"]["text"], "roger")
        self.assertFalse(any(x["type"] == "PB_MSG" for x in self.b.sent))
        self.phone(self.a, "A", "PB_READ", seq=m["seq"])
        self.assertTrue(self.gw.pb.message(m["id"])["read"])


if __name__ == "__main__":
    unittest.main()
