"""Phase 3: H.264 camera packets from the phone → fan-out, and the instant-save H.264 recorder."""
import asyncio
import json
import re
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.h264_recorder import H264Recorder, split_access_units
from app.services.media_packets import KIND_CONFIG, KIND_DELTA, KIND_KEY, unpack
from app.websocket.hub import WebSocketHub, parse_device_binary

FIXTURES = Path(__file__).parent / "fixtures"
FFMPEG = shutil.which("ffmpeg")


def device_frame(header: dict, payload: bytes) -> bytes:
    h = json.dumps(header).encode()
    return struct.pack(">H", len(h)) + h + payload


class TestParseDeviceBinary(unittest.TestCase):
    def test_parse(self):
        hdr, payload = parse_device_binary(device_frame({"type": "CAMERA_PKT", "k": 2, "pts": 33}, b"\x00\x00\x00\x01e"))
        self.assertEqual(hdr, {"type": "CAMERA_PKT", "k": 2, "pts": 33})
        self.assertEqual(payload, b"\x00\x00\x00\x01e")

    def test_garbage(self):
        self.assertEqual(parse_device_binary(b"\x00"), (None, b""))
        self.assertEqual(parse_device_binary(b"\x00\x05notjs"), (None, b""))


class FakeWS:
    def __init__(self):
        self.binary, self.json = [], []

    async def send_bytes(self, data):
        self.binary.append(unpack(data))

    async def send_json(self, m):
        self.json.append(m)


def make_hub():
    dev = SimpleNamespace(id="xiaomi", serial="s", name="P", transport="adb", status="online", relay_id="rid")
    dm = SimpleNamespace(add_listener=lambda fn: None, get_device=lambda i: dev, get_all_devices=lambda: [dev])
    hub = WebSocketHub(dm, SimpleNamespace(adb_path="adb"), SimpleNamespace(add_listener=lambda fn: None))
    phone = FakeWS()
    hub.relays = SimpleNamespace(for_socket=lambda ws: SimpleNamespace(relay_id="rid") if ws is phone else None,
                                 get=lambda rid: phone if rid == "rid" else None)
    hub.device_clients["rid"] = phone
    hub._get_companion_socket = lambda d: phone
    return hub, phone


class TestHubCameraPackets(unittest.IsolatedAsyncioTestCase):
    async def test_camera_packets_published_on_cam_stream(self):
        hub, phone = make_hub()
        viewer = FakeWS()
        hub.dashboard_clients.add(viewer)
        hub.fanout.add(viewer, True)
        for k, pts in ((1, 0), (2, 1000), (3, 34000)):
            self.assertTrue(await hub.handle_device_binary(phone, device_frame({"type": "CAMERA_PKT", "k": k, "pts": pts}, b"x")))
        await hub.fanout.flush()
        self.assertEqual([(b[0], b[1], b[2]) for b in viewer.binary],
                         [(KIND_CONFIG, "xiaomi#cam", 0), (KIND_KEY, "xiaomi#cam", 1000), (KIND_DELTA, "xiaomi#cam", 34000)])

    async def test_other_binary_frames_not_consumed(self):
        hub, phone = make_hub()
        self.assertFalse(await hub.handle_device_binary(phone, device_frame({"type": "FS_RES", "req_id": "1"}, b"data")))

    async def test_start_stream_asks_for_h264_only_when_browser_can_decode(self):
        hub, phone = make_hub()
        modern, legacy = FakeWS(), FakeWS()
        for ws, h264 in ((modern, True), (legacy, False)):
            hub.dashboard_clients.add(ws)
            hub.fanout.add(ws, h264)
        await hub._process_dashboard_message(modern, {"type": "START_CAMERA_STREAM", "device_id": "xiaomi", "payload": {"lens": "back"}})
        await hub._process_dashboard_message(legacy, {"type": "START_CAMERA_STREAM", "device_id": "xiaomi", "payload": {"lens": "back"}})
        sent = [m["payload"].get("codec") for m in phone.json if m["type"] == "START_CAMERA_STREAM"]
        self.assertEqual(sent, ["h264", "jpeg"])

    async def test_recording_gets_h264_packets(self):
        hub, phone = make_hub()
        rec = SimpleNamespace(video=[], submit_video=lambda k, p, d: rec.video.append((k, p)), submit=lambda b: None)
        hub.cam_recorders["xiaomi"] = rec
        await hub.handle_device_binary(phone, device_frame({"type": "CAMERA_PKT", "k": 2, "pts": 5}, b"k"))
        self.assertEqual(rec.video, [(2, 5)])


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class TestH264Recorder(unittest.TestCase):
    def feed(self, rec, stream: bytes):
        for kind, data in split_access_units(stream):
            rec.submit_video(kind, 0, data)

    def probe(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-show_entries",
                              "stream=codec_name,width,height,nb_read_packets", "-of", "json", path],
                             capture_output=True, text=True).stdout
        return json.loads(out)["streams"][0]

    def test_split_access_units(self):
        units = split_access_units((FIXTURES / "cam_720p_10s.h264").read_bytes())
        kinds = [k for k, _ in units]
        self.assertEqual(kinds[0], KIND_CONFIG)
        self.assertEqual(kinds.count(KIND_KEY), 5)                 # -g 60 over 300 frames
        self.assertEqual(kinds.count(KIND_KEY) + kinds.count(KIND_DELTA), 300)

    def test_saves_instantly_and_keeps_every_frame(self):
        with tempfile.TemporaryDirectory() as d:
            rec = H264Recorder("Phone (test)", folder=Path(d))
            self.feed(rec, (FIXTURES / "cam_720p_10s.h264").read_bytes())
            time.sleep(1.0)                                         # ffmpeg has been writing as frames arrived
            t = time.perf_counter()
            path = rec.close()
            took = time.perf_counter() - t
            self.assertIsNotNone(path)
            self.assertLess(took, 0.5)
            s = self.probe(path)
            self.assertEqual((s["codec_name"], s["width"], s["height"]), ("h264", 1280, 720))
            self.assertEqual(int(s["nb_read_packets"]), 300)

    def test_waits_for_first_key_frame(self):
        with tempfile.TemporaryDirectory() as d:
            rec = H264Recorder("Phone", folder=Path(d))
            units = split_access_units((FIXTURES / "cam_720p_10s.h264").read_bytes())
            config = next(data for k, data in units if k == KIND_CONFIG)
            rec.submit_video(KIND_CONFIG, 0, config)
            joined_late = units[30:]                                # starts mid-group: deltas before the next key
            for kind, data in joined_late:
                rec.submit_video(kind, 0, data)
            path = rec.close()
            self.assertEqual(int(self.probe(path)["nb_read_packets"]), 300 - 60)   # from the 2nd key frame on

    def test_audio_muxed_without_reencoding_video(self):
        with tempfile.TemporaryDirectory() as d:
            rec = H264Recorder("Phone", folder=Path(d))
            self.feed(rec, (FIXTURES / "cam_720p_10s.h264").read_bytes())
            for _ in range(20):
                rec.submit_audio(b"\x00\x01" * 1600, 16000)
            path = rec.close()
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                                 capture_output=True, text=True).stdout.split()
            self.assertEqual(sorted(out), ["aac", "h264"])

    def test_close_without_frames_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(H264Recorder("Phone", folder=Path(d)).close())


if __name__ == "__main__":
    unittest.main()
