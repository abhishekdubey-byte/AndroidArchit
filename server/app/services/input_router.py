"""One entry point for taps, swipes, live pointer gestures, wheel scrolls, keys and text.

USB phones: the scrcpy control socket (a few ms per event) — the live mirror's session when mirroring is on,
otherwise a control-only session started on first use and closed after a minute idle. Touch and scroll events
need the stream's frame size, which only a mirror session has: without one they go through ADB `input`
(keys and text still use the control socket). Any socket error before anything was sent falls back to ADB or the
companion app; a gesture that was half sent is not replayed (that would double-tap). Text scrcpy cannot type
(ø, Devanagari, emoji…) goes to the companion app (UnsupportedText when it is not connected).
"""
import asyncio
import time
from typing import Callable, Dict, Optional

from ..config import ADB_PATH, FAST_MIRROR
from .adb_bridge import adb_typable, normalize_text
from .scrcpy_protocol import (ACTION_DOWN, ACTION_MOVE, ACTION_UP, KEYCODES, key_event, scroll_event,
                              scrcpy_can_type, text_events, touch_event)
from .scrcpy_session import ScrcpySession

MOVE_STEP_S = 0.008      # target spacing of swipe moves; positions follow the clock, not the sleep count
TAP_SLOP_PX = 30         # a press that moved less than this is a tap


