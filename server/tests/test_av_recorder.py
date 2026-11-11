"""Phase 4: recordings use the phone's timestamps (right duration at any frame rate, audio in sync)."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from app.services.av_recorder import AvRecorder
from app.services.cam_record import FFMPEG
from app.services.h264_recorder import AutoRecorder, split_access_units
from app.services.media_packets import KIND_AUDIO, KIND_AUDIO_CONFIG, KIND_CONFIG

FIXTURES = Path(__file__).parent / "fixtures"
ASC = b"\x11\x88"
T0 = 5_000_000_000            # the phone's monotonic clock is far from zero


def aac_frames():
    data = (FIXTURES / "tone_48k_mono_10s.aac").read_bytes()
    out, i = [], 0
    while i + 7 <= len(data):
        n = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        out.append(data[i + 7:i + n])
        i += n
    return out


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,start_time,duration:format=duration", "-of", "json", path],
                         capture_output=True, text=True).stdout
    return json.loads(out)


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class TestAvRecorder(unittest.TestCase):
    def feed_video(self, rec, slow_after=None):
        """300 frames: 30 fps, or 30 fps then 15 fps (the phone got warm) after `slow_after` frames."""
        pts, n = T0, 0
        for kind, data in split_access_units((FIXTURES / "cam_720p_10s.h264").read_bytes()):
            if kind == KIND_CONFIG:
                rec.submit_video(kind, 0, data)
                continue
            rec.submit_video(kind, pts, data)
            n += 1
            pts += 66_667 if slow_after is not None and n >= slow_after else 33_333
        return pts - T0

    def test_frame_rate_change_keeps_real_duration(self):
        with tempfile.TemporaryDirectory() as d:
            rec = AvRecorder("Phone", folder=Path(d))
            span = self.feed_video(rec, slow_after=150)            # 5 s at 30 fps + 150 frames at 15 fps = 15 s
            path = rec.close()
            self.assertIsNotNone(path)
            dur = float(probe(path)["format"]["duration"])
            self.assertAlmostEqual(dur, span / 1e6, delta=0.1)
            self.assertAlmostEqual(dur, 15.0, delta=0.1)

    def test_audio_and_video_start_together_and_close_is_fast(self):
        with tempfile.TemporaryDirectory() as d:
            rec = AvRecorder("Phone", folder=Path(d))
            rec.submit_audio_packet(KIND_AUDIO_CONFIG, T0, ASC)
            frames = aac_frames()
            # interleave: audio arrives a little before the first picture, then alongside it
            for i, f in enumerate(frames[:3]):
                rec.submit_audio_packet(KIND_AUDIO, T0 - 64_000 + i * 21_333, f)
            self.feed_video(rec)
            for i, f in enumerate(frames[3:], start=3):
                rec.submit_audio_packet(KIND_AUDIO, T0 - 64_000 + i * 21_333, f)
            time.sleep(0.5)
            t = time.perf_counter()
            path = rec.close()
            took = time.perf_counter() - t
            self.assertIsNotNone(path)
            self.assertLess(took, 0.5)
            info = probe(path)
            starts = {s["codec_type"]: float(s["start_time"]) for s in info["streams"]}
            self.assertEqual(set(starts), {"video", "audio"})
            self.assertLess(abs(starts["video"] - starts["audio"]), 0.04 + 0.1)   # pre-roll ≤ 0.1 s kept
            self.assertAlmostEqual(float(info["format"]["duration"]), 10.0, delta=0.15)
            self.assertFalse(Path(path).with_suffix(".ts").exists())

    def test_audio_synced_to_video_clock(self):
        """Audio that starts 2 s into the picture lands 2 s into the file (not at 0)."""
        with tempfile.TemporaryDirectory() as d:
            rec = AvRecorder("Phone", folder=Path(d), audio_config=ASC)
            self.feed_video(rec)
            for i, f in enumerate(aac_frames()[:100]):
                rec.submit_audio_packet(KIND_AUDIO, T0 + 2_000_000 + i * 21_333, f)
            path = rec.close()
            starts = {s["codec_type"]: float(s["start_time"]) for s in probe(path)["streams"]}
            self.assertAlmostEqual(starts["audio"] - starts["video"], 2.0, delta=0.03)

    def test_rotation_and_no_frames(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(AvRecorder("Phone", folder=Path(d)).close())
            rec = AvRecorder("Phone", folder=Path(d), rotation=90)
            self.feed_video(rec)
            path = rec.close()
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream_side_data=rotation", "-of", "csv=p=0", path],
                                 capture_output=True, text=True).stdout
            self.assertIn("-90", out)

    def test_auto_recorder_uses_av_recorder_and_forwards_aac(self):
        with tempfile.TemporaryDirectory() as d:
            rec = AutoRecorder("Phone", folder=Path(d), audio_config=ASC)
            self.feed_video(rec)
            for i, f in enumerate(aac_frames()[:50]):
                rec.submit_audio_packet(KIND_AUDIO, T0 + i * 21_333, f)
            self.assertIsInstance(rec.inner, AvRecorder)
            path = rec.close()
            kinds = {s["codec_type"] for s in probe(path)["streams"]}
            self.assertEqual(kinds, {"video", "audio"})

    def test_legacy_pcm_phone_keeps_old_recorder(self):
        from app.services.h264_recorder import H264Recorder
        with tempfile.TemporaryDirectory() as d:
            rec = AutoRecorder("Phone", folder=Path(d), legacy_audio=True)
            self.feed_video(rec)
            self.assertIsInstance(rec.inner, H264Recorder)
            rec.close()


if __name__ == "__main__":
    unittest.main()
