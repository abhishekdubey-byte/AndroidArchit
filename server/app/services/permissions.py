"""Which of the companion app's permissions each phone has granted — read-only, for the Permissions page.

Fresh from the app itself when it is connected (any network), otherwise over ADB when the phone is on USB
(also works while the app is stopped), otherwise the last report the app sent.
"""
import asyncio
import re
import time
from typing import Dict, List, Optional

from .storage_service import StorageError

PKG = "com.operator.companion"
FIX = "On the phone: Operator Companion → LAUNCH → UNLOCK MORE POWERS → "

# key, power (as named in the app), what it unlocks, how to fix it on the phone, full-edition only
CATALOG = [
    ("all_files_access", "VAULT · All files access", "File manager and Phone Backup",
     FIX + "VAULT → Grant (\"Allow access to manage all files\")", False),
    ("camera", "SENSES · Camera", "Remote photos and videos", FIX + "SENSES → Grant → Camera: Allow", False),
    ("audio", "SENSES · Microphone", "Call audio and video sound", FIX + "SENSES → Grant → Microphone: Allow", False),
    ("phone_state", "SENSES · Phone state", "Incoming-call alerts on the laptop", FIX + "SENSES → Grant → Phone: Allow", False),
    ("answer_calls", "SENSES · Answer calls", "Answer / end calls from the laptop", FIX + "SENSES → Grant → Phone: Allow", False),
    ("call_phone", "SENSES · Place calls", "Dialling from the laptop", FIX + "SENSES → Grant → Phone: Allow", False),
    ("accessibility_enabled", "PILOT · Accessibility", "Remote screen view, taps, typing, app launch (and remote camera)",
     FIX + "PILOT → Grant → turn on Operator Companion. Greyed out? App info → ⋮ (top right) → "
     "\"Allow restricted settings\", then try again", True),
    ("overlay", "STEALTH · Display over other apps", "Remote camera while Accessibility is off",
     FIX + "STEALTH → Grant → allow \"Display over other apps\"", False),
    ("battery_unrestricted", "ENDLESS · Battery unrestricted", "Keeps the link alive in the background",
     FIX + "ENDLESS → Grant. Realme/Oppo/Vivo/Xiaomi also: App info → Battery → allow background activity "
     "and auto launch", False),
    ("install_updates", "EVOLVE · Install updates", "One-tap app updates from the laptop",
     FIX + "EVOLVE → Grant → \"Allow from this source\"", False),
]


def rows(perms: Dict) -> List[Dict]:
    """One row per permission: state on / off / unknown (older app didn't report it) / n/a (Lite edition)."""
    out = []
    lite = perms.get("edition") == "lite"
    for key, title, unlocks, fix, full_only in CATALOG:
        note = ""
        if full_only and lite:
            state = "n/a"
            note = "Not in the Lite edition — install the full app over USB for this"
        elif key not in perms:
            state = "unknown"
            note = "This app version doesn't report it — update the app"
        else:
            state = "on" if perms[key] else "off"
            if key == "accessibility_enabled" and perms[key] and perms.get("accessibility_service") is False:
                state = "off"
                note = "Switched on but not running — turn it off and on again in Accessibility settings"
        out.append({"key": key, "title": title, "unlocks": unlocks, "fix": fix, "state": state, "note": note})
    return out


# ---------------------------------------------------------------- ADB

ADB_SCRIPT = (
    f"echo __SDK; getprop ro.build.version.sdk; echo __PKG; dumpsys package {PKG} | grep -E "
    "'versionName=|android.permission.(CAMERA|RECORD_AUDIO|READ_PHONE_STATE|ANSWER_PHONE_CALLS|CALL_PHONE|"
    "READ_EXTERNAL_STORAGE|READ_MEDIA_IMAGES): granted'; "
    f"echo __MES; appops get {PKG} MANAGE_EXTERNAL_STORAGE; echo __SAW; appops get {PKG} SYSTEM_ALERT_WINDOW; "
    f"echo __RIP; appops get {PKG} REQUEST_INSTALL_PACKAGES; echo __ACC; settings get secure enabled_accessibility_services; "
    f"echo __IDLE; dumpsys deviceidle whitelist | grep -c {PKG}; echo __PID; pidof {PKG}; echo __END"
)
RUNTIME = {"camera": "CAMERA", "audio": "RECORD_AUDIO", "phone_state": "READ_PHONE_STATE",
           "answer_calls": "ANSWER_PHONE_CALLS", "call_phone": "CALL_PHONE"}


