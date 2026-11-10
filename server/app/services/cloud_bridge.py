"""Laptop side of the Cloudflare relay (see cloud-relay/src/index.js).

The laptop keeps one outbound WebSocket ("hub") to the relay. Every phone that connects to the
relay is re-created here as a local connection to /ws/device, marked as proxied (X-Forwarded-For),
so the normal AccessGuard device-token check and the hub/relay code apply unchanged.
"""
import asyncio
import hashlib
import json
import time
from typing import Dict, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .. import security

K_OPEN, K_TEXT, K_BIN, K_CLOSE, K_AUTH = 1, 2, 3, 4, 5
ID_LEN = 16
HUB_MAX_FRAME = 17 * 1024 * 1024  # relay caps phone messages at 16 MiB
LOCAL_MAX_FRAME = 16 * 1024 * 1024
PHONE_QUEUE = 64  # frames buffered per phone (honest traffic stays far below: 4 pipelined chunks)
HEARTBEAT = 20


def frame(kind: int, cid: str, payload: bytes) -> bytes:
    return bytes([kind]) + cid.encode("ascii") + payload


def sendable(code: Optional[int]) -> int:
    if code is None:
        return 1000
    if code == 1000 or (1001 <= code <= 1014 and code not in (1004, 1005, 1006)) or 3000 <= code <= 4999:
        return code
    return 1000


