import io
import ntpath
import unittest


class TestUtf8Stdio(unittest.TestCase):
    def test_utf8_stdio_survives_arrow_on_cp1252(self):
        from app.stdio_utf8 import force_utf8_stdio
        raw = io.BytesIO()
        w = io.TextIOWrapper(raw, encoding="cp1252")
        force_utf8_stdio([w])
        print("sync started → folder", file=w)
        w.flush()
        self.assertIn("→".encode("utf-8"), raw.getvalue())

    def test_streams_without_reconfigure_are_skipped(self):
        from app.stdio_utf8 import force_utf8_stdio
        force_utf8_stdio([io.StringIO(), None])  # must not raise


class TestResolveAdbPath(unittest.TestCase):
    def resolve(self, env, which=None, existing=()):
        from app.config import resolve_adb_path
        return resolve_adb_path(env, lambda name: which, lambda p: p in existing)

    def test_adb_env_wins_even_if_missing(self):
        self.assertEqual(self.resolve({"ADB_PATH": r"C:\nope\adb.exe"}), r"C:\nope\adb.exe")

    def test_adb_from_path(self):
        self.assertEqual(self.resolve({}, which=r"C:\tools\adb.exe"), r"C:\tools\adb.exe")

    def test_adb_from_android_home(self):
        sdk = ntpath.join(r"C:\sdk", "platform-tools", "adb.exe")
        posix = "/opt/sdk/platform-tools/adb"
        self.assertIn(self.resolve({"ANDROID_HOME": r"C:\sdk"}, existing={sdk}), {sdk})
        self.assertEqual(self.resolve({"ANDROID_SDK_ROOT": "/opt/sdk"}, existing={posix}), posix)

    def test_adb_from_windows_sdk(self):
        sdk = r"C:\Users\u\AppData\Local\Android\Sdk\platform-tools\adb.exe"
        self.assertEqual(self.resolve({"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, existing={sdk}), sdk)

    def test_adb_from_linux_home_sdk(self):
        sdk = "/home/u/Android/Sdk/platform-tools/adb"
        self.assertEqual(self.resolve({"HOME": "/home/u"}, existing={sdk}), sdk)

    def test_adb_linux_default(self):
        self.assertEqual(self.resolve({}), "/usr/bin/adb")


if __name__ == "__main__":
    unittest.main()
