"""One entry point for taps, swipes, keys and text.

USB phones: the scrcpy control socket (a few ms per event) — the live mirror's session when mirroring is on,
otherwise a control-only session started on first use and closed after a minute idle. Anything the socket
cannot do (or any socket error) falls back to the existing path: ADB `input` or the companion app.
Text scrcpy cannot type (Devanagari, emoji…) goes to the companion app (UnsupportedText when not connected).
"""
import asyncio
import time
from typing import Callable, Dict, Optional

from ..config import ADB_PATH, FAST_MIRROR
from .adb_bridge import adb_typable, normalize_text
from .scrcpy_protocol import (ACTION_DOWN, ACTION_MOVE, ACTION_UP, KEYCODES, key_event, scrcpy_can_type,
                              text_events, touch_event)
from .scrcpy_session import ScrcpySession

MOVE_INTERVAL_MS = 16


class InputRouter:
    IDLE_CLOSE = 60.0       # seconds before an unused control-only session is closed
    RETRY_AFTER = 60.0      # seconds before retrying fast input after a failure

    def __init__(self, control, device_manager, mirror_sessions: Dict[str, object], *, fast_input: bool = FAST_MIRROR,
                 session_factory: Callable[..., object] = None, adb_path: str = ADB_PATH):
        self.control = control                  # DeviceControl: ADB `input` / companion app
        self.dm = device_manager
        self.mirror_sessions = mirror_sessions  # the hub's running mirrors (shared dict)
        self.fast_input = fast_input
        self.session_factory = session_factory or (lambda serial: ScrcpySession(adb_path, serial, video=False))
        self._control_sessions: Dict[str, object] = {}
        self._last_used: Dict[str, float] = {}
        self._failed: Dict[str, float] = {}
        self._starting: Dict[str, asyncio.Lock] = {}
        self._reaper: Optional[asyncio.Task] = None

    # ---- routing ----
    def _device(self, device_id: str):
        dev = self.dm.get_device(device_id)
        if dev is None:
            raise KeyError(device_id)
        return dev

    @staticmethod
    def _on_usb(dev) -> bool:
        return dev.transport == "adb" and dev.status != "offline"

    async def _session(self, dev):
        if not self._on_usb(dev):
            return None
        s = self.mirror_sessions.get(dev.id)
        if s is not None:
            return s
        if not self.fast_input or time.time() - self._failed.get(dev.id, 0) < self.RETRY_AFTER:
            return None
        s = self._control_sessions.get(dev.id)
        if s is not None:
            self._last_used[dev.id] = time.time()
            return s
        lock = self._starting.setdefault(dev.id, asyncio.Lock())
        async with lock:
            if dev.id in self._control_sessions:
                return self._control_sessions[dev.id]
            s = self.session_factory(dev.serial)
            try:
                await s.start()
            except Exception:
                self._failed[dev.id] = time.time()
                return None
            self._control_sessions[dev.id] = s
            self._last_used[dev.id] = time.time()
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.create_task(self._reap_idle())
            return s

    async def _drop(self, dev_id: str, session) -> None:
        """A session failed: never use it again (the mirror loop restarts its own)."""
        if self._control_sessions.get(dev_id) is session:
            del self._control_sessions[dev_id]
            try:
                await session.stop()
            except Exception:
                pass

    async def _reap_idle(self):
        while self._control_sessions:
            await asyncio.sleep(5)
            now = time.time()
            for dev_id, s in list(self._control_sessions.items()):
                if now - self._last_used.get(dev_id, 0) > self.IDLE_CLOSE:
                    await self._drop(dev_id, s)

    async def close(self):
        for dev_id, s in list(self._control_sessions.items()):
            await self._drop(dev_id, s)

    def _fallback_name(self, dev) -> str:
        return "adb" if self._on_usb(dev) else "companion"

    @staticmethod
    def _scale(dev, session, x: int, y: int):
        fw, fh = session.frame_size
        dw, dh = dev.display.width or fw, dev.display.height or fh
        # Dashboards send positions in the phone's portrait pixel space, normalised to the picture shown;
        # scaling by the current frame size keeps taps right after a rotation too.
        return round(x * fw / dw), round(y * fh / dh), fw, fh

    async def _send_all(self, dev, session, messages) -> bool:
        try:
            for m in messages:
                await session.send(m)
            return True
        except Exception:
            await self._drop(dev.id, session)
            return False

    # ---- actions ----
    async def tap(self, device_id: str, x: int, y: int) -> str:
        dev = self._device(device_id)
        s = await self._session(dev)
        if s is not None:
            sx, sy, fw, fh = self._scale(dev, s, x, y)
            if await self._send_all(dev, s, [touch_event(ACTION_DOWN, sx, sy, fw, fh),
                                             touch_event(ACTION_UP, sx, sy, fw, fh)]):
                return "scrcpy"
        await self.control.inject_tap(dev.serial, x, y)
        return self._fallback_name(dev)

    async def swipe(self, device_id: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
        dev = self._device(device_id)
        s = await self._session(dev)
        if s is not None:
            ax, ay, fw, fh = self._scale(dev, s, x1, y1)
            bx, by, _, _ = self._scale(dev, s, x2, y2)
            steps = max(1, int(duration_ms) // MOVE_INTERVAL_MS)
            ok = await self._send_all(dev, s, [touch_event(ACTION_DOWN, ax, ay, fw, fh)])
            for i in range(1, steps + 1):
                if not ok:
                    break
                await asyncio.sleep(MOVE_INTERVAL_MS / 1000)
                px, py = round(ax + (bx - ax) * i / steps), round(ay + (by - ay) * i / steps)
                ok = await self._send_all(dev, s, [touch_event(ACTION_MOVE, px, py, fw, fh)])
            if ok and await self._send_all(dev, s, [touch_event(ACTION_UP, bx, by, fw, fh)]):
                return "scrcpy"
        await self.control.inject_swipe(dev.serial, x1, y1, x2, y2, duration_ms)
        return self._fallback_name(dev)

    async def key(self, device_id: str, name: str) -> str:
        dev = self._device(device_id)
        code = KEYCODES.get(str(name).upper())
        s = await self._session(dev) if code is not None else None
        if s is not None and await self._send_all(dev, s, [key_event(ACTION_DOWN, code), key_event(ACTION_UP, code)]):
            return "scrcpy"
        await self.control.inject_key(dev.serial, name)
        return self._fallback_name(dev)

    async def text(self, device_id: str, text: str) -> str:
        """Raises UnsupportedText when nothing connected can type it."""
        dev = self._device(device_id)
        text = normalize_text(text)
        if not text:
            return "scrcpy"
        if scrcpy_can_type(text):
            s = await self._session(dev)
            if s is not None and await self._send_all(dev, s, text_events(text)):
                return "scrcpy"
        await self.control.inject_text(dev.serial, text)
        if not self._on_usb(dev):
            return "companion"
        return "adb" if adb_typable(text) else "companion"
