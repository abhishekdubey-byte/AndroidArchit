import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

from app.services import scrcpy_session as ss
from app.services.scrcpy_session import MirrorUnavailable, ScrcpySession, ensure_server_jar

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = (FIXTURES / "sps_720x1440_high.h264").read_bytes()


def frame(payload: bytes, *, config=False, key=False, pts=0) -> bytes:
    flags = (1 << 63 if config else 0) | (1 << 62 if key else 0) | pts
    return flags.to_bytes(8, "big") + len(payload).to_bytes(4, "big") + payload


class FakePhone:
    """Plays the scrcpy server: first connection = video (dummy byte, codec meta, packets), second = control."""

    def __init__(self, video_bytes: bytes, close_video=True):
        self.video_bytes, self.close_video = video_bytes, close_video
        self.connections, self.control = 0, b""
        self.control_done = asyncio.Event()

    async def handle(self, reader, writer):
        self.connections += 1
        if self.connections == 1:
            writer.write(self.video_bytes)
            await writer.drain()
            if self.close_video:
                writer.close()
            else:
                await reader.read()  # until the client closes
        else:
            self.control += await reader.read()
            self.control_done.set()
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


class TestSession(ScrcpySession):
    def __init__(self, port, **kw):
        super().__init__("adb", "serial", jar_path=Path("unused.jar"), **kw)
        self.fake_port, self.adb_calls, self.spawned = port, [], []

    async def _adb(self, *args):
        self.adb_calls.append(args)
        if args[:3] == ("forward", "--no-rebind", "tcp:0"):
            return 0, f"{self.fake_port}\n", ""   # adb prints the port it allocated
        return 0, "", ""

    async def _push_jar(self, jar):
        pass

    async def _spawn_server(self, command):
        self.spawned.append(command)


def video_stream(*packets) -> bytes:
    meta = b"h264" + (720).to_bytes(4, "big") + (1440).to_bytes(4, "big")
    return b"\x00" + meta + b"".join(packets)


class TestScrcpySession(unittest.IsolatedAsyncioTestCase):
    async def test_packets_and_frame_size(self):
        phone = FakePhone(video_stream(frame(CONFIG, config=True), frame(b"K" * 50, key=True, pts=10),
                                       frame(b"D" * 7, pts=43)))
        port = await phone.start()
        s = TestSession(port)
        await s.start()
        self.assertEqual((s.width, s.height), (720, 1440))
        got = [p async for p in s.packets()]
        self.assertEqual([(p.is_config, p.is_key, len(p.data), p.pts_us) for p in got],
                         [(True, False, len(CONFIG), 0), (False, True, 50, 10), (False, False, 7, 43)])
        self.assertEqual(s.frame_size, (720, 1440))
        await s.stop()
        await s.stop()  # idempotent
        removes = [c for c in s.adb_calls if c[:2] == ("forward", "--remove")]
        self.assertEqual(len(removes), 1)
        self.assertTrue(any(c[:3] == ("forward", "--no-rebind", "tcp:0") for c in s.adb_calls))
        self.assertIn(("forward", "--remove", f"tcp:{port}"), s.adb_calls)   # tunnel removed once connected
        self.assertIn(" com.genymobile.scrcpy.Server 2.7 ", s.spawned[0])
        phone.server.close()

    async def test_packets_end_when_phone_disconnects(self):
        phone = FakePhone(video_stream(frame(b"K", key=True)))
        port = await phone.start()
        s = TestSession(port)
        await s.start()
        got = await asyncio.wait_for(self._collect(s), 1.0)
        self.assertEqual(len(got), 1)
        await s.stop()
        phone.server.close()

    async def _collect(self, s):
        return [p async for p in s.packets()]

    async def test_control_bytes_reach_phone(self):
        phone = FakePhone(video_stream(), close_video=False)
        port = await phone.start()
        s = TestSession(port)
        await s.start()
        await s.send(b"\x00\x00abc")
        await s.send(b"\x01")
        await s.stop()
        await asyncio.wait_for(phone.control_done.wait(), 2)
        self.assertEqual(phone.control, b"\x00\x00abc\x01")
        phone.server.close()

    async def test_no_server_raises_mirror_unavailable(self):
        async def refuse(reader, writer):
            writer.close()  # adb forward accepts, nobody on the phone side
        srv = await asyncio.start_server(refuse, "127.0.0.1", 0)
        s = TestSession(srv.sockets[0].getsockname()[1])
        s.CONNECT_TIMEOUT = 0.5
        with self.assertRaises(MirrorUnavailable):
            await s.start()
        self.assertTrue(any(c[:2] == ("forward", "--remove") for c in s.adb_calls))  # cleaned up
        srv.close()


