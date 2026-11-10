import ipaddress
import shlex
import socket
from typing import List, Optional
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import security
from ..config import SERVER_PORT
from ..services.adb_bridge import ADBBridge
from ..services.device_manager import DeviceManager
from ..services.relay import RelayRegistry

COMPANION_PKG = "com.operator.companion"


class RemoteConfig(BaseModel):
    public_url: Optional[str] = None


class CloudConfig(BaseModel):
    url: str = ""
    hub_key: Optional[str] = None  # omitted = keep the stored key


def lan_ips() -> List[str]:
    """This laptop's private IPv4 addresses, best first: Wi-Fi, wired Ethernet, then the rest
    (USB tethering / VPN). Container bridges are skipped. Phones try them in this order."""
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "up"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        out = ""
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface, ip = parts[1], parts[3].split("/")[0]
        if iface.startswith(("docker", "br-", "veth", "virbr", "lxc", "cni", "flannel", "tun", "tap", "wg",
                             "tailscale", "zt")):
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_private or addr.is_loopback or addr.is_link_local:
            continue  # never send the phone token in cleartext to public/CGNAT/VPN addresses
        rank = 0 if iface.startswith(("wl", "wifi")) else 1 if iface.startswith(("eth", "enp", "eno", "ens")) else 2
        found.append((rank, iface, ip))
    if not found:  # fallback: interface used for the default route
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                found.append((0, "", s.getsockname()[0]))
        except OSError:
            pass
    return [ip for _, _, ip in sorted(found)][:3]


def _ws(u: str) -> str:
    p = urlparse(u if "://" in u else f"https://{u}")
    return f"{ {'https': 'wss', 'wss': 'wss', 'http': 'ws', 'ws': 'ws'}.get(p.scheme, 'wss') }://{p.netloc}"


def ws_urls() -> List[str]:
    """Candidate URLs the phone tries in order: same-Wi-Fi LAN, Cloudflare relay, then a public/tunnel URL."""
    urls = []
    cfg = security.load_remote_config()
    for ip in lan_ips():
        urls.append(f"ws://{ip}:{SERVER_PORT}/ws/device")
    if cfg.get("cloud_url") and cfg.get("cloud_hub_key"):
        urls.append(f"{_ws(cfg['cloud_url'])}/ws/device")
    public = (cfg.get("public_url") or "").strip().rstrip("/")
    if public:
        u = urlparse(public if "://" in public else f"https://{public}")
        scheme = {"https": "wss", "wss": "wss", "http": "ws", "ws": "ws"}.get(u.scheme, "wss")
        urls.append(f"{scheme}://{u.netloc}/ws/device")
    return urls


def pairing_link() -> str:
    return "acc://pair?" + urlencode([("u", u) for u in ws_urls()] + [("t", security.get_device_token())])


def create_remote_router(device_manager: DeviceManager, adb: ADBBridge, relays: RelayRegistry, hub=None,
                         cloud=None) -> APIRouter:
    router = APIRouter(prefix="/api/remote", tags=["remote"])

    def info():
        devices = [{"id": d.id, "name": d.name, "android_version": d.android_version, "transport": d.transport,
                    "status": d.status, "relay_online": relays.get(d.relay_id) is not None, "relay_id": d.relay_id,
                    "usb": d.transport == "adb" and d.status != "offline"}
                   for d in device_manager.get_all_devices()]
        return {"token": security.get_token(), "device_token": security.get_device_token(), "public_url": security.load_remote_config().get("public_url", ""),
                "ws_urls": ws_urls(), "pairing_link": pairing_link(), "devices": devices,
                "refused": relays.refused_phones(),
                "cloud": {**(cloud.status() if cloud else {}),
                          "configured": bool(security.load_remote_config().get("cloud_hub_key"))}}

    @router.get("/pairing")
    async def pairing():
        return info()

    @router.post("/config")
    async def set_config(cfg: RemoteConfig):
        data = security.load_remote_config()
        data["public_url"] = (cfg.public_url or "").strip()
        security.save_remote_config(data)
        return info()

    @router.post("/cloud")
    async def set_cloud(cfg: CloudConfig):
        data = security.load_remote_config()
        data["cloud_url"] = cfg.url.strip()
        if cfg.hub_key is not None:
            data["cloud_hub_key"] = cfg.hub_key.strip()
        security.save_remote_config(data)
        if cloud:
            await cloud.configure(data["cloud_url"], data.get("cloud_hub_key", ""))
        return info()

    @router.post("/token/rotate")
    async def rotate():
        try:
            security.rotate_tokens()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        if hub:
            await hub.close_remote_clients()  # revoke live sessions, not just future handshakes
        if cloud:
            await cloud.push_auth()  # relay must refuse the old tokens too
        return info()

    @router.post("/forget/{relay_id}")
    async def forget(relay_id: str):
        """Unlock a phone's identity (e.g. after the app was reinstalled) so it can register again."""
        relays.forget(relay_id)
        return info()

    @router.post("/provision/{device_id}")
    async def provision(device_id: str):
        """Pair a USB-connected phone in one click: hands URLs + token to the companion app via adb."""
        dev = device_manager.get_device(device_id)
        if not dev or dev.transport != "adb" or dev.status == "offline":
            raise HTTPException(409, "Phone must be connected over USB/ADB to provision it")
        pkgs = await adb.run_device_shell(dev.serial, f"pm list packages {COMPANION_PKG}")
        if f"package:{COMPANION_PKG}" not in pkgs:
            raise HTTPException(409, "Companion app is not installed on this phone")
        # PairReceiver is protected by android.permission.DUMP: only adb/system can send it, so no tap is needed.
        out = await adb.run_device_shell(
            dev.serial, f"am broadcast -n {COMPANION_PKG}/.relay.PairReceiver --es uri {shlex.quote(pairing_link())}")
        if "result=0" not in out and "Broadcast completed" not in out:
            raise HTTPException(500, f"Phone did not accept the pairing: {out.strip()[:300]}")
        if dev.relay_id:
            relays.forget(dev.relay_id)  # USB = physical possession: allow this phone to re-register
        await adb.run_device_shell(dev.serial, f"am start -n {COMPANION_PKG}/.MainActivity")
        return {"status": "sent", "urls": ws_urls()}

    return router
