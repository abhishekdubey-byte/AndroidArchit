"""Phase 4: the MPEG-TS muxer behind timestamp-exact recordings."""
import unittest

from app.services.ts_mux import (PACKET, PID_AUDIO, PID_PAT, PID_PMT, PID_VIDEO, TsMuxer, adts_header,
                                 asc_sample_rate, crc32_mpeg, decode_pts, encode_pts, parse_asc, us_to_90k)

ASC = b"\x11\x88"            # AAC-LC, 48 kHz, mono


def packets(data: bytes):
    assert len(data) % PACKET == 0
    return [data[i:i + PACKET] for i in range(0, len(data), PACKET)]


def pid(p):
    return ((p[1] & 0x1F) << 8) | p[2]


class TestTsMux(unittest.TestCase):
    def test_crc_matches_mpeg2_check_value(self):
        self.assertEqual(crc32_mpeg(b"123456789"), 0x0376E6E7)

    def test_pts_round_trip_and_90k_from_us(self):
        for v in (0, 1, 90000, (1 << 33) - 1, 123456789):
            self.assertEqual(decode_pts(encode_pts(v)), v)
        self.assertEqual(us_to_90k(1_000_000), 90000)
        self.assertEqual(us_to_90k(33_333), 2999)

    def test_adts_header_known_vector(self):
        self.assertEqual(adts_header(200, ASC), bytes.fromhex("fff14c4019fffc"))
        self.assertEqual(parse_asc(ASC), (2, 3, 1))
        self.assertEqual(asc_sample_rate(ASC), 48000)

    def test_every_packet_is_188_bytes_with_sync(self):
        m = TsMuxer()
        out = m.psi() + m.video(63000, b"\x00\x00\x00\x01\x65" + b"x" * 5000, True)
        for n in (1, 183, 184, 185, 400):
            out += m.audio_frame(64000 + n, b"a" * n, ASC)
            out += m.video(64000 + n, b"\x00\x00\x00\x01\x41" + b"d" * n, False)
        for p in packets(out):
            self.assertEqual(p[0], 0x47)

    def test_continuity_counters_wrap_per_pid(self):
        m = TsMuxer()
        out = b"".join(m.audio_frame(i, b"a" * 10, ASC) for i in range(40))
        out += b"".join(m.video(i, b"v" * 10, False) for i in range(20))
        seen = {}
        for p in packets(out):
            cc = p[3] & 0x0F
            if pid(p) in seen:
                self.assertEqual(cc, (seen[pid(p)] + 1) & 0x0F)
            seen[pid(p)] = cc
        self.assertEqual(set(seen), {PID_AUDIO, PID_VIDEO})

    def test_pes_carries_pts_and_key_frames_carry_psi_and_pcr(self):
        m = TsMuxer()
        ps = packets(m.video(123456, b"\x00\x00\x00\x01\x65" + b"k" * 300, True))
        self.assertEqual([pid(p) for p in ps[:2]], [PID_PAT, PID_PMT])
        first = ps[2]
        self.assertEqual(pid(first), PID_VIDEO)
        self.assertTrue(first[1] & 0x40)                      # payload unit start
        af_len = first[4]
        self.assertTrue(first[5] & 0x10)                      # PCR present
        self.assertTrue(first[5] & 0x40)                      # random access
        pes = first[5 + af_len:]
        self.assertEqual(pes[:4], b"\x00\x00\x01\xe0")
        self.assertEqual(decode_pts(pes[9:14]), 123456)


if __name__ == "__main__":
    unittest.main()
