"""Phase 3 review fixes: recorder timing/close/rotation/auto-pick, hub camera stream lifecycle."""
import asyncio
import json
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.h264_recorder import AutoRecorder, H264Recorder, split_access_units
from app.websocket.hub import WebSocketHub

FIXTURES = Path(__file__).parent / "fixtures"
FFMPEG = shutil.which("ffmpeg")
UNITS = None


def units():
    global UNITS
    if UNITS is None:
        UNITS = split_access_units((FIXTURES / "cam_720p_10s.h264").read_bytes())
    return UNITS


def probe(path, entries, stream="v:0"):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", entries, "-of", "json", path],
                         capture_output=True, text=True).stdout
    return json.loads(out)


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class TestRecorderTiming(unittest.TestCase):
    def test_paced_small_frames_keep_their_spacing(self):
        with tempfile.TemporaryDirectory() as d:
            rec = H264Recorder("Phone", folder=Path(d))
            for kind, data in units()[:61]:                 # config + 60 frames, sent like a live stream
                rec.submit_video(kind, 0, data)
                if kind != 1:
                    time.sleep(1 / 30)
            path = rec.close()
            pts = [float(p["pts_time"]) for p in
                   probe(path, "packet=pts_time")["packets"] if p.get("pts_time") not in (None, "N/A")]
            pts.sort()
            gaps = [b - a for a, b in zip(pts, pts[1:])]
            clumped = sum(1 for g in gaps if g < 0.005)
            self.assertLess(clumped, len(gaps) * 0.2, f"{clumped}/{len(gaps)} frames arrived in clumps")

    def test_close_with_audio_stays_fast_for_long_clips(self):
        with tempfile.TemporaryDirectory() as d:
            rec = H264Recorder("Phone", folder=Path(d))
            seq = units()
            for _ in range(6):                               # ~60 s of video
                for kind, data in seq:
                    rec.submit_video(kind, 0, data)
            rec.t0_for_tests = True
            pcm = b"\x01\x00" * 1600                        # 100 ms @ 16 kHz
            time.sleep(0.5)
            for _ in range(600):                             # ~60 s of audio, encoded while it arrives
                rec.submit_audio(pcm, 16000)
            time.sleep(1.5)
            t = time.perf_counter()
            path = rec.close()
            took = time.perf_counter() - t
            self.assertIsNotNone(path)
            self.assertLess(took, 0.5)
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                                 capture_output=True, text=True).stdout.split()
            self.assertEqual(sorted(out), ["aac", "h264"])


class FakeInner:
    made = []

    def __init__(self, name, folder=None, rotation=0, **kw):
        self.rotation, self.video, self.audio, self.jpeg, self.closed = rotation, [], [], [], False
        FakeInner.made.append(self)

    def submit_video(self, kind, pts, data):
        self.video.append(kind)

    def submit_audio(self, pcm, rate=16000):
        self.audio.append(pcm)

    def submit(self, jpeg):
        self.jpeg.append(jpeg)

    def close(self):
        self.closed = True
        return f"/rec/{len(FakeInner.made)}-{id(self)}.mp4"


class TestAutoRecorder(unittest.TestCase):
    def setUp(self):
        FakeInner.made = []

    def make(self, **kw):
        return AutoRecorder("Phone", Path("."), h264_factory=FakeInner, jpeg_factory=FakeInner, **kw)

    def test_save_before_first_packet_picks_h264(self):
        rec = self.make(config=b"CFG", rotation=90)
        rec.submit_audio(b"a", 16000)                       # audio may come first
        rec.submit_video(2, 1, b"K")
        inner = FakeInner.made[0]
        self.assertEqual(inner.video, [1, 2])               # cached config first, then the key frame
        self.assertEqual(inner.audio, [b"a"])
        self.assertEqual(inner.rotation, 90)

    def test_jpeg_stream_picks_jpeg_recorder(self):
        rec = self.make()
        rec.submit(b"jpeg")
        self.assertEqual(FakeInner.made[0].jpeg, [b"jpeg"])

    def test_lens_switch_rolls_over_to_an_upright_second_file(self):
        rec = self.make(config=b"CFG", rotation=90)
        rec.submit_video(2, 1, b"K")
        rec.submit_video(3, 2, b"D")
        rec.set_rotation(270)                               # front camera
        rec.submit_video(1, 0, b"CFG2")
        rec.submit_video(2, 3, b"K2")
        paths = rec.close_all()
        self.assertEqual(len(FakeInner.made), 2)
        self.assertEqual((FakeInner.made[0].rotation, FakeInner.made[1].rotation), (90, 270))
        self.assertTrue(all(i.closed for i in FakeInner.made))
        self.assertEqual(len(paths), 2)


