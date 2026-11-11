import unittest

from app.services.media_packets import (KIND_AUDIO, KIND_AUDIO_CONFIG, KIND_CONFIG, KIND_DELTA, KIND_KEY, pack,
                                        unpack)


class TestMediaPackets(unittest.TestCase):
    def test_round_trip_every_kind(self):
        for kind in (KIND_CONFIG, KIND_KEY, KIND_DELTA, KIND_AUDIO_CONFIG, KIND_AUDIO):
            data = pack(kind, "xiaomi_redmi_note_5_pro_523d4f6", 123456789, b"\x00\x00\x00\x01\x67abc")
            self.assertEqual(unpack(data), (kind, "xiaomi_redmi_note_5_pro_523d4f6", 123456789, b"\x00\x00\x00\x01\x67abc"))

    def test_header_layout(self):
        data = pack(KIND_KEY, "dev_é", 2 ** 40, b"\x00\x00\x00\x01e")
        self.assertEqual(data[:2], bytes([2, 6]))                 # kind, utf-8 id length ("é" is 2 bytes)
        self.assertEqual(data[2:8], "dev_é".encode("utf-8"))
        self.assertEqual(data[8:16], (2 ** 40).to_bytes(8, "big"))
        self.assertEqual(data[16:], b"\x00\x00\x00\x01e")

    def test_kind_values(self):
        self.assertEqual((KIND_CONFIG, KIND_KEY, KIND_DELTA, KIND_AUDIO_CONFIG, KIND_AUDIO), (1, 2, 3, 4, 5))

    def test_id_too_long(self):
        with self.assertRaises(ValueError):
            pack(KIND_DELTA, "x" * 300, 0, b"")

    def test_unpack_rejects_truncated(self):
        with self.assertRaises(ValueError):
            unpack(b"\x02\x05ab")


if __name__ == "__main__":
    unittest.main()
