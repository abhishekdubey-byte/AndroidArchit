import time
import unittest

from app.services.cam_record import CamRecorder, display_name


def bare_recorder():
    """A CamRecorder without ffmpeg start-up: only the queue/worker is exercised."""
    rec = CamRecorder.__new__(CamRecorder)
    rec._closed = False
    rec._start_worker()
    return rec


class TestSubmitQueue(unittest.TestCase):
    def test_submit_never_blocks_and_keeps_order(self):
        rec = bare_recorder()
        seen = []
        rec.write = lambda b, at=None: (time.sleep(0.05), seen.append(b))
        t = time.perf_counter()
        for i in range(20):
            rec.submit(bytes([i]))
        self.assertLess(time.perf_counter() - t, 0.1)
        rec.drain()
        self.assertEqual(seen, [bytes([i]) for i in range(20)])

    def test_frames_keep_their_arrival_time(self):
        rec = bare_recorder()
        stamps = []
        rec.write = lambda b, at=None: (time.sleep(0.05), stamps.append(at))
        before = time.monotonic()
        rec.submit(b"a")
        rec.submit(b"b")
        after = time.monotonic()
        rec.drain()
        self.assertTrue(all(before <= s <= after for s in stamps))  # not the (later) processing time

    def test_full_queue_drops_oldest_video_never_audio(self):
        rec = CamRecorder.__new__(CamRecorder)
        rec._closed = False
        rec.QUEUE_MAX = 4
        rec._start_worker()
        seen = []
        import threading
        release = threading.Event()
        rec.write = lambda b, at=None: (release.wait(5), seen.append(("v", b)))
        rec.write_audio = lambda pcm, rate=16000, at=None: seen.append(("a", pcm))
        rec.submit(b"v0")            # taken by the worker, blocks in write
        time.sleep(0.05)
        rec.submit_audio(b"a1", 16000)
        for i in range(1, 6):
            rec.submit(b"v%d" % i)   # queue overflows: oldest video dropped first
        release.set()
        rec.drain()
        self.assertIn(("a", b"a1"), seen)
        self.assertEqual(seen[-1], ("v", b"v5"))
        self.assertLess(len([x for x in seen if x[0] == "v"]), 6)

    def test_submit_after_close_is_ignored(self):
        rec = bare_recorder()
        seen = []
        rec.write = lambda b, at=None: seen.append(b)
        rec.submit(b"x")
        rec.drain()
        rec.submit(b"late")
        rec.submit_audio(b"late", 16000)
        self.assertEqual(seen, [b"x"])


class TestDisplayName(unittest.TestCase):
    def test_display_name_windows_path(self):
        self.assertEqual(display_name(r"D:\a\b\live_x.mp4"), "live_x.mp4")
        self.assertEqual(display_name("/home/u/live_y.mp4"), "live_y.mp4")
        self.assertIsNone(display_name(None))


if __name__ == "__main__":
    unittest.main()
