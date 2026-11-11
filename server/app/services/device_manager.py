import asyncio
import secrets
import shlex
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any
from .adb_bridge import ADBBridge
from ..models.schemas import Device, BatteryInfo, DisplayInfo

RELAY_ID_FILE = "/sdcard/Android/media/com.operator.companion/relay_id"

class DeviceManager:
    def __init__(self, adb_bridge: Optional[ADBBridge] = None):
        self.adb = adb_bridge or ADBBridge()
        self.devices: Dict[str, Device] = {}
        self.serial_to_id: Dict[str, str] = {}
        self.device_call_states: Dict[str, Dict[str, Any]] = {}
        self.diagnostics_log: List[Dict[str, Any]] = []
        self._listeners: List[Callable[[str, Any], Any]] = []
        self._sync_task: Optional[asyncio.Task] = None
        self._telephony_task: Optional[asyncio.Task] = None
        self._is_running = False
        self.relay_online_ids: set = set()
        self._relay_id_cache: Dict[str, tuple] = {}  # serial -> (relay_id or "", checked_at)
        self._last_wake: Dict[str, float] = {}
        self._wake_backoff: Dict[str, float] = {}
        self.relays = None  # RelayRegistry, attached in main.py

    def add_diagnostic_report(self, report: Dict[str, Any]):
        self.diagnostics_log.insert(0, report)
        if len(self.diagnostics_log) > 100:
            self.diagnostics_log.pop()

        dev_id = report.get("device_id")
        dev = self.get_device(dev_id) if dev_id else None
        if dev:
            dev.diagnostic_history.insert(0, report)
            if len(dev.diagnostic_history) > 50:
                dev.diagnostic_history.pop()

    def update_device_capabilities(self, device_id: str, caps: Dict[str, Any]):
        dev = self.get_device(device_id) or next(
            (d for d in self.devices.values() if d.relay_id and d.relay_id == device_id), None)
        if dev:
            if "camera_capabilities" in caps:
                dev.hardware_capabilities = caps["camera_capabilities"]
            if "permissions" in caps:
                dev.permissions = caps["permissions"]
            if "power_and_lockscreen" in caps:
                dev.power_and_lockscreen = caps["power_and_lockscreen"]
            if "device_info" in caps:
                dinfo = caps["device_info"]
                dev.manufacturer = dinfo.get("manufacturer", dev.manufacturer)
                dev.sdk_int = dinfo.get("sdk_int", dev.sdk_int)
                dev.android_version = dinfo.get("android_version", dev.android_version)
            return dev
        return None

    def add_listener(self, callback: Callable[[str, Any], Any]):
        self._listeners.append(callback)

    async def _notify(self, event_type: str, data: Any):
        if event_type == "DEVICE_CONNECTED":
            try:  # each phone gets its own capture folder on this laptop as soon as it connects
                from ..config import register_device_dir
                register_device_dir(data)
            except Exception as e:
                print(f"[DeviceManager] could not create the capture folder: {e}")
        for listener in self._listeners:
            try:
                res = listener(event_type, data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                print(f"[DeviceManager] Listener error: {e}")

    async def start(self):
        self._is_running = True
        try:   # never let a stuck first USB scan block startup (and Ctrl+C); the poll loop keeps scanning
            await asyncio.wait_for(self.refresh_devices(), 25)
        except asyncio.TimeoutError:
            print("[Startup] USB scan is slow — continuing; phones will appear as soon as adb answers")
        self._sync_task = asyncio.create_task(self._background_poll_loop())
        self._telephony_task = asyncio.create_task(self._telephony_poll_loop())

    async def stop(self):
        self._is_running = False
        if self._sync_task:
            self._sync_task.cancel()
        if self._telephony_task:
            self._telephony_task.cancel()
        try:
            if self._sync_task:
                await self._sync_task
            if self._telephony_task:
                await self._telephony_task
        except asyncio.CancelledError:
            pass

    async def refresh_devices(self) -> List[Device]:
        """Discovers and refreshes state for all connected devices."""
        attached = await self.adb.list_attached_devices()
        current_serials = {d_meta["serial"] for d_meta in attached}
        # A serial that went away may come back as a different phone (same Wi-Fi ip:port, emulator-5554…):
        # never let it inherit the old phone's companion identity.
        self.prune_relay_id_cache(current_serials)

        for d_meta in attached:
            serial = d_meta["serial"]
            try:
                dev = await self.adb.get_device_details(serial)
                old_dev = self.devices.get(dev.id)
                dev.relay_id = await self._relay_id_for(serial)
                dev.relay_online = bool(dev.relay_id) and dev.relay_id in self.relay_online_ids
                if dev.relay_id and not dev.relay_online:
                    if self.relays and self.relays.recently_refused(dev.relay_id):
                        self._schedule_rekey(dev)  # reinstalled app lost its identity secret
                    else:
                        self._schedule_wake(dev)
                if dev.relay_id and dev.relay_id != dev.id and dev.relay_id in self.devices:
                    del self.devices[dev.relay_id]  # remote-only entry is now the same phone on USB
                if old_dev:
                    dev.diagnostic_history = old_dev.diagnostic_history
                    if old_dev.hardware_capabilities:
                        dev.hardware_capabilities = old_dev.hardware_capabilities
                    if old_dev.permissions:
                        dev.permissions = old_dev.permissions
                prev_id = self.serial_to_id.get(serial)
                if prev_id and prev_id != dev.id and prev_id in self.devices:
                    del self.devices[prev_id]  # one USB serial = one phone: drop a stale entry under another id
                    await self._notify("DEVICE_REMOVED", {"id": prev_id})
                self.devices[dev.id] = dev
                self.serial_to_id[serial] = dev.id
                
                # Check for updates or new device
                if not old_dev or old_dev.status != "online":
                    await self._notify("DEVICE_CONNECTED", dev)
                elif old_dev.battery.level != dev.battery.level or old_dev.active_package != dev.active_package:
                    await self._notify("TELEMETRY_UPDATE", dev)
            except Exception as e:
                print(f"[DeviceManager] Error querying device {serial}: {e}")

        # Mark disconnected devices
        for dev_id, dev in list(self.devices.items()):
            if dev.transport == "adb" and dev.serial not in current_serials:
                if dev.relay_online:
                    # Unplugged but still reachable through the companion relay
                    dev.transport = "companion_ws"
                    await self._notify("TELEMETRY_UPDATE", dev)
                elif dev.status != "offline":
                    dev.status = "offline"
                    await self._notify("DEVICE_DISCONNECTED", dev)

        return list(self.devices.values())

    async def _background_poll_loop(self):
        while self._is_running:
            try:
                await self.refresh_devices()
            except Exception as e:
                print(f"[DeviceManager] Polling error: {e}")
            await asyncio.sleep(4.0)

    async def _telephony_poll_loop(self):
        while self._is_running:
            try:
                for dev in self.get_online_devices():
                    if dev.transport == "adb":
                        tele = await self.adb.get_telephony_state(dev.serial)
                        old_tele = self.device_call_states.get(dev.id, {"state": 0, "incoming_number": ""})
                        curr_state = tele["state"]
                        old_state = old_tele["state"]

                        if curr_state != old_state:
                            self.device_call_states[dev.id] = tele
                            if curr_state == 1:
                                # Ringing (Incoming Call)
                                await self._notify("INCOMING_CALL", {
                                    "device_id": dev.id,
                                    "device_name": dev.name,
                                    "phone_number": tele["incoming_number"] or "Unknown Caller",
                                    "caller_name": "Caller",
                                    "state": "ringing"
                                })
                            elif curr_state == 2:
                                # Active Call (Answered)
                                await self._notify("CALL_ACTIVE", {
                                    "device_id": dev.id,
                                    "device_name": dev.name,
                                    "phone_number": tele["incoming_number"],
                                    "state": "active"
                                })
                            elif curr_state == 0:
                                # Call Ended / Idle
                                await self._notify("CALL_ENDED", {
                                    "device_id": dev.id,
                                    "device_name": dev.name,
                                    "state": "idle"
                                })
            except Exception as e:
                pass
            await asyncio.sleep(1.0)


    def _schedule_wake(self, dev: Device):
        """Paired phone on USB but its app is down (e.g. force-stopped by the maker's battery manager):
        restart it through the adb-only receiver. Runs in the background so polling never stalls."""
        now = time.time()
        if now - self._last_wake.get(dev.serial, 0) < self._wake_backoff.get(dev.serial, 60):
            return
        self._last_wake[dev.serial] = now
        asyncio.create_task(self._wake_companion(dev.serial, dev.name))

    async def _wake_companion(self, serial: str, name: str):
        try:
            out = await asyncio.wait_for(self.adb.run_device_shell(
                serial, "am broadcast -f 0x10000000 --include-stopped-packages "
                        "-n com.operator.companion/.relay.PairReceiver --ez wake true"), 15)
        except Exception:
            return
        if "result=0" in out:  # 0 = service started (4 = already running / disconnected by the user)
            self._wake_backoff.pop(serial, None)
            print(f"[DeviceManager] Restarted the companion app on {name} over USB (it had been stopped)")
        elif "result=3" in out:
            if serial not in self._wake_backoff:
                print(f"[DeviceManager] {name}: Android blocked restarting the companion app — open it once on the phone")
            self._wake_backoff[serial] = 600

    def _schedule_rekey(self, dev: Device):
        """The laptop refuses this phone's relay link because its identity secret no longer matches
        (the app was reinstalled or its data cleared). The phone is on USB, i.e. physically here:
        hand it a fresh secret through the adb-only receiver so it reconnects without a manual reset."""
        now = time.time()
        key = "rekey:" + dev.serial
        if now - self._last_wake.get(key, 0) < self._wake_backoff.get(key, 30):
            return
        self._last_wake[key] = now
        asyncio.create_task(self._rekey_companion(dev.serial, dev.name, dev.relay_id, key))

    async def _rekey_companion(self, serial: str, name: str, relay_id: str, key: str):
        # relay_id comes from shared storage the phone controls, so before handing out that identity
        # make sure the receiving app is ours (release key) and it confirms the id is its own.
        if not await self._companion_signed_by_us(serial):
            if key not in self._wake_backoff:
                print(f"[DeviceManager] {name}: relay identity is locked; the installed app is not signed "
                      "with this laptop's release key, so it is not re-keyed automatically (use Pair phones → Reset)")
            self._wake_backoff[key] = 600
            return
        secret = secrets.token_urlsafe(24)  # [A-Za-z0-9_-]: shell-safe
        # Bind first: the phone reconnects the moment it stores the secret. Nobody else knows it.
        prev = self.relays.rebind(relay_id, secret)
        try:
            out = await asyncio.wait_for(self.adb.run_device_shell(
                serial, "am broadcast -f 0x10000000 --include-stopped-packages "
                        f"-n com.operator.companion/.relay.PairReceiver --es id {shlex.quote(relay_id)} --es secret {secret}"), 15)
        except Exception:
            out = ""
        if "result=5" in out:  # 5 = the app stored the secret for exactly this id
            self.relays.refused.pop(relay_id, None)
            self._wake_backoff.pop(key, None)
            print(f"[DeviceManager] {name}: re-issued the relay identity over USB (app was reinstalled)")
        else:
            self.relays.restore(relay_id, prev, secret)
            if key not in self._wake_backoff:
                print(f"[DeviceManager] {name}: relay identity is locked and the app did not accept a new one "
                      f"over USB ({out.strip()[-60:] or 'no reply'}) — update the app or use Pair phones → Reset")
            self._wake_backoff[key] = 600

    async def _companion_signed_by_us(self, serial: str) -> bool:
        from . import app_update
        ours = await asyncio.to_thread(app_update.our_signer_digest)
        if not ours:
            return False
        try:
            path = (await self.adb.run_device_shell(serial, "pm path com.operator.companion")).strip().splitlines()
            path = next((l[8:].strip() for l in path if l.startswith("package:") and l.strip().endswith("base.apk")), "")
            if not path:
                return False
            with tempfile.TemporaryDirectory() as d:
                local = Path(d) / "base.apk"
                if not await asyncio.wait_for(self.adb.pull_file(serial, path, local), 60):
                    return False
                return await asyncio.to_thread(app_update.signer_digest, local) == ours
        except Exception:
            return False

    def prune_relay_id_cache(self, current_serials) -> None:
        for serial in list(self._relay_id_cache):
            if serial not in current_serials:
                del self._relay_id_cache[serial]

    async def _relay_id_for(self, serial: str) -> str:
        rid, checked = self._relay_id_cache.get(serial, ("", 0.0))
        if rid or time.time() - checked < 30:
            return rid
        try:
            rid = (await self.adb.run_device_shell(serial, f"cat {RELAY_ID_FILE} 2>/dev/null")).strip()
        except Exception:
            rid = ""
        rid = rid if rid and " " not in rid and len(rid) < 200 else ""
        self._relay_id_cache[serial] = (rid, time.time())
        return rid

    async def register_relay(self, relay_id: str, info: Dict[str, Any]):
        self.relay_online_ids.add(relay_id)
        linked = next((d for d in self.devices.values() if d.relay_id == relay_id and d.id != relay_id), None)
        if linked:
            linked.relay_online = True
            linked.app_edition = "lite" if info.get("edition") == "lite" else "full"
            linked.device_owner = bool(info.get("device_owner"))
            if linked.status == "offline":
                linked.status, linked.transport = "online", "companion_ws"
            await self._notify("DEVICE_CONNECTED", linked)
            return linked
        model = str(info.get("model") or "Android")
        brand = str(info.get("brand") or "")
        dev = self.devices.get(relay_id) or Device(
            id=relay_id, serial=relay_id, name=f"{brand.title()} {model}".strip(), model=model, brand=brand or "android",
            transport="companion_ws", relay_id=relay_id)
        dev.manufacturer = str(info.get("manufacturer") or dev.manufacturer)
        dev.android_version = str(info.get("android_version") or dev.android_version)
        dev.sdk_int = int(info.get("sdk_int") or dev.sdk_int)
        if isinstance(info.get("battery_level"), int):
            dev.battery = BatteryInfo(level=info["battery_level"])
        if info.get("display_width") and info.get("display_height"):
            dev.display = DisplayInfo(width=int(info["display_width"]), height=int(info["display_height"]),
                                      density=int(info.get("display_density") or dev.display.density))
        dev.status, dev.relay_online = "online", True
        dev.app_edition = "lite" if info.get("edition") == "lite" else "full"
        dev.device_owner = bool(info.get("device_owner"))
        self.devices[relay_id] = dev
        await self._notify("DEVICE_CONNECTED", dev)
        return dev

    async def unregister_relay(self, relay_id: str):
        self.relay_online_ids.discard(relay_id)
        for dev in self.devices.values():
            if dev.relay_id == relay_id:
                dev.relay_online = False
                if dev.transport == "companion_ws" and dev.status != "offline":
                    dev.status = "offline"
                    await self._notify("DEVICE_DISCONNECTED", dev)

    def get_device(self, device_id_or_serial: str) -> Optional[Device]:
        if device_id_or_serial in self.devices:
            return self.devices[device_id_or_serial]
        if device_id_or_serial in self.serial_to_id:
            return self.devices[self.serial_to_id[device_id_or_serial]]
        return None

    def get_all_devices(self) -> List[Device]:
        return list(self.devices.values())

    def get_online_devices(self) -> List[Device]:
        return [d for d in self.devices.values() if d.status == "online"]
