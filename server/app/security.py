"""Access control for remote (internet-exposed) operation.

* Requests from this laptop (loopback, not via a reverse proxy/tunnel) are trusted.
* Everything else (LAN phones, tunnel traffic, remote browsers) must present the
  access token: `?token=`, `X-ACC-Token` header, or the `acc_token` cookie.
* Cross-site browser requests (Origin from another site) are always rejected,
  so a malicious web page cannot drive the API through the user's browser.
"""
import hmac
import json
import os
import secrets
from http.cookies import SimpleCookie
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .config import STORAGE_DIR

TOKEN_FILE = STORAGE_DIR / "access_token"
DEVICE_TOKEN_FILE = STORAGE_DIR / "device_token"
REMOTE_FILE = STORAGE_DIR / "remote.json"
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray", "x-forwarded-host")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
LOCAL_NAMES = LOOPBACK | {"0.0.0.0"}


def _load(path, env_name: str = "") -> str:
    env = os.getenv(env_name) if env_name else None
    if env:
        return env
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    return _new(path)


def _new(path) -> str:
    tok = secrets.token_urlsafe(24)
    path.write_text(tok)
    path.chmod(0o600)
    return tok


# Admin token: dashboards / REST API from other machines. Device token: phones, valid ONLY on /ws/device.
ACCESS_TOKEN = _load(TOKEN_FILE, "ACC_TOKEN")
DEVICE_TOKEN = _load(DEVICE_TOKEN_FILE)


def rotate_tokens():
    global ACCESS_TOKEN, DEVICE_TOKEN
    if os.getenv("ACC_TOKEN"):
        raise RuntimeError("Access token is fixed by the ACC_TOKEN environment variable; change it there")
    ACCESS_TOKEN, DEVICE_TOKEN = _new(TOKEN_FILE), _new(DEVICE_TOKEN_FILE)


def get_token() -> str:
    return ACCESS_TOKEN


def get_device_token() -> str:
    return DEVICE_TOKEN


def load_remote_config() -> dict:
    try:
        return json.loads(REMOTE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_remote_config(cfg: dict):
    REMOTE_FILE.write_text(json.dumps(cfg, indent=2))
    REMOTE_FILE.chmod(0o600)  # holds the cloud relay hub key


def _token_from(headers: dict, query: str) -> Optional[str]:
    tok = parse_qs(query).get("token", [None])[0] or headers.get("x-acc-token")
    if tok:
        return tok
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("cookie", ""))
    except Exception:
        return None
    return cookie["acc_token"].value if "acc_token" in cookie else None


class AccessGuard:
    """Pure ASGI middleware (covers HTTP and WebSocket)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        origin = headers.get("origin")
        if origin:
            o = urlparse(origin).netloc
            if o not in (headers.get("host"), headers.get("x-forwarded-host")):
                return await self._deny(scope, receive, send, 403, "Cross-site request blocked")

        client_host = (scope.get("client") or ("", 0))[0]
        # Trust only genuine local use: loopback socket AND loopback Host/Origin (defeats DNS rebinding)
        # AND no reverse-proxy/tunnel headers.
        # 0.0.0.0 is what uvicorn prints as the address; a rebinding attacker's Host would be their own domain.
        host_name = urlparse("//" + headers.get("host", "")).hostname or ""
        origin_local = not origin or (urlparse(origin).hostname or "") in LOCAL_NAMES
        if (client_host in LOOPBACK and host_name in LOCAL_NAMES and origin_local
                and not any(h in headers for h in PROXY_HEADERS)):
            return await self.app(scope, receive, send)

        query = scope.get("query_string", b"").decode("latin-1")
        tok = _token_from(headers, query)
        if tok and scope.get("path") == "/ws/device" and hmac.compare_digest(tok, DEVICE_TOKEN):
            return await self.app(scope, receive, send)
        if not tok or not hmac.compare_digest(tok, ACCESS_TOKEN):
            return await self._deny(scope, receive, send, 401,
                                    "Unauthorized: open this URL with ?token=<access token> (shown on the laptop at /pair.html)")

        if scope["type"] == "http" and "token=" in query:
            secure = headers.get("x-forwarded-proto") == "https" or scope.get("scheme") == "https"

            async def send_with_cookie(message):
                if message["type"] == "http.response.start":
                    cookie = f"acc_token={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" + ("; Secure" if secure else "")
                    message = {**message, "headers": list(message.get("headers", [])) + [(b"set-cookie", cookie.encode())]}
                await send(message)
            return await self.app(scope, receive, send_with_cookie)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, receive, send, status: int, text: str):
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 4000 + status})
            return
        body = text.encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
