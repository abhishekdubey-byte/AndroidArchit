"""One control interface for every phone, wherever it is.

DeviceControl keeps ADBBridge's API, so the dashboard, REST routes and AI operator need no changes:
phones on USB / Wi-Fi ADB use ADB exactly as before, phones that are only reachable through the
companion app (other networks / the Cloudflare relay) get the same commands executed on the phone
by its accessibility service (see android-companion RemoteControl.kt).
"""
import base64
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from ..models.schemas import AppInfo, UINode
from .adb_bridge import ADBBridge, InputRejected, adb_typable, normalize_text

COMPANION_MISSING = "Install the Operator Companion app on this phone to use the camera"
from .relay import RelayClient, RelayRegistry
from .storage_service import StorageError


class DeviceControl(ADBBridge):
    def __init__(self, device_manager, relays: RelayRegistry, adb_path: Optional[str] = None):
        super().__init__(adb_path) if adb_path else super().__init__()  # same adb binary as ADBBridge
        self.dm = device_manager
        self.relays = relays

    # ---------- routing ----------
    def uses_adb(self, dev) -> bool:
        """USB/Wi-Fi ADB when connected (fastest); ACC_PREFER_RELAY=1 forces the companion path (testing)."""
        on_adb = dev.transport == "adb" and dev.status != "offline"
        if on_adb and os.getenv("ACC_PREFER_RELAY") == "1" and self.relays.get(dev.relay_id):
            return False
        return on_adb

    def _relay_for(self, serial: str) -> Optional[RelayClient]:
        """None = use ADB. A companion client = phone is remote. Raises if the phone is unreachable."""
        dev = self.dm.get_device(serial)
        if dev is None or self.uses_adb(dev):
            return None
        client = self.relays.get(dev.relay_id)
        if client is None:
            raise StorageError(f"{dev.name} is offline: not on USB/ADB and its companion app is not connected", 503)
        return client

    def _companion_alt(self, serial: str) -> Optional[RelayClient]:
        """The phone's connected companion app even while it is on USB (None if not connected)."""
        dev = self.dm.get_device(serial)
        return self.relays.get(dev.relay_id) if dev is not None and dev.relay_id else None

    @staticmethod
    async def _ctl(client: RelayClient, op: str, timeout: float = 20.0, **args) -> Tuple[Dict[str, Any], bytes]:
        return await client.request(op, args, timeout=timeout)

    # ---------- input ----------
    async def inject_tap(self, serial: str, x: int, y: int) -> bool:
        c = self._relay_for(serial)
        if c is None:
            return await super().inject_tap(serial, x, y)
        await self._ctl(c, "ctl.tap", x=x, y=y)
        return True

    async def inject_swipe(self, serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> bool:
        c = self._relay_for(serial)
        if c is None:
            return await super().inject_swipe(serial, x1, y1, x2, y2, duration_ms)
        await self._ctl(c, "ctl.swipe", x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms)
        return True

    async def inject_key(self, serial: str, key_name: str) -> bool:
        c = self._relay_for(serial)
        if c is None:
            return await super().inject_key(serial, key_name)
        data, _ = await self._ctl(c, "ctl.key", key=key_name)
        return bool(data.get("ok", True))

    async def inject_text(self, serial: str, text: str) -> bool:
        text = normalize_text(text)
        c = self._relay_for(serial)
        if c is None and not adb_typable(text):
            # adb can only type ASCII: on USB too, let the companion type Hindi, accents and emoji
            c = self._companion_alt(serial)
        if c is None:
            return await super().inject_text(serial, text)
        lines = text.split("\n")
        for i, line in enumerate(lines):           # the companion inserts text: a newline must press Enter
            if line:
                data, _ = await self._ctl(c, "ctl.text", text=line)
                if not data.get("ok", True):
                    raise InputRejected("The phone did not accept the text. Tap a text field on the phone first.")
            if i < len(lines) - 1:
                await self._ctl(c, "ctl.key", key="ENTER")
        return True

    # ---------- screen ----------
    async def capture_screen(self, serial: str, scale_width: int = 540, quality: int = 70) -> Tuple[bytes, str]:
        c = self._relay_for(serial)
        if c is None:
            return await super().capture_screen(serial, scale_width, quality)
        _, jpeg = await self._ctl(c, "ctl.screenshot", timeout=15, width=scale_width, quality=min(quality, 60))
        if not jpeg:
            raise StorageError("Phone returned an empty screenshot", 502)
        return jpeg, base64.b64encode(jpeg).decode("ascii")

    async def dump_ui_hierarchy(self, serial: str) -> List[UINode]:
        c = self._relay_for(serial)
        if c is None:
            # On USB too, prefer the companion app's accessibility read when it's connected: uiautomator waits for
            # the screen to go idle and hangs on animated screens (e.g. a playing WhatsApp status video).
            dev = self.dm.get_device(serial)
            alt = self.relays.get(dev.relay_id) if dev is not None and dev.relay_id else None
            if alt is not None:
                try:
                    data, _ = await self._ctl(alt, "ctl.ui", timeout=12)
                    nodes = [UINode(**n) for n in data.get("nodes", [])]
                    if nodes:
                        return nodes
                except Exception:
                    pass
            return await super().dump_ui_hierarchy(serial)
        data, _ = await self._ctl(c, "ctl.ui", timeout=20)
        return [UINode(**n) for n in data.get("nodes", [])]

    async def foreground_package(self, serial: str) -> Optional[str]:
        c = self._relay_for(serial)
        if c is None:
            return await super().foreground_package(serial)
        data, _ = await self._ctl(c, "ctl.ui", timeout=12)  # accessibility root node = app in front
        nodes = data.get("nodes") or []
        return (nodes[0].get("package") or None) if nodes else None

    # ---------- apps / calls ----------
    async def get_installed_apps(self, serial: str) -> List[AppInfo]:
        c = self._relay_for(serial)
        if c is None:
            return await super().get_installed_apps(serial)
        data, _ = await self._ctl(c, "ctl.apps", timeout=20)
        return [AppInfo(**a) for a in data.get("apps", [])]

    async def launch_package(self, serial: str, package_name: str) -> bool:
        c = self._relay_for(serial)
        if c is None:
            return await super().launch_package(serial, package_name)
        await self._ctl(c, "ctl.launch", package=package_name)
        return True

    async def start_phone_call(self, serial: str, target: str) -> bool:
        c = self._relay_for(serial)
        if c is None:
            return await super().start_phone_call(serial, target)
        await self._ctl(c, "ctl.call", number=target)
        return True

    async def get_telephony_state(self, serial: str) -> Dict[str, Any]:
        c = self._relay_for(serial)
        if c is None:
            return await super().get_telephony_state(serial)
        dev = self.dm.get_device(serial)
        return self.dm.device_call_states.get(dev.id, {"state": 0, "incoming_number": ""})

    async def answer_incoming_call(self, serial: str) -> bool:
        c = self._relay_for(serial)
        if c is None:
            return await super().answer_incoming_call(serial)
        await c.ws.send_text(json.dumps({"type": "ACCEPT_CALL"}))
        return True

    async def end_call(self, serial: str) -> bool:
        c = self._relay_for(serial)
        if c is None:
            return await super().end_call(serial)
        await c.ws.send_text(json.dumps({"type": "END_CALL"}))
        return True

    async def wake(self, serial: str) -> bool:
        c = self._relay_for(serial)
        if c is None:
            await self.run_device_shell(serial, "input keyevent 224")
            return True
        await self._ctl(c, "ctl.key", key="WAKEUP")
        return True
