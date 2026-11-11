import unittest
from pathlib import Path

from app.services.scrcpy_protocol import (ACTION_DOWN, ACTION_MOVE, ACTION_UP, DEVICE_JAR, KEYCODES, SCRCPY_VERSION,
                                          key_event, new_scid, parse_codec_meta, parse_frame_header,
                                          scrcpy_can_type, server_command, socket_name, sps_dimensions, text_events,
                                          touch_event)

FIXTURES = Path(__file__).parent / "fixtures"


class TestConstants(unittest.TestCase):
    def test_pinned_version_and_jar(self):
        self.assertEqual(SCRCPY_VERSION, "2.7")
        self.assertEqual(DEVICE_JAR, "/data/local/tmp/scrcpy-server.jar")
        self.assertEqual((ACTION_DOWN, ACTION_UP, ACTION_MOVE), (0, 1, 2))
        self.assertEqual((KEYCODES["HOME"], KEYCODES["BACK"], KEYCODES["RECENTS"], KEYCODES["POWER"]), (3, 4, 187, 26))

    def test_scid(self):
        for _ in range(50):
            scid = new_scid()
            self.assertRegex(scid, r"^[0-9a-f]{8}$")
            self.assertLess(int(scid, 16), 0x80000000)
        self.assertEqual(socket_name("0a1b2c3d"), "scrcpy_0a1b2c3d")


class TestServerCommand(unittest.TestCase):
    def test_server_command(self):
        cmd = server_command("0a1b2c3d", video=True, max_size=1280, bit_rate=8_000_000, max_fps=60)
        self.assertTrue(cmd.startswith("CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / "))
        self.assertIn(" com.genymobile.scrcpy.Server 2.7 scid=0a1b2c3d ", cmd + " ")
        for part in ("tunnel_forward=true", "audio=false", "control=true", "video=true", "video_codec=h264",
                     "max_size=1280", "video_bit_rate=8000000", "max_fps=60",
                     "video_codec_options=i-frame-interval:int=2", "send_device_meta=false", "send_dummy_byte=true",
                     "send_codec_meta=true", "send_frame_meta=true", "cleanup=true", "power_off_on_close=false",
                     "clipboard_autosync=false"):
            self.assertIn(" " + part, cmd)

    def test_control_only(self):
        self.assertIn(" video=false", server_command("00000001", video=False, max_size=1280, bit_rate=1, max_fps=1))

    def test_rejects_bad_scid(self):
        with self.assertRaises(ValueError):
            server_command("1; rm -rf /", video=True, max_size=1280, bit_rate=1, max_fps=1)


class TestStreamHeaders(unittest.TestCase):
    def test_codec_meta(self):
        meta = b"h264" + (720).to_bytes(4, "big") + (1440).to_bytes(4, "big")
        self.assertEqual(parse_codec_meta(meta), ("h264", 720, 1440))

    def test_frame_header_flags(self):
        key = ((1 << 62) | 123456).to_bytes(8, "big") + (999).to_bytes(4, "big")
        self.assertEqual(parse_frame_header(key), (123456, False, True, 999))
        cfg = (1 << 63).to_bytes(8, "big") + (30).to_bytes(4, "big")
        self.assertEqual(parse_frame_header(cfg), (0, True, False, 30))
        delta = (777).to_bytes(8, "big") + (5).to_bytes(4, "big")
        self.assertEqual(parse_frame_header(delta), (777, False, False, 5))

    def test_sps_dimensions_high_profile(self):
        self.assertEqual(sps_dimensions((FIXTURES / "sps_720x1440_high.h264").read_bytes()), (720, 1440))

    def test_sps_dimensions_with_cropping(self):
        self.assertEqual(sps_dimensions((FIXTURES / "sps_1080x2340_baseline.h264").read_bytes()), (1080, 2340))

    def test_sps_missing(self):
        with self.assertRaises(ValueError):
            sps_dimensions(b"\x00\x00\x00\x01\x68\xce\x3c\x80")  # PPS only


class TestControlMessages(unittest.TestCase):
    def test_key_event_layout(self):
        self.assertEqual(key_event(ACTION_DOWN, 3), bytes([0, 0]) + (3).to_bytes(4, "big") + bytes(8))
        self.assertEqual(len(key_event(ACTION_UP, 187, repeat=1, meta=2)), 14)

    def test_touch_event_layout(self):
        b = touch_event(ACTION_DOWN, 100, 200, 720, 1440)
        self.assertEqual(len(b), 32)
        self.assertEqual(b[:2], bytes([2, 0]))
        self.assertEqual(b[2:10], b"\xff" * 7 + b"\xfe")                     # generic finger pointer id
        self.assertEqual(b[10:18], (100).to_bytes(4, "big") + (200).to_bytes(4, "big"))
        self.assertEqual(b[18:24], (720).to_bytes(2, "big") + (1440).to_bytes(2, "big") + b"\xff\xff")
        self.assertEqual(b[24:32], bytes(8))                                  # action_button, buttons
        self.assertEqual(touch_event(ACTION_UP, 1, 1, 2, 2, pressure=0.0)[22:24], b"\x00\x00")

    def test_text_single_chunk(self):
        self.assertEqual(text_events("hi"), [bytes([1]) + (2).to_bytes(4, "big") + b"hi"])

    def test_text_chunks_never_split_codepoints(self):
        chunks = text_events("é" * 200)            # 400 UTF-8 bytes
        self.assertEqual([len(c) - 5 for c in chunks], [300, 100])
        self.assertEqual(b"".join(c[5:] for c in chunks).decode("utf-8"), "é" * 200)
        odd = text_events("a" + "é" * 200)         # boundary would fall inside a 2-byte char
        self.assertEqual(b"".join(c[5:] for c in odd).decode("utf-8"), "a" + "é" * 200)
        self.assertTrue(all(len(c) - 5 <= 300 for c in odd))

    def test_scrcpy_can_type(self):
        self.assertTrue(scrcpy_can_type("Hello, world! café ñ"))
        self.assertFalse(scrcpy_can_type("नमस्ते"))
        self.assertFalse(scrcpy_can_type("😀"))


if __name__ == "__main__":
    unittest.main()


class TestRealEncoder(unittest.TestCase):
    def test_sps_from_real_scrcpy_session(self):
        # first config packet captured from scrcpy 2.7 on a Redmi Note 5 Pro (max_size=1280)
        self.assertEqual(sps_dimensions((FIXTURES / "scrcpy_config_real.h264").read_bytes()), (640, 1280))
