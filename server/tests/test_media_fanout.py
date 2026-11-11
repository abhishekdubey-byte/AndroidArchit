import asyncio
import unittest

from app.services.media_packets import KIND_CONFIG, KIND_DELTA, KIND_KEY, unpack
from app.services.scrcpy_session import VideoPacket
from app.websocket.media_fanout import MediaFanout


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


def assert_decodable(test, got):
    """Every delta must follow its key frame and all earlier deltas of the same group, after a config."""
    have_config, gop = False, None
    for kind, _, pts, _ in got:
        if kind == KIND_CONFIG:
            have_config, gop = True, None
        elif kind == KIND_KEY:
            test.assertTrue(have_config, "key frame before any config")
            gop = [pts]
        else:
            test.assertIsNotNone(gop, f"delta {pts} without its key frame")
            test.assertGreater(pts, gop[-1], f"delta {pts} out of order / gap-free chain broken")
            gop.append(pts)


class TestMediaFanout(unittest.IsolatedAsyncioTestCase):
    async def settle(self):
        for _ in range(50):
            await asyncio.sleep(0)

    async def test_fast_and_slow_clients(self):
        f = MediaFanout(queue_max=10)
        fast, gate = FakeWS(), asyncio.Event()
        slow = FakeWS(gate)
        f.add(fast, True)
        f.add(slow, True)
        await f.publish("d", cfg())
        await f.publish("d", key(1))
        for i in range(2, 102):
            await f.publish("d", delta(i))
            await self.settle()
        await f.publish("d", key(200))
        for i in range(201, 206):
            await f.publish("d", delta(i))
        await self.settle()
        gate.set()
        await f.flush()
        self.assertEqual(len(fast.got), 108)  # config + key + 100 deltas + key + 5 deltas
        assert_decodable(self, fast.got)
        self.assertLess(len(slow.got), 108)          # it was resynced, not sent everything
        assert_decodable(self, slow.got)
        self.assertEqual([g[2] for g in slow.got[-6:]], [200, 201, 202, 203, 204, 205])
        await f.close()

    async def test_late_joiner_gets_config_and_current_group(self):
        f = MediaFanout()
        early = FakeWS()
        f.add(early, True)
        await f.publish("d", cfg())
        await f.publish("d", key(1))
        await f.publish("d", delta(2))
        await f.publish("d", delta(3))
        late = FakeWS()
        f.add(late, True)
        await f.publish("d", delta(4))
        await f.flush()
        self.assertEqual([(g[0], g[2]) for g in late.got],
                         [(KIND_CONFIG, 0), (KIND_KEY, 1), (KIND_DELTA, 2), (KIND_DELTA, 3), (KIND_DELTA, 4)])
        await f.close()

    async def test_jpeg_client_gets_no_binary(self):
        f = MediaFanout()
        jpeg = FakeWS()
        f.add(jpeg, False)
        await f.publish("d", cfg())
        await f.publish("d", key(1))
        await f.flush()
        self.assertEqual(jpeg.got, [])
        self.assertEqual((f.h264_viewers("d"), f.jpeg_viewers("d")), (0, 1))
        f.remove(jpeg)
        self.assertEqual(f.jpeg_viewers("d"), 0)
        await f.close()

    async def test_new_config_resets_group(self):
        f = MediaFanout()
        a = FakeWS()
        f.add(a, True)
        await f.publish("d", cfg())
        await f.publish("d", key(1))
        await f.publish("d", delta(2))
        await f.publish("d", cfg())            # rotation: new SPS
        await f.publish("d", key(10))
        late = FakeWS()
        f.add(late, True)
        await f.publish("d", delta(11))
        await f.flush()
        self.assertEqual([g[2] for g in late.got], [0, 10, 11])
        await f.close()

    async def test_broken_client_removed(self):
        class Broken:
            async def send_bytes(self, data):
                raise ConnectionError("gone")
        f = MediaFanout()
        b = Broken()
        f.add(b, True)
        await f.publish("d", cfg())
        await f.flush()
        self.assertEqual(f.h264_viewers("d"), 0)
        await f.close()


if __name__ == "__main__":
    unittest.main()