def _sections(text: str) -> Dict[str, str]:
    out, cur = {}, None
    for line in text.replace("\r\n", "\n").split("\n"):
        m = re.fullmatch(r"__([A-Z]+)", line.strip())
        if m:
            cur = m.group(1)
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return out


def _appop_allowed(section: str, op: str) -> bool:
    """`appops get` prints a package mode and sometimes a uid mode; a non-default package mode wins."""
    pkg = re.search(rf"^{op}: (\w+)", section, re.M)
    uid = re.search(rf"Uid mode: {op}: (\w+)", section)
    if pkg and pkg.group(1) != "default":
        return pkg.group(1) == "allow"
    return bool(uid and uid.group(1) == "allow")


def parse_adb_report(text: str) -> Dict:
    s = _sections(text)
    pkg = s.get("PKG", "")
    sdk = int(s.get("SDK", "0").strip() or 0)
    granted = lambda perm: bool(re.search(rf"android\.permission\.{perm}: granted=true", pkg))
    p = {k: granted(v) for k, v in RUNTIME.items()}
    version = re.search(r"versionName=(\S+)", pkg)
    p.update(
        installed=bool(version),
        app_version=version.group(1) if version else None,
        edition=("lite" if version.group(1).endswith("-lite") else "full") if version else None,
        sdk=sdk,
        all_files_access=_appop_allowed(s.get("MES", ""), "MANAGE_EXTERNAL_STORAGE") if sdk >= 30
        else granted("READ_EXTERNAL_STORAGE"),
        overlay=_appop_allowed(s.get("SAW", ""), "SYSTEM_ALERT_WINDOW"),
        install_updates=_appop_allowed(s.get("RIP", ""), "REQUEST_INSTALL_PACKAGES"),
        accessibility_enabled=f"{PKG}/" in s.get("ACC", ""),
        battery_unrestricted=(s.get("IDLE", "0").strip() or "0") != "0",
        app_running=bool(s.get("PID", "").strip()),
    )
    return p


# ---------------------------------------------------------------- service

class PermissionService:
    def __init__(self, device_manager, hub, storage):
        self.dm, self.hub, self.storage = device_manager, hub, storage

    async def report(self, device_id: str) -> Optional[Dict]:
        dev = self.dm.get_device(device_id)
        if not dev:
            return None
        on_usb = dev.transport == "adb" and dev.status != "offline"
        rep = {"device_id": dev.id, "name": dev.name, "checked": time.time(), "app_running": None}
        perms: Dict = {}
        adb_error = None
        if self.hub.companion_connected(dev) and await self.hub.request_capabilities(dev):
            dev = self.dm.get_device(device_id) or dev  # the device poll may have rebuilt the record meanwhile
            perms, rep["source"], rep["app_running"] = dict(dev.permissions or {}), "app", True
        elif on_usb:
            try:
                perms = parse_adb_report((await self.storage.shell(dev.serial, ADB_SCRIPT, timeout=20))[1])
                rep["source"], rep["app_running"] = "adb", perms.get("app_running")
                if not perms.get("installed"):
                    rep["message"] = "Operator Companion is not installed on this phone"
            except StorageError as e:
                adb_error = e.message
        if "source" not in rep:
            perms, rep["source"] = dict(dev.permissions or {}), "last known"
            if adb_error:
                rep["message"] = f"Couldn't read it over USB: {adb_error}"
            elif not perms:
                rep["message"] = "Phone is offline and hasn't reported its permissions yet"
        rep["app_version"], rep["edition"] = perms.get("app_version"), perms.get("edition")
        rep["rows"] = rows(perms) if perms else []
        return rep

    async def all(self) -> List[Dict]:
        """Every phone once (a phone on USB that is also on the relay is listed as its USB entry)."""
        devs = list(self.dm.devices.values())
        ids = [d.id for d in devs if not (d.transport != "adb" and d.relay_id and any(
            o.relay_id == d.relay_id and o.id != d.id for o in devs))]
        # in parallel: each phone may take a few seconds to answer
        reps = await asyncio.gather(*(self.report(i) for i in ids), return_exceptions=True)
        return [r for r in reps if isinstance(r, dict)]