class WS:
    def __init__(self):
        self.json, self.binary = [], []

    async def send_json(self, m):
        self.json.append(m)

    async def send_bytes(self, b):
        self.binary.append(b)


def frame(header, payload=b"x"):
    h = json.dumps(header).encode()
    return struct.pack(">H", len(h)) + h + payload


def make_hub():
    dev = SimpleNamespace(id="ph", serial="s", name="P", transport="adb", status="online", relay_id="rid")
    dm = SimpleNamespace(add_listener=lambda fn: None, get_device=lambda i: dev, get_all_devices=lambda: [dev])
    hub = WebSocketHub(dm, SimpleNamespace(adb_path="adb"), SimpleNamespace(add_listener=lambda fn: None))
    phone = WS()
    hub.relays = SimpleNamespace(for_socket=lambda ws: SimpleNamespace(relay_id="rid") if ws is phone else None,
                                 get=lambda rid: phone)
    hub._get_companion_socket = lambda d: phone
    return hub, phone


async def viewer(hub, h264=True):
    ws = WS()
    hub.dashboard_clients.add(ws)
    await hub._process_dashboard_message(ws, {"type": "CLIENT_CAPS", "payload": {"h264": h264}})
    return ws


async def start(hub, ws):
    await hub._process_dashboard_message(ws, {"type": "START_CAMERA_STREAM", "device_id": "ph", "payload": {"lens": "back"}})


class TestHubCameraLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_jpeg_viewer_first_keeps_jpeg_for_everyone(self):
        hub, phone = make_hub()
        await start(hub, await viewer(hub, h264=False))
        await start(hub, await viewer(hub, h264=True))
        self.assertEqual([m["payload"]["codec"] for m in phone.json if m["type"] == "START_CAMERA_STREAM"], ["jpeg", "jpeg"])

    async def test_newcomer_gets_current_camera_state(self):
        hub, phone = make_hub()
        await start(hub, await viewer(hub))
        await hub._process_dashboard_message(phone, {"type": "CAMERA_STREAM_STATE", "device_id": "rid",
                                                     "payload": {"active": True, "codec": "h264", "rotation": 90, "lens": "back"}})
        late = await viewer(hub)
        await start(hub, late)
        states = [m for m in late.json if m["type"] == "CAMERA_STREAM_STATE"]
        self.assertTrue(states and states[-1]["payload"]["rotation"] == 90)

    async def test_no_stale_picture_after_the_camera_stops(self):
        hub, phone = make_hub()
        ws = await viewer(hub)
        await start(hub, ws)
        await hub.handle_device_binary(phone, frame({"type": "CAMERA_PKT", "k": 1, "pts": 0}, b"CFG"))
        await hub.handle_device_binary(phone, frame({"type": "CAMERA_PKT", "k": 2, "pts": 1}, b"K"))
        await hub._process_dashboard_message(ws, {"type": "STOP_CAMERA_STREAM", "device_id": "ph"})
        self.assertNotIn("ph#cam", hub.fanout._config)
        self.assertNotIn("ph", hub._cam_config)

    async def test_oversized_camera_packet_dropped(self):
        hub, phone = make_hub()
        ws = await viewer(hub)
        await start(hub, ws)
        self.assertTrue(await hub.handle_device_binary(phone, frame({"type": "CAMERA_PKT", "k": 1, "pts": 0}, b"x" * (3 << 20))))
        self.assertNotIn("ph#cam", hub.fanout._config)

    async def test_malformed_pts_does_not_raise(self):
        hub, phone = make_hub()
        raw = b'{"type":"CAMERA_PKT","k":2,"pts":Infinity}'
        self.assertTrue(await hub.handle_device_binary(phone, struct.pack(">H", len(raw)) + raw + b"x"))

    async def test_last_viewer_closing_tab_stops_the_phone_camera(self):
        hub, phone = make_hub()
        ws = await viewer(hub)
        await start(hub, ws)
        await hub.dashboard_left(ws)
        self.assertIn("STOP_CAMERA_STREAM", [m["type"] for m in phone.json])


if __name__ == "__main__":
    unittest.main()