class TestServerJar(unittest.TestCase):
    def test_wrong_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            orig = ss._download
            ss._download = lambda url, dest: Path(dest).write_bytes(b"not the real server")
            try:
                with self.assertRaises(MirrorUnavailable):
                    ensure_server_jar(Path(d))
                self.assertEqual(list(Path(d).iterdir()), [])
            finally:
                ss._download = orig

    def test_existing_good_jar_reused(self):
        with tempfile.TemporaryDirectory() as d:
            data = b"pretend jar"
            orig_sha, orig_dl = ss.SCRCPY_SERVER_SHA256, ss._download
            ss.SCRCPY_SERVER_SHA256 = hashlib.sha256(data).hexdigest()
            calls = []
            ss._download = lambda url, dest: (calls.append(url), Path(dest).write_bytes(data))
            try:
                p1 = ensure_server_jar(Path(d))
                p2 = ensure_server_jar(Path(d))
                self.assertEqual(p1, p2)
                self.assertEqual(p1.read_bytes(), data)
                self.assertEqual(len(calls), 1)
            finally:
                ss.SCRCPY_SERVER_SHA256, ss._download = orig_sha, orig_dl

    def test_pinned_checksum(self):
        self.assertEqual(ss.SCRCPY_SERVER_SHA256, "a23c5659f36c260f105c022d27bcb3eafffa26070e7baa9eda66d01377a1adba")


if __name__ == "__main__":
    unittest.main()


class TestSessionHardening(unittest.IsolatedAsyncioTestCase):
    async def test_forward_is_atomic_no_rebind_port_from_adb(self):
        phone = FakePhone(video_stream(frame(CONFIG, config=True)), close_video=False)
        port = await phone.start()

        class AtomicForward(ScrcpySession):
            def __init__(self):
                super().__init__("adb", "serial", jar_path=Path("unused.jar"))
                self.adb_calls = []

            async def _adb(self, *args):
                self.adb_calls.append(args)
                if args[:3] == ("forward", "--no-rebind", "tcp:0"):
                    return 0, f"{port}\n", ""      # adb prints the port it picked
                return 0, "", ""

            async def _push_jar(self, jar):
                pass

            async def _spawn_server(self, command):
                self.command = command

        s = AtomicForward()
        await s.start()
        self.assertEqual(s.port, port)
        fwd = [c for c in s.adb_calls if c[0] == "forward" and c[1] != "--remove"][0]
        self.assertEqual(fwd[:3], ("forward", "--no-rebind", "tcp:0"))
        self.assertIn(f"/data/local/tmp/scrcpy-server-{s.scid}.jar", s.command)   # per-session jar: no clobbering
        await s.stop()
        self.assertIn(("forward", "--remove", f"tcp:{port}"), s.adb_calls)
        phone.server.close()


class TestProtocolHardening(unittest.TestCase):
    def test_scid_trailing_newline_rejected(self):
        from app.services.scrcpy_protocol import server_command
        with self.assertRaises(ValueError):
            server_command("deadbeef\n", video=True, max_size=1280, bit_rate=1, max_fps=1)

    def test_packet_cap(self):
        self.assertLessEqual(ss.MAX_PACKET, 4 * 1024 * 1024)


class TestControlOnlyCommand(unittest.TestCase):
    def test_input_only_session_does_not_power_on_screen(self):
        from app.services.scrcpy_protocol import server_command
        self.assertIn(" power_on=false", server_command("00000001", video=False, max_size=1280, bit_rate=1, max_fps=1))
        self.assertNotIn("power_on=false", server_command("00000001", video=True, max_size=1280, bit_rate=1, max_fps=1))

    def test_jar_path_validated(self):
        from app.services.scrcpy_protocol import server_command
        with self.assertRaises(ValueError):
            server_command("00000001", video=True, max_size=1, bit_rate=1, max_fps=1, jar="/sdcard/x;reboot.jar")
