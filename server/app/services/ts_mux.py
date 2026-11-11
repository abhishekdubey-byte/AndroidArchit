"""Minimal MPEG-TS muxer for the live camera recorder: H.264 + AAC with the phone's real timestamps.

Raw H.264 (`ffmpeg -f h264`) has no timestamps, so a recording made from it is only right when the frame rate never
changes - and the phone's camera drops to 15 fps when it is warm, or after a lens switch. Transport stream carries a
90 kHz PTS per frame, so the file plays at exactly the speed it was filmed and the audio stays on the same clock;
on stop `ffmpeg -c copy` turns it into MP4 in a fraction of a second.

Layout: PAT (PID 0) -> PMT (PID 0x1000) -> H.264 on PID 0x100 (PCR carrier), ADTS AAC on PID 0x101.
"""
from typing import List, Optional, Tuple

PACKET = 188
PID_PAT = 0x0000
PID_PMT = 0x1000
PID_VIDEO = 0x100
PID_AUDIO = 0x101
PTS_OFFSET = 63000            # 0.7 s: PTS starts above the PCR so a player never sees a frame "before" the clock

_AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


def _crc_table():
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
    return crc


def us_to_90k(pts_us: int) -> int:
    return (pts_us * 9 // 100) & ((1 << 33) - 1)


def encode_pts(pts90: int, marker: int = 0x2) -> bytes:
    """The 5-byte PTS field of a PES header ('0010' + 33 bits with marker bits)."""
    pts90 &= (1 << 33) - 1
    return bytes([
        (marker << 4) | (((pts90 >> 30) & 0x07) << 1) | 1,
        (pts90 >> 22) & 0xFF,
        (((pts90 >> 15) & 0x7F) << 1) | 1,
        (pts90 >> 7) & 0xFF,
        ((pts90 & 0x7F) << 1) | 1,
    ])


def decode_pts(field: bytes) -> int:
    return (((field[0] >> 1) & 0x07) << 30) | (field[1] << 22) | ((field[2] >> 1) << 15) | (field[3] << 7) | (field[4] >> 1)


def parse_asc(asc: bytes) -> Tuple[int, int, int]:
    """AudioSpecificConfig -> (object type, sample-rate index, channel config)."""
    if len(asc) < 2:
        raise ValueError("AudioSpecificConfig too short")
    bits = (asc[0] << 8) | asc[1]
    return bits >> 11, (bits >> 7) & 0x0F, (bits >> 3) & 0x0F


def asc_sample_rate(asc: bytes) -> int:
    return _AAC_RATES[parse_asc(asc)[1]]


def adts_header(frame_len: int, asc: bytes) -> bytes:
    """7-byte ADTS header (no CRC) for one raw AAC frame of `frame_len` bytes."""
    obj, idx, ch = parse_asc(asc)
    full = frame_len + 7
    if full >= 1 << 13:
        raise ValueError("AAC frame too large for ADTS")
    return bytes([
        0xFF, 0xF1,
        ((obj - 1) & 0x03) << 6 | (idx << 2) | (ch >> 2),
        ((ch & 0x03) << 6) | (full >> 11),
        (full >> 3) & 0xFF,
        ((full & 0x07) << 5) | 0x1F,
        0xFC,
    ])


class TsMuxer:
    """Turns (pts µs, access unit) pairs into 188-byte TS packets. Not thread-safe; one writer."""

    def __init__(self, audio: bool = True):
        self.audio = audio
        self._cc = {}

    def _counter(self, pid: int) -> int:
        cc = self._cc.get(pid, -1)
        cc = (cc + 1) & 0x0F
        self._cc[pid] = cc
        return cc

    def _section_packet(self, pid: int, section: bytes) -> bytes:
        body = b"\x00" + section + crc32_mpeg(section).to_bytes(4, "big")    # pointer_field + table + CRC
        head = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10 | self._counter(pid)])
        return head + body + b"\xFF" * (PACKET - 4 - len(body))

    def psi(self) -> bytes:
        """PAT + PMT (written at the start and before every key frame, so any cut point can be played)."""
        pat_body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01, 0xE0 | (PID_PMT >> 8), PID_PMT & 0xFF])
        pat = bytes([0x00, 0xB0, len(pat_body) + 4]) + pat_body
        streams = bytes([0x1B, 0xE0 | (PID_VIDEO >> 8), PID_VIDEO & 0xFF, 0xF0, 0x00])
        if self.audio:
            streams += bytes([0x0F, 0xE0 | (PID_AUDIO >> 8), PID_AUDIO & 0xFF, 0xF0, 0x00])
        pmt_body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0xE0 | (PID_VIDEO >> 8), PID_VIDEO & 0xFF, 0xF0, 0x00]) + streams
        pmt = bytes([0x02, 0xB0, len(pmt_body) + 4]) + pmt_body
        return self._section_packet(PID_PAT, pat) + self._section_packet(PID_PMT, pmt)

    def video(self, pts90: int, annexb: bytes, key: bool) -> bytes:
        pes = self._pes(0xE0, pts90, annexb, bounded=False)
        out = self.psi() if key else b""
        return out + self._packetize(PID_VIDEO, pes, pcr=max(0, pts90 - PTS_OFFSET), random_access=key)

    def audio_frame(self, pts90: int, raw_aac: bytes, asc: bytes) -> bytes:
        pes = self._pes(0xC0, pts90, adts_header(len(raw_aac), asc) + raw_aac, bounded=True)
        return self._packetize(PID_AUDIO, pes)

    @staticmethod
    def _pes(stream_id: int, pts90: int, payload: bytes, bounded: bool) -> bytes:
        header = bytes([0x80, 0x80, 0x05]) + encode_pts(pts90)             # PTS only (no B-frames: DTS = PTS)
        length = len(header) + len(payload)
        size = length if bounded and length <= 0xFFFF else 0                  # 0 = unbounded (allowed for video)
        return b"\x00\x00\x01" + bytes([stream_id]) + size.to_bytes(2, "big") + header + payload

    def _packetize(self, pid: int, pes: bytes, pcr: Optional[int] = None, random_access: bool = False) -> bytes:
        out: List[bytes] = []
        pos, first = 0, True
        while pos < len(pes):
            af: Optional[bytes] = None                                      # adaptation field body (after its length byte)
            if first and (pcr is not None or random_access):
                af = bytes([(0x40 if random_access else 0) | (0x10 if pcr is not None else 0)])
                if pcr is not None:
                    base = pcr & ((1 << 33) - 1)
                    af += bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                                 ((base & 1) << 7) | 0x7E, 0x00])
            room = PACKET - 4 - (0 if af is None else 1 + len(af))
            chunk = pes[pos:pos + room]
            short = room - len(chunk)                                       # last packet: fill with stuffing bytes
            if short:
                if af is None:                                              # needs an adaptation field of `short` bytes
                    af = b"" if short == 1 else b"\x00" + b"\xFF" * (short - 2)
                else:
                    af += b"\xFF" * short
            head = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF,
                          (0x10 if af is None else 0x30) | self._counter(pid)])
            packet = head + chunk if af is None else head + bytes([len(af)]) + af + chunk
            assert len(packet) == PACKET, len(packet)
            out.append(packet)
            pos += len(chunk)
            first = False
        return b"".join(out)
