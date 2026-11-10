"""Uninstall / update / reinstall the companion app on a phone, from the dashboard.

Path per phone (decided here, shown on the dashboard before the user confirms):
* USB / Wi-Fi ADB reachable -> `adb uninstall` / `adb install -r` (always silent).
* Otherwise over the companion relay -> APP_UNINSTALL / APP_UPDATE_FORCE; the phone answers with
  APP_LIFECYCLE_STATUS (accepted / done / needs_tap / failed), forwarded to dashboards by the hub.
  Device owner installs/uninstalls without prompts, Android 12+ self-updates silently once the app
  is its own installer of record, everything else needs one tap on the phone.
"""
import shlex

from fastapi import APIRouter, HTTPException

from ..services import app_update
from ..services.device_manager import DeviceManager
from ..services.storage_service import StorageError, StorageService

PACKAGE = "com.operator.companion"
ADMIN_COMPONENT = f"{PACKAGE}/.admin.CompanionAdminReceiver"
_LOG = "[AppLifecycle]"


def create_app_lifecycle_router(device_manager: DeviceManager, storage: StorageService, ws_hub) -> APIRouter:
    router = APIRouter(prefix="/api/devices/{device_id}", tags=["app-lifecycle"])
    busy = set()  # device ids with an adb install/uninstall in flight

    def device(device_id: str):
        dev = device_manager.get_device(device_id)
        if not dev:
            raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
        return dev

    def adb_ok(dev) -> bool:
        return dev.transport == "adb" and dev.status != "offline"

    def paths(dev) -> dict:
        owner = bool(getattr(dev, "device_owner", False))
        relay = ws_hub.companion_connected(dev)
        if adb_ok(dev):
            # A device owner cannot be removed by `adb uninstall`: the app releases the role itself first.
            return {"uninstall": "one_tap" if owner and relay else "adb", "update": "adb", "reinstall": "adb"}
        if not relay:
            return {"uninstall": None, "update": None, "reinstall": None}
        if owner:
            return {"uninstall": "one_tap", "update": "device_owner", "reinstall": None}
        return {"uninstall": "one_tap", "update": "silent_a12" if (dev.sdk_int or 0) >= 31 else "one_tap",
                "reinstall": None}

    def edition(dev) -> str:
        return "lite" if getattr(dev, "app_edition", "full") == "lite" else "full"

    async def adb(dev, args, timeout: float):
        """Plain adb (not shell): exit codes are unreliable on old adb/Android, so callers read the text."""
        try:
            _, out, err = await storage._exec(["-s", dev.serial, *args], timeout)
        except StorageError as e:
            raise HTTPException(status_code=e.status, detail=e.message)
        errtext = err.decode("utf-8", "replace")
        try:
            storage._raise_transport(errtext)  # unauthorized / offline -> clear message
        except StorageError as e:
            raise HTTPException(status_code=e.status, detail=e.message)
        return (out.decode("utf-8", "replace") + errtext).replace("\r\n", "\n").strip()

    async def adb_install(dev, action: str) -> dict:
        info = app_update.latest(edition(dev))
        if not info:
            raise HTTPException(status_code=409, detail=f"No published {edition(dev)} APK on the laptop yet")
        if dev.id in busy:
            raise HTTPException(status_code=409, detail="An install is already running on this phone")
        busy.add(dev.id)
        try:
            print(f"{_LOG} {action} {dev.name} via ADB: v{info['version_name']} ({info['edition']})")
            text = await adb(dev, ["install", "-r", str(info["path"])], 300)
        finally:
            busy.discard(dev.id)
        ok = "Success" in text and "Failure" not in text
        print(f"{_LOG} {action} {dev.name}: {'done' if ok else 'failed'} — {text[-300:]}")
        return {"path": "adb", "status": "done" if ok else "failed", "version_name": info["version_name"],
                "edition": info["edition"], "output": text}

    async def relay_send(dev, msg: dict, path: str) -> dict:
        sock = ws_hub._get_companion_socket(dev)
        if not sock:
            raise HTTPException(status_code=503, detail="Phone is not on ADB and its companion app is not connected")
        try:
            await sock.send_json(msg)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Could not reach the phone: {e}")
        print(f"{_LOG} {msg['type']} sent to {dev.name} (path {path})")
        return {"path": path, "status": "sent"}

    @router.get("/app-lifecycle")
    async def lifecycle(device_id: str):
        dev = device(device_id)
        return {"paths": paths(dev), "device_owner": bool(getattr(dev, "device_owner", False)),
                "sdk": dev.sdk_int, "adb": adb_ok(dev), "edition": edition(dev)}

    @router.post("/app-lifecycle/uninstall")
    async def uninstall(device_id: str):
        dev = device(device_id)
        p = paths(dev)["uninstall"]
        if p == "adb":
            if dev.id in busy:
                raise HTTPException(status_code=409, detail="An install/uninstall is already running on this phone")
            busy.add(dev.id)
            try:
                print(f"{_LOG} uninstall {dev.name} via ADB")
                text = await adb(dev, ["uninstall", PACKAGE], 120)
            finally:
                busy.discard(dev.id)
            ok = "Success" in text and "Failure" not in text
            print(f"{_LOG} uninstall {dev.name}: {'done' if ok else 'failed'} — {text[-300:]}")
            hint = ("The app is device owner: connect its companion link so it can release the role, then retry."
                    if "DEVICE_POLICY_MANAGER" in text else None)
            return {"path": "adb", "status": "done" if ok else "failed", "output": text, "hint": hint}
        if p is None:
            raise HTTPException(status_code=503, detail="Phone is not on ADB and its companion app is not connected")
        return await relay_send(dev, {"type": "APP_UNINSTALL"}, p)

    @router.post("/app-lifecycle/update")
    async def update(device_id: str):
        dev = device(device_id)
        p = paths(dev)["update"]
        if p == "adb":
            return await adb_install(dev, "update")
        if p is None:
            raise HTTPException(status_code=503, detail="Phone is not on ADB and its companion app is not connected")
        if not app_update.latest(edition(dev)):
            raise HTTPException(status_code=409, detail=f"No published {edition(dev)} APK on the laptop yet")
        return await relay_send(dev, {"type": "APP_UPDATE_FORCE"}, p)

    @router.post("/app-lifecycle/reinstall")
    async def reinstall(device_id: str):
        dev = device(device_id)
        if not adb_ok(dev):
            raise HTTPException(status_code=409, detail="Reinstall needs the phone on USB / Wi-Fi ADB")
        return await adb_install(dev, "reinstall")

    @router.post("/app-lifecycle/make-device-owner")
    async def make_device_owner(device_id: str):
        dev = device(device_id)
        if not adb_ok(dev):
            raise HTTPException(status_code=409, detail="Making the app device owner needs the phone on USB / Wi-Fi ADB")
        try:
            rc, out = await storage.shell(dev.serial, f"dpm set-device-owner {shlex.quote(ADMIN_COMPONENT)} 2>&1", 30)
        except StorageError as e:
            raise HTTPException(status_code=e.status, detail=e.message)
        out = out.strip()
        ok = "Success" in out or "Device owner set" in out
        hint = None
        if not ok:
            low = out.lower()
            if "account" in low:
                hint = "Remove all accounts on the phone first, then retry."
            elif "already" in low and "owner" in low:
                hint = "This phone already has a device owner."
            elif "unknown admin" in low or "not installed" in low:
                hint = "Update the companion app on this phone first (this version has no device-admin receiver)."
        if ok:
            dev.device_owner = True
        print(f"{_LOG} make-device-owner {dev.name}: {'done' if ok else 'failed'} — {out[-300:]}")
        return {"status": "done" if ok else "failed", "output": out, "hint": hint}

    return router
