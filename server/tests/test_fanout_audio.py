"""Phase 4: the audio track in the fan-out has its own cache and lane."""
import asyncio
import unittest

from app.services.media_packets import KIND_AUDIO, KIND_AUDIO_CONFIG, KIND_CONFIG, KIND_KEY, unpack
from app.services.scrcpy_session import VideoPacket
from app.websocket.media_fanout import AUDIO_QUEUE_MAX, MediaFanout

ASC = b"\x11\x88"


class FakeWS:
    def __init__(self, gate: asyncio.Event = None):
        self.gate = gate
        self.got = []

    async def send_bytes(self, data):
        if self.gate is not None:
            await self.gate.wait()
        self.got.append(unpack(data))


def cfg():
    return VideoPacket(0, True, False, b"\x00\x00\x00\x01\x67cfg")


def key(pts):
    return VideoPacket(pts, False, True, b"K%d" % pts)


def delta(pts):
    return VideoPacket(pts, False, False, b"D%d" % pts)


class TestFanoutAudio(unittest.IsolatedAsyncioTestCase):
    async def test_late_joiner_gets_audio_config_before_frames(self):
        f = MediaFanout()
        await f.publish_audio("d#mic", KIND_AUDIO_CONFIG, 0, ASC)
        for i in range(5):
            await f.publish_audio("d#mic", KIND_AUDIO, 1000 + i, b"A%d" % i)
        ws = FakeWS()
        f.add(ws, True)
        f.subscribe(ws, "d#mic")
        await f.publish_audio("d#mic", KIND_AUDIO, 2000, b"A")
        await f.flush()
        kinds = [k for k, *_ in ws.got]
        self.assertEqual(kinds[0], KIND_AUDIO_CONFIG)
        self.assertEqual(kinds[1:], [KIND_AUDIO])
        self.assertEqual(ws.got[0][3], ASC)

    async def test_audio_without_config_is_held_back(self):
        f = MediaFanout()
        ws = FakeWS()
        f.add(ws, True)
        f.subscribe(ws, "d#mic")
        await f.publish_audio("d#mic", KIND_AUDIO, 1, b"A")       # no config yet: undecodable, not sent
        await f.publish_audio("d#mic", KIND_AUDIO_CONFIG, 2, ASC)
        await f.publish_audio("d#mic", KIND_AUDIO, 3, b"B")
        await f.flush()
        self.assertEqual([(k, p) for k, _, p, _ in ws.got], [(KIND_AUDIO_CONFIG, 2), (KIND_AUDIO, 3)])

    async def test_audio_never_enters_video_group(self):
        f = MediaFanout()
        await f.publish("d#cam", cfg())
        await f.publish("d#cam", key(1))
        await f.publish_audio("d#cam", KIND_AUDIO_CONFIG, 1, ASC)
        await f.publish_audio("d#cam", KIND_AUDIO, 2, b"A")
        await f.publish("d#cam", delta(3))
        late = FakeWS()
        f.add(late, True)
        f.subscribe(late, "d#cam")
        await f.flush()
        kinds = [k for k, *_ in late.got]
        self.assertEqual(kinds.count(KIND_AUDIO), 0, "an old audio frame was replayed from the picture group")
        self.assertIn(KIND_AUDIO_CONFIG, kinds)
        self.assertEqual([k for k in kinds if k != KIND_AUDIO_CONFIG], [KIND_CONFIG, KIND_KEY, 3])

    async def test_video_overflow_does_not_clear_audio(self):
        f = MediaFanout(queue_max=5)
        gate = asyncio.Event()
        ws = FakeWS(gate)
        f.add(ws, True)
        f.subscribe(ws, "d#cam")
        f.subscribe(ws, "d#mic")
        await f.publish_audio("d#mic", KIND_AUDIO_CONFIG, 0, ASC)
        await f.publish("d#cam", cfg())
        await f.publish("d#cam", key(1))
        for i in range(3):
            await f.publish_audio("d#mic", KIND_AUDIO, 10 + i, b"A%d" % i)
        for i in range(2, 30):                                  # video overflows → resync of the video lane
            await f.publish("d#cam", delta(i))
        gate.set()
        await f.flush()
        audio = [p for k, _, p, _ in ws.got if k == KIND_AUDIO]
        self.assertEqual(audio, [10, 11, 12])

    async def test_audio_lane_drops_oldest_after_stall(self):
        f = MediaFanout()
        gate = asyncio.Event()
        ws = FakeWS(gate)
        f.add(ws, True)
        f.subscribe(ws, "d#mic")
        await f.publish_audio("d#mic", KIND_AUDIO_CONFIG, 0, ASC)
        for i in range(100):                                     # a 2 s stall's worth of audio
            await f.publish_audio("d#mic", KIND_AUDIO, i, b"A")
        gate.set()
        await f.flush()
        audio = [p for k, _, p, _ in ws.got if k == KIND_AUDIO]
        self.assertEqual(ws.got[0][0], KIND_AUDIO_CONFIG, "config was dropped with the old frames")
        self.assertLessEqual(len(audio), AUDIO_QUEUE_MAX + 1)
        self.assertEqual(audio[-1], 99, "the newest audio must survive")

    async def test_jpeg_viewers_get_no_audio(self):
        f = MediaFanout()
        ws = FakeWS()
        f.add(ws, False)
        f.subscribe(ws, "d#mic")
        await f.publish_audio("d#mic", KIND_AUDIO_CONFIG, 0, ASC)
        await f.publish_audio("d#mic", KIND_AUDIO, 1, b"A")
        await f.flush()
        self.assertEqual(ws.got, [])

    async def test_forget_device_drops_audio_config(self):
        f = MediaFanout()
        await f.publish_audio("d#mic", KIND_AUDIO_CONFIG, 0, ASC)
        f.forget_device("d#mic")
        ws = FakeWS()
        f.add(ws, True)
        f.subscribe(ws, "d#mic")
        await f.flush()
        self.assertEqual(ws.got, [])
        self.assertFalse(f.has_audio("d#mic"))


if __name__ == "__main__":
    unittest.main()
