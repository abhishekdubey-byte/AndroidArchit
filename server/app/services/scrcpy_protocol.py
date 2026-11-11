"""scrcpy server v2.7 wire protocol (pure functions, no I/O).

Byte layouts checked against upstream at tag v2.7: doc/develop.md, app/src/control_msg.c, app/src/demuxer.c,
app/src/server.c. The server (Apache-2.0) is pushed to the phone and encodes the screen with the phone's
hardware H.264 encoder; we talk to it over `adb forward`.
"""
import re
import secrets
import struct
from typing import List, Tuple

SCRCPY_VERSION = "2.7"
DEVICE_JAR = "/data/local/tmp/scrcpy-server.jar"

ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
KEYCODES = {"HOME": 3, "BACK": 4, "RECENTS": 187, "APP_SWITCH": 187, "POWER": 26, "ENTER": 66, "DEL": 67,
            "VOLUME_UP": 24, "VOLUME_DOWN": 25, "MENU": 82, "TAB": 61, "ESCAPE": 111, "WAKEUP": 224}

_TYPE_KEY, _TYPE_TEXT, _TYPE_TOUCH = 0, 1, 2
_POINTER_GENERIC_FINGER = 0xFFFFFFFFFFFFFFFE  # SC_POINTER_ID_GENERIC_FINGER = UINT64_C(-2)
TEXT_MAX = 300                                 # SC_CONTROL_MSG_INJECT_TEXT_MAX_LENGTH
_CODECS = {0x68323634: "h264", 0x68323635: "h265", 0x00617631: "av1"}
_SCID = re.compile(r"^[0-9a-f]{8}$")


def new_scid() -> str:
    """31-bit random id, so several sessions can start at once for the same phone."""
    return f"{secrets.randbelow(0x80000000):08x}"


def socket_name(scid: str) -> str:
    return "scrcpy_" + scid


def device_jar(scid: str) -> str:
    """Per-session jar path on the phone: two sessions starting at once never overwrite or delete each other's
    copy (each server unlinks its own jar at start-up with cleanup=true)."""
    if not _SCID.fullmatch(scid):
        raise ValueError("invalid scid")
    return f"/data/local/tmp/scrcpy-server-{scid}.jar"


def server_command(scid: str, *, video: bool, max_size: int, bit_rate: int, max_fps: int,
                   jar: str = DEVICE_JAR) -> str:
    """Shell command that starts the server on the phone (run via `adb shell`)."""
    if not _SCID.fullmatch(scid):
        raise ValueError("invalid scid")
    if not re.fullmatch(r"/data/local/tmp/[A-Za-z0-9._-]+\.jar", jar):
        raise ValueError("invalid jar path")
    params = [
        f"scid={scid}", "log_level=info", "tunnel_forward=true", "audio=false", "control=true",
        f"video={'true' if video else 'false'}", "video_codec=h264", f"max_size={int(max_size)}",
        f"video_bit_rate={int(bit_rate)}", f"max_fps={int(max_fps)}", "video_codec_options=i-frame-interval:int=2",
        "send_device_meta=false", "send_dummy_byte=true", "send_codec_meta=true", "send_frame_meta=true",
        "cleanup=true", "power_off_on_close=false", "clipboard_autosync=false",
    ]
    if not video:
        params.append("power_on=false")   # an input-only session must not switch the screen on by itself
    return (f"CLASSPATH={jar} app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
            + " ".join(params))


# ---------------- video socket ----------------

def parse_codec_meta(b: bytes) -> Tuple[str, int, int]:
    codec_id, w, h = struct.unpack(">III", b[:12])
    return _CODECS.get(codec_id, f"0x{codec_id:08x}"), w, h


def parse_frame_header(b: bytes) -> Tuple[int, bool, bool, int]:
    """(pts_us, is_config, is_key, size) from the 12-byte frame header."""
    pts_flags, size = struct.unpack(">QI", b[:12])
    return pts_flags & ((1 << 62) - 1), bool(pts_flags >> 63 & 1), bool(pts_flags >> 62 & 1), size


class _Bits:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-Golomb code")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _nal_units(annexb: bytes) -> List[bytes]:
    starts = [m.end() for m in re.finditer(b"\x00\x00\x01", annexb)]
    units = []
    for i, s in enumerate(starts):
        end = starts[i + 1] - 3 if i + 1 < len(starts) else len(annexb)
        units.append(annexb[s:end].rstrip(b"\x00"))
    return units


def _skip_scaling_list(bits: _Bits, size: int):
    last = nxt = 8
    for _ in range(size):
        if nxt != 0:
            nxt = (last + bits.se() + 256) % 256
        last = last if nxt == 0 else nxt


