"""One binary packet format for every live stream sent to dashboards (USB mirror, companion camera/screen, audio).

    [u8 kind][u8 idLen][idLen bytes device_id, UTF-8][u64 big-endian PTS in microseconds][payload]

Video payloads are H.264 Annex-B; the browser decodes them with WebCodecs (static/js/h264-player.js).
"""
import struct
from typing import Tuple

KIND_CONFIG = 1        # codec config (SPS/PPS)
KIND_KEY = 2           # key frame
KIND_DELTA = 3         # delta frame
KIND_AUDIO_CONFIG = 4
KIND_AUDIO = 5

_HEAD = struct.Struct(">BB")
_PTS = struct.Struct(">Q")


def pack(kind: int, device_id: str, pts_us: int, payload: bytes) -> bytes:
    did = device_id.encode("utf-8")
    if len(did) > 255:
        raise ValueError("device id longer than 255 bytes")
    return _HEAD.pack(kind, len(did)) + did + _PTS.pack(pts_us & 0xFFFFFFFFFFFFFFFF) + payload


def unpack(data: bytes) -> Tuple[int, str, int, bytes]:
    if len(data) < 2:
        raise ValueError("packet too short")
    kind, n = _HEAD.unpack_from(data, 0)
    end = 2 + n + 8
    if len(data) < end:
        raise ValueError("packet truncated")
    did = data[2:2 + n].decode("utf-8")
    (pts,) = _PTS.unpack_from(data, 2 + n)
    return kind, did, pts, bytes(data[end:])
