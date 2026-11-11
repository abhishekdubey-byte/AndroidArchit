import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from .static_files import DashboardFiles

from .config import STATIC_DIR, SCREENSHOT_DIR, DOWNLOADS_DIR
from .services.adb_bridge import ADBBridge
from .services.device_manager import DeviceManager
from .services.ai_agent import AIAgentOperator
from .services.storage_service import StorageService, StorageError
from .services.adb_bridge import InputRejected, UiDumpUnavailable, UnsupportedText
from .services.relay import RelayRegistry, RelayStorage
from .services.remote_control import DeviceControl
from .security import AccessGuard, get_token
from .websocket.hub import WebSocketHub
from .api.routes import create_router
from .api.backup_routes import create_backup_router
from .api.permissions_routes import create_permissions_router
from .services.permissions import PermissionService
from .services.backup import BackupService, BackupError
from .api.storage_routes import create_storage_router
from .api.remote_routes import create_remote_router, ws_urls
from .api.pb_routes import create_pb_router
from .services.powerboosters import PowerBoosters, PBError
from .services.pb_gateway import PBGateway
from .services.cloud_bridge import CloudBridge
from .security import load_remote_config
from .config import SERVER_PORT
from . import logsetup

# Initialize singletons
adb_bridge = ADBBridge()
device_manager = DeviceManager(adb_bridge)
storage_service = StorageService()
relays = RelayRegistry()
relay_storage = RelayStorage(relays)
# Same API as ADBBridge, but remote phones are driven through their companion app
control = DeviceControl(device_manager, relays)
ai_agent = AIAgentOperator(device_manager, control)
from .services.routines import RoutineService
routines = RoutineService(ai_agent, device_manager)
ws_hub = WebSocketHub(device_manager, control, ai_agent)
ws_hub.relays = relays
device_manager.relays = relays
ws_hub.relay_storage = relay_storage
backups = BackupService(device_manager, storage_service, relay_storage, relays, ws_hub.broadcast_to_dashboards)
cloud_bridge = CloudBridge(SERVER_PORT)
# PowerBoosters: separate module (own DB, routes, WS message types); reuses phone identity only
pb_gateway = PBGateway(PowerBoosters(), ws_hub, device_manager, relays)
ws_hub.pb = pb_gateway


def _panel_state():
    """Live phone table for the terminal view (in-memory data only)."""
    rows = []
    for d in device_manager.get_all_devices():
        usb = d.transport == "adb" and d.status != "offline"
        remote = relays.get(d.relay_id) is not None
        link = " + ".join(x for x in (("Wi-Fi ADB" if ":" in d.serial else "USB") if usb else "",
                                       ("Remote (Lite)" if d.app_edition == "lite" else "Remote") if remote else "") if x) or "—"
        rows.append({"name": d.name, "android": d.android_version, "link": link,
                     "battery": f"{d.battery.level}%" if usb or remote else "—", "status": d.status})
    return {"devices": rows, "relay": cloud_bridge.status(), "dashboards": len(ws_hub.dashboard_clients)}


logsetup.set_panel_provider(_panel_state)
ai_agent.set_ws_hub(ws_hub)

async def _watch_app_updates():
    """publish_apk.sh runs outside the server: when a new build lands, tell every connected phone at once."""
    from .services import app_update
    def stamp():
        return tuple((app_update.latest(ed) or {}).get("version_code") for ed in ("full", "lite"))
    seen = stamp()
    while True:
        await asyncio.sleep(15)
        try:
            now = stamp()
            if now != seen:
                seen = now
                n = await ws_hub.notify_update_all()
                print(f"[Update] New app build {now} published — told {n} connected phone(s) to update")
        except Exception as e:
            print(f"[Update] watcher error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Startup] Android Control Centre & Multi-Device AI Operator")
    # Start background device discovery & telemetry sync
    await device_manager.start()
    devs = device_manager.get_all_devices()
    print(f"[Startup] {len(devs)} phone(s) attached over USB/ADB")
    for d in devs:
        print(f"[Startup]   • {d.name} · Android {d.android_version} · battery {d.battery.level}% · {d.status}")
    cfg = load_remote_config()
    await cloud_bridge.configure(cfg.get("cloud_url", ""), cfg.get("cloud_hub_key", ""))
    print("[Startup] Dashboard http://localhost:8000  ·  Files /files.html  ·  Pair phones /pair.html")
    print(f"[Remote] Phones connect via: {'  |  '.join(ws_urls()) or 'none'}")

    update_watch = asyncio.create_task(_watch_app_updates())
    routines.start()   # scheduled AI-operator routines (e.g. daily WhatsApp statuses)
    yield
    update_watch.cancel()
    await routines.stop()
    await ws_hub.shutdown()   # end fast mirrors / input sessions: no adb forward or phone process left behind
    
    await cloud_bridge.stop()
    print("[Shutdown] Stopping device manager...")
    await device_manager.stop()

app = FastAPI(
    title="Android Control Centre",
    description="Central Laptop Control Server & Multi-Device AI Operator",
    version="1.0.0",
    lifespan=lifespan
)

# Remote devices/browsers need the access token; cross-site browser requests are refused.
app.add_middleware(AccessGuard)

# Attach REST Router
app.include_router(create_router(device_manager, control, ai_agent, ws_hub))
from .api.routine_routes import create_routine_router
app.include_router(create_routine_router(routines))
app.include_router(create_storage_router(device_manager, storage_service, relays, relay_storage))
app.include_router(create_remote_router(device_manager, adb_bridge, relays, ws_hub, cloud_bridge))
app.include_router(create_backup_router(backups))
app.include_router(create_pb_router(pb_gateway))
from .api.app_lifecycle_routes import create_app_lifecycle_router
app.include_router(create_app_lifecycle_router(device_manager, storage_service, ws_hub))

@app.exception_handler(PBError)
async def pb_error_handler(request: Request, exc: PBError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})
app.include_router(create_permissions_router(PermissionService(device_manager, ws_hub, storage_service)))

@app.exception_handler(BackupError)
async def backup_error_handler(request: Request, exc: BackupError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})

@app.exception_handler(UnsupportedText)
async def unsupported_text_handler(request: Request, exc: UnsupportedText):
    return JSONResponse(status_code=422, content={"detail": str(exc)})

@app.exception_handler(UiDumpUnavailable)
async def ui_dump_handler(request: Request, exc: UiDumpUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})

@app.exception_handler(InputRejected)
async def input_rejected_handler(request: Request, exc: InputRejected):
    return JSONResponse(status_code=409, content={"detail": str(exc)})

@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: StorageError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})

# Attach WebSocket endpoint for Dashboards
@app.websocket("/ws/dashboard")
async def websocket_dashboard_endpoint(websocket: WebSocket):
    await ws_hub.register_dashboard(websocket)

# Companion apps (USB, same Wi-Fi, or any remote network via tunnel/public URL)
@app.websocket("/ws/device")
async def websocket_device_endpoint(websocket: WebSocket):
    await ws_hub.register_dashboard(websocket, role="device")

# Mount static storage downloads/screenshots for web access
app.mount("/storage/downloads", StaticFiles(directory=DOWNLOADS_DIR), name="downloads")
app.mount("/storage/screenshots", StaticFiles(directory=SCREENSHOT_DIR), name="screenshots")

# Mount Dashboard web client
app.mount("/", DashboardFiles(directory=STATIC_DIR, html=True), name="static")
