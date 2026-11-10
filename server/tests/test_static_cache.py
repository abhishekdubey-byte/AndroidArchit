import asyncio
import unittest


async def get(app, path):
    sent = []

    first = [True]

    async def receive():  # like a real client: the request once, then nothing until it disconnects
        if first[0]:
            first[0] = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(msg):
        sent.append(msg)
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": b"", "headers": [], "http_version": "1.1", "scheme": "http",
             "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 1)}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}


class TestDashboardFilesRevalidate(unittest.TestCase):
    """Browsers must not keep using an old copy of the dashboard's CSS/JS after an update."""

    def test_css_and_js_are_revalidated(self):
        from app.config import STATIC_DIR
        from app.static_files import DashboardFiles
        app = DashboardFiles(directory=STATIC_DIR, html=True)
        for path in ("/css/files.css", "/js/files.js", "/files.html"):
            status, headers = asyncio.run(get(app, path))
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get("cache-control"), "no-cache", path)
            self.assertIn("etag", headers, path)  # so revalidation is a cheap 304


if __name__ == "__main__":
    unittest.main()