class InputRouter:
    IDLE_CLOSE = 60.0       # seconds before an unused control-only session is closed
    RETRY_AFTER = 60.0      # seconds before retrying fast input after a failure

    def __init__(self, control, device_manager, mirror_sessions: Dict[str, object], *, fast_input: bool = FAST_MIRROR,
                 session_factory: Callable[..., object] = None, adb_path: str = ADB_PATH):
        self.control = control                  # DeviceControl: ADB `input` / companion app
        self.dm = device_manager
        self.mirror_sessions = mirror_sessions  # the hub's running mirrors, keyed by device id (shared dict)
        self.fast_input = fast_input
        self.session_factory = session_factory or (lambda serial: ScrcpySession(adb_path, serial, video=False))
        self._control_sessions: Dict[str, object] = {}
        self._last_used: Dict[str, float] = {}
        self._failed: Dict[str, float] = {}
        self._starting: Dict[str, asyncio.Lock] = {}
        self._gesture_locks: Dict[str, asyncio.Lock] = {}
        self._pointer: Dict[str, dict] = {}
        self._reaper: Optional[asyncio.Task] = None

    # ---------------- sessions ----------------
    def _device(self, device_id: str):
        dev = self.dm.get_device(device_id)
        if dev is None:
            raise KeyError(device_id)
        return dev

    @staticmethod
    def _on_usb(dev) -> bool:
        return dev.transport == "adb" and dev.status != "offline"

    @staticmethod
    def _has_frame(session) -> bool:
        fw, fh = getattr(session, "frame_size", (0, 0))
        return fw > 0 and fh > 0

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
            if time.time() - self._failed.get(dev.id, 0) < self.RETRY_AFTER:
                return None                     # another caller just failed: don't sit through a second failing start
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

    async def _touch_session(self, dev):
        """A session that can take touch/scroll events (needs the mirror's frame size), else None → ADB."""
        s = await self._session(dev)
        return s if s is not None and self._has_frame(s) else None

    async def _drop(self, dev_id: str, session) -> None:
        if self._control_sessions.get(dev_id) is session:
            del self._control_sessions[dev_id]
            try:
                await session.stop()
            except Exception:
                pass

    def forget_device(self, dev_id: str) -> None:
        """The phone (re)connected: forget old failures and any session tied to the previous connection."""
        self._failed.pop(dev_id, None)
        self._pointer.pop(dev_id, None)
        s = self._control_sessions.pop(dev_id, None)
        if s is not None:
            asyncio.ensure_future(s.stop())

    async def _reap_idle(self):
        while self._control_sessions:
            await asyncio.sleep(5)
            now = time.time()
            for dev_id, s in list(self._control_sessions.items()):
                if dev_id in self.mirror_sessions:
                    self._last_used[dev_id] = now    # mirroring: the mirror session carries input anyway
                    await self._drop(dev_id, s)
                elif now - self._last_used.get(dev_id, 0) > self.IDLE_CLOSE:
                    await self._drop(dev_id, s)

    async def close(self):
        for dev_id, s in list(self._control_sessions.items()):
            await self._drop(dev_id, s)

    def _fallback_name(self, dev) -> str:
        return "adb" if self._on_usb(dev) else "companion"

    def _gesture_lock(self, dev_id: str) -> asyncio.Lock:
        return self._gesture_locks.setdefault(dev_id, asyncio.Lock())

    @staticmethod
    def clamp_duration(ms) -> int:
        try:
            return max(1, min(10_000, int(ms)))
        except (TypeError, ValueError):
            return 300

    @staticmethod
    def _scale(dev, session, x: int, y: int):
        fw, fh = session.frame_size
        dw = dev.display.width or fw
        dh = dev.display.height or fh
        # Dashboards send positions in the phone's portrait pixel space, normalised to the picture shown;
        # scaling by the current frame size keeps taps right after a rotation too.
        sx = min(fw - 1, max(0, round(int(x) * fw / dw)))
        sy = min(fh - 1, max(0, round(int(y) * fh / dh)))
        return sx, sy, fw, fh

    async def _send_seq(self, dev, session, messages) -> int:
        """Send in order; returns how many went out (stops and drops a control session at the first error)."""
        sent = 0
        try:
            for m in messages:
                await session.send(m)
                sent += 1
        except Exception:
            await self._drop(dev.id, session)
        return sent

    # ---------------- actions ----------------
    async def tap(self, device_id: str, x: int, y: int) -> str:
        dev = self._device(device_id)
        async with self._gesture_lock(dev.id):
            s = await self._touch_session(dev)
            if s is not None:
                sx, sy, fw, fh = self._scale(dev, s, x, y)
                msgs = [touch_event(ACTION_DOWN, sx, sy, fw, fh), touch_event(ACTION_UP, sx, sy, fw, fh)]
                sent = await self._send_seq(dev, s, msgs)
                if sent == len(msgs):
                    return "scrcpy"
                if sent:
                    return "interrupted"
            await self.control.inject_tap(dev.serial, x, y)
            return self._fallback_name(dev)

    async def swipe(self, device_id: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
        dev = self._device(device_id)
        duration_ms = self.clamp_duration(duration_ms)
        async with self._gesture_lock(dev.id):
            s = await self._touch_session(dev)
            if s is not None:
                ax, ay, fw, fh = self._scale(dev, s, x1, y1)
                bx, by, _, _ = self._scale(dev, s, x2, y2)
                if not await self._send_seq(dev, s, [touch_event(ACTION_DOWN, ax, ay, fw, fh)]):
                    await self.control.inject_swipe(dev.serial, x1, y1, x2, y2, duration_ms)
                    return self._fallback_name(dev)
                loop = asyncio.get_running_loop()
                start, total = loop.time(), duration_ms / 1000
                while True:
                    await asyncio.sleep(MOVE_STEP_S)
                    frac = min(1.0, (loop.time() - start) / total)   # follow the clock: timer granularity can't stretch it
                    px, py = round(ax + (bx - ax) * frac), round(ay + (by - ay) * frac)
                    if not await self._send_seq(dev, s, [touch_event(ACTION_MOVE, px, py, fw, fh)]):
                        return "interrupted"
                    if frac >= 1.0:
                        break
                if await self._send_seq(dev, s, [touch_event(ACTION_UP, bx, by, fw, fh)]):
                    return "scrcpy"
                return "interrupted"
            await self.control.inject_swipe(dev.serial, x1, y1, x2, y2, duration_ms)
            return self._fallback_name(dev)

    async def pointer(self, device_id: str, action: str, x: int, y: int) -> str:
        """Live finger from the dashboard: 'down' / 'move' / 'up' as they happen (no replay after release)."""
        dev = self._device(device_id)
        now = time.monotonic()
        if action == "down":
            s = await self._touch_session(dev)
            state = {"x0": x, "y0": y, "t0": now, "x": x, "y": y, "s": s}
            self._pointer[dev.id] = state
            if s is not None:
                sx, sy, fw, fh = self._scale(dev, s, x, y)
                if await self._send_seq(dev, s, [touch_event(ACTION_DOWN, sx, sy, fw, fh)]):
                    return "scrcpy"
                state["s"] = None
            return self._fallback_name(dev)
        state = self._pointer.get(dev.id)
        if state is None:
            return "ignored"                    # a move/up without its down (e.g. after a reconnect)
        state["x"], state["y"] = x, y
        s = state["s"]
        if s is not None:
            sx, sy, fw, fh = self._scale(dev, s, x, y)
            act = ACTION_MOVE if action == "move" else ACTION_UP
            ok = await self._send_seq(dev, s, [touch_event(act, sx, sy, fw, fh)])
            if action != "move":
                self._pointer.pop(dev.id, None)
            if not ok:
                state["s"] = None
                return "interrupted"
            return "scrcpy"
        if action != "up":
            return self._fallback_name(dev)     # ADB can't stream a finger: replayed as one gesture on release
        self._pointer.pop(dev.id, None)
        if abs(x - state["x0"]) + abs(y - state["y0"]) > TAP_SLOP_PX:
            await self.control.inject_swipe(dev.serial, state["x0"], state["y0"], x, y,
                                            self.clamp_duration((now - state["t0"]) * 1000))
        else:
            await self.control.inject_tap(dev.serial, x, y)
        return self._fallback_name(dev)

    async def scroll(self, device_id: str, x: int, y: int, vscroll: float, hscroll: float = 0.0) -> str:
        """Mouse wheel: ±1.0 per notch (positive = up)."""
        dev = self._device(device_id)
        vscroll = max(-1.0, min(1.0, float(vscroll)))
        hscroll = max(-1.0, min(1.0, float(hscroll)))
        s = await self._touch_session(dev)
        if s is not None:
            sx, sy, fw, fh = self._scale(dev, s, x, y)
            if await self._send_seq(dev, s, [scroll_event(sx, sy, fw, fh, hscroll, vscroll)]):
                return "scrcpy"
        # ADB has no wheel: drag the content instead (wheel down = finger moves up)
        await self.control.inject_swipe(dev.serial, x, y, x, int(y + vscroll * 400), 150)
        return self._fallback_name(dev)

    async def key(self, device_id: str, name: str) -> str:
        dev = self._device(device_id)
        code = KEYCODES.get(str(name).upper())
        s = await self._session(dev) if code is not None else None
        if s is not None:
            sent = await self._send_seq(dev, s, [key_event(ACTION_DOWN, code), key_event(ACTION_UP, code)])
            if sent == 2:
                return "scrcpy"
            if sent:
                return "interrupted"
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
            if s is not None:
                msgs = text_events(text)
                sent = await self._send_seq(dev, s, msgs)
                if sent == len(msgs):
                    return "scrcpy"
                if sent:
                    return "interrupted"        # part was typed: typing it all again would duplicate it
        await self.control.inject_text(dev.serial, text)
        if not self._on_usb(dev):
            return "companion"
        return "adb" if adb_typable(text) else "companion"