def sps_dimensions(annexb_config: bytes) -> Tuple[int, int]:
    """Display width/height from the first SPS NAL in an Annex-B buffer (cropping applied)."""
    sps = next((u for u in _nal_units(annexb_config) if u and u[0] & 0x1F == 7), None)
    if sps is None:
        raise ValueError("no SPS in buffer")
    rbsp = re.sub(b"\x00\x00\x03", b"\x00\x00", sps[1:])
    b = _Bits(rbsp)
    profile_idc = b.u(8)
    b.u(16)          # constraint flags + level_idc
    b.ue()           # seq_parameter_set_id
    chroma_format_idc, separate_colour_plane = 1, 0
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = b.ue()
        if chroma_format_idc == 3:
            separate_colour_plane = b.u(1)
        b.ue(); b.ue(); b.u(1)          # bit depths, qpprime_y_zero_transform_bypass
        if b.u(1):                      # seq_scaling_matrix_present_flag
            for i in range(8 if chroma_format_idc != 3 else 12):
                if b.u(1):
                    _skip_scaling_list(b, 16 if i < 6 else 64)
    b.ue()                              # log2_max_frame_num_minus4
    poc_type = b.ue()
    if poc_type == 0:
        b.ue()
    elif poc_type == 1:
        b.u(1); b.se(); b.se()
        for _ in range(b.ue()):
            b.se()
    b.ue(); b.u(1)                      # max_num_ref_frames, gaps_in_frame_num_allowed
    width_mbs, height_map = b.ue() + 1, b.ue() + 1
    frame_mbs_only = b.u(1)
    if not frame_mbs_only:
        b.u(1)
    b.u(1)                              # direct_8x8_inference
    width, height = width_mbs * 16, (2 - frame_mbs_only) * height_map * 16
    if b.u(1):                          # frame_cropping_flag
        left, right, top, bottom = b.ue(), b.ue(), b.ue(), b.ue()
        chroma_array_type = 0 if separate_colour_plane else chroma_format_idc
        if chroma_array_type == 0:
            crop_x, crop_y = 1, 2 - frame_mbs_only
        else:
            sub_w = 2 if chroma_array_type in (1, 2) else 1
            sub_h = 2 if chroma_array_type == 1 else 1
            crop_x, crop_y = sub_w, sub_h * (2 - frame_mbs_only)
        width -= (left + right) * crop_x
        height -= (top + bottom) * crop_y
    return width, height


# ---------------- control socket ----------------

def key_event(action: int, keycode: int, repeat: int = 0, meta: int = 0) -> bytes:
    return struct.pack(">BBiII", _TYPE_KEY, action, keycode, repeat, meta)


def touch_event(action: int, x: int, y: int, frame_w: int, frame_h: int, pressure: float = 1.0) -> bytes:
    p = 0xFFFF if pressure >= 1.0 else max(0, int(pressure * 0x10000))
    return struct.pack(">BBQiiHHHii", _TYPE_TOUCH, action, _POINTER_GENERIC_FINGER, int(x), int(y),
                       int(frame_w), int(frame_h), p, 0, 0)


def text_events(text: str) -> List[bytes]:
    """INJECT_TEXT messages, each at most 300 UTF-8 bytes, never splitting a character."""
    chunks, cur = [], b""
    for ch in text:
        enc = ch.encode("utf-8")
        if len(cur) + len(enc) > TEXT_MAX:
            chunks.append(cur)
            cur = b""
        cur += enc
    if cur:
        chunks.append(cur)
    return [struct.pack(">BI", _TYPE_TEXT, len(c)) + c for c in chunks]


# Characters scrcpy 2.7 can type: ASCII via the Virtual key map, accented vowels/ñ via KeyComposition (dead keys),
# ç/Ç/ß directly. Anything else (ø, å, æ, µ, Devanagari, emoji…) would be skipped silently → the companion types it.
_COMPOSABLE = set("àáâãäèéêëìíîïòóôõöùúûüýÿÀÁÂÃÄÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝñÑçÇß")


def scrcpy_can_type(text: str) -> bool:
    return all(c == "\n" or " " <= c <= "~" or c in _COMPOSABLE for c in text)


def _i16fp(f: float) -> int:
    """Float in [-1, 1] → signed 16-bit fixed point (sc_float_to_i16fp)."""
    i = int(max(-1.0, min(1.0, f)) * 0x8000)
    return max(-0x8000, min(0x7FFF, i))


def scroll_event(x: int, y: int, frame_w: int, frame_h: int, hscroll: float, vscroll: float) -> bytes:
    """INJECT_SCROLL_EVENT (type 3, 21 bytes); one mouse-wheel notch = ±1.0."""
    return struct.pack(">BiiHHhhi", 3, int(x), int(y), int(frame_w), int(frame_h), _i16fp(hscroll), _i16fp(vscroll), 0)