class CloudBridge:
    def __init__(self, local_port: int):
        self.local_port = local_port
        self.url = ""
        self.key = ""
        self.state = "off"  # off | connecting | connected | error
        self.error: Optional[str] = None
        self.phones = 0
        self._task: Optional[asyncio.Task] = None
        self._hub = None
        self._queues: Dict[str, asyncio.Queue] = {}
        self._phone_tasks: Set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()

    def status(self) -> dict:
        return {"url": self.url, "state": self.state, "error": self.error, "phones": self.phones}

    async def configure(self, url: str, key: str):
        async with self._config_lock:  # two quick Saves must not leave two hub loops fighting
            await self._stop()
            self.url = (url or "").strip().rstrip("/")
            if self.url.startswith("https://"):
                self.url = "wss://" + self.url[len("https://"):]
            elif self.url.startswith("http://"):
                self.url = "ws://" + self.url[len("http://"):]
            self.key = (key or "").strip()
            if self.url and self.key:
                self._task = asyncio.create_task(self._run())

    async def stop(self):
        async with self._config_lock:
            await self._stop()

    async def _stop(self):
        t, self._task = self._task, None
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        tasks = list(self._phone_tasks)
        for pt in tasks:
            pt.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.state, self.error, self.phones = "off", None, 0

    async def push_auth(self):
        """Tell the relay which phone/admin tokens are valid (hashes only) so it rejects others before upgrading."""
        hub = self._hub
        if not hub:
            return
        hashes = [hashlib.sha256(t.encode()).hexdigest() for t in (security.get_device_token(), security.get_token())]
        try:
            await self._send(hub, frame(K_AUTH, "0" * ID_LEN, json.dumps({"hashes": hashes}).encode()))
        except Exception:
            pass

    async def _heartbeat(self, hub):
        while True:
            await asyncio.sleep(HEARTBEAT)
            await self._send(hub, "hb")

    async def _run(self):
        delay = 1
        while True:
            self.state = "connecting"
            beat = None
            try:
                async with websockets.connect(
                        f"{self.url}/hub", additional_headers={"X-Hub-Key": self.key},
                        max_size=HUB_MAX_FRAME, ping_interval=20, ping_timeout=20, open_timeout=15) as hub:
                    self._hub = hub
                    await self.push_auth()
                    beat = asyncio.create_task(self._heartbeat(hub))
                    self.state, self.error, delay = "connected", None, 1
                    print(f"[CloudBridge] Connected to relay {self.url}")
                    async for msg in hub:
                        if isinstance(msg, bytes) and len(msg) > ID_LEN:
                            self._on_frame(hub, msg)
            except asyncio.CancelledError:
                raise
            except InvalidStatus as e:
                self.state = "error"
                self.error = ("Relay rejected the hub key" if e.response.status_code == 403
                              else f"Relay answered HTTP {e.response.status_code}")
            except ConnectionClosed as e:
                self.state = "error"
                if e.rcvd and e.rcvd.code == 4000:
                    self.error = "Another laptop/server took over this relay"
                    delay = 300  # don't fight over the relay every few seconds
                else:
                    self.error = f"Relay connection closed ({e.rcvd.code if e.rcvd else 'no close frame'})"
            except Exception as e:
                self.state, self.error = "error", f"{type(e).__name__}: {e}"
            finally:
                self._hub = None
                if beat:
                    beat.cancel()
                for q in list(self._queues.values()):
                    self._end(q)
            print(f"[CloudBridge] Relay connection lost ({self.error}); retrying in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300 if delay >= 300 else 30)

    @staticmethod
    def _end(q: asyncio.Queue):
        """Signal end-of-stream even if the queue is full."""
        while True:
            try:
                q.put_nowait(None)
                return
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass

    def _on_frame(self, hub, msg: bytes):
        kind, cid, body = msg[0], msg[1:1 + ID_LEN].decode("ascii", "replace"), msg[1 + ID_LEN:]
        if kind == K_OPEN:
            q: asyncio.Queue = asyncio.Queue(PHONE_QUEUE)
            self._queues[cid] = q
            try:
                info = json.loads(body)
            except ValueError:
                info = {}
            t = asyncio.create_task(self._phone(hub, cid, info, q))
            self._phone_tasks.add(t)
            t.add_done_callback(self._phone_tasks.discard)
        elif kind in (K_TEXT, K_BIN):
            q = self._queues.get(cid)
            if q:
                try:
                    q.put_nowait((kind, body))
                except asyncio.QueueFull:  # phone flooding faster than the laptop consumes: drop it
                    self._end(q)
        elif kind == K_CLOSE:
            q = self._queues.get(cid)
            if q:
                self._end(q)

    async def _send(self, hub, data):
        async with self._send_lock:
            await hub.send(data)

    async def _phone(self, hub, cid: str, info: dict, q: asyncio.Queue):
        """One remote phone <-> one local /ws/device connection."""
        code = 1000
        counted = False
        started = time.monotonic()
        headers = {"X-ACC-Token": str(info.get("token", "")),
                   "X-Forwarded-For": str(info.get("ip") or "0.0.0.0"),
                   "X-ACC-Relay": "cloudflare"}
        try:
            async with websockets.connect(f"ws://127.0.0.1:{self.local_port}/ws/device", additional_headers=headers,
                                          max_size=LOCAL_MAX_FRAME, ping_interval=None, open_timeout=10) as local:
                self.phones += 1
                counted = True

                async def phone_to_laptop():
                    while True:
                        item = await q.get()
                        if item is None:
                            return
                        kind, body = item
                        await local.send(body.decode("utf-8", "replace") if kind == K_TEXT else body)

                async def laptop_to_phone():
                    async for m in local:
                        if isinstance(m, str):
                            await self._send(hub, frame(K_TEXT, cid, m.encode("utf-8")))
                        else:
                            await self._send(hub, frame(K_BIN, cid, m))

                tasks = [asyncio.create_task(phone_to_laptop()), asyncio.create_task(laptop_to_phone())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in tasks:
                        t.cancel()
                code = local.close_code or 1000
        except InvalidStatus as e:
            code = 4401 if e.response.status_code in (401, 403) else 1011
        except asyncio.CancelledError:
            code = 1012
        except Exception:
            code = 1011
        finally:
            if counted:
                self.phones -= 1
            self._queues.pop(cid, None)
            print(f"[CloudBridge] relayed phone {cid} ({info.get('ip')}) closed with {code} after {time.monotonic() - started:.1f}s")
            try:
                await self._send(hub, frame(K_CLOSE, cid, sendable(code).to_bytes(2, "big")))
            except Exception:
                pass
