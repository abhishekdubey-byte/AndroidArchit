import io
from pathlib import Path
from typing import List, Optional, Any
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, UploadFile, File, Response, Query
from ..models.schemas import (
    Device, TouchAction, KeyAction, TextAction, LaunchAppAction,
    AppInfo, Task, ApprovalDecision, UINode
)
from ..services.device_manager import DeviceManager
from ..services.adb_bridge import ADBBridge, UiDumpUnavailable, UnsupportedText
from ..services.ai_agent import AIAgentOperator
from ..config import DOWNLOADS_DIR, UPLOADS_DIR, download_url

def create_router(device_manager: DeviceManager, adb_bridge: ADBBridge, ai_agent: AIAgentOperator, ws_hub: Optional[Any] = None) -> APIRouter:
    router = APIRouter(prefix="/api")

    def resolve_device(device_id: str) -> Device:
        dev = device_manager.get_device(device_id)
        if not dev:
            raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
        return dev

    # 1. Device Fleet Management
    @router.get("/devices", response_model=List[Device])
    async def get_devices():
        return device_manager.get_all_devices()

    @router.get("/devices/{device_id}", response_model=Device)
    async def get_device(device_id: str):
        return resolve_device(device_id)

    @router.post("/devices/refresh", response_model=List[Device])
    async def refresh_devices():
        return await device_manager.refresh_devices()

    @router.post("/devices/{device_id}/reconnect")
    async def reconnect_device(device_id: str):
        """Try to bring an offline phone back: re-dial Wi-Fi ADB, and if it is on USB silently wake its app
        (same wake broadcast the poll loop uses) so its link to this laptop comes back."""
        dev = resolve_device(device_id)
        if dev.status != "offline":
            return {"ok": True, "online": True, "message": f"{dev.name} is already online"}
        steps = []
        if ":" in dev.serial:   # Wi-Fi ADB
            _, out, err = await adb_bridge.run_command("connect", dev.serial)
            steps.append((out or err).strip())
        attached = {d["serial"] for d in await adb_bridge.list_attached_devices()}
        if dev.serial in attached:
            await adb_bridge.run_device_shell(dev.serial, "am broadcast -n com.operator.companion/.relay.PairReceiver --ez wake true")
            steps.append("woke the app over USB")
        await device_manager.refresh_devices()
        import asyncio as _a
        for _ in range(8):      # a woken app needs a few seconds to dial back in
            d = device_manager.get_device(device_id)
            if d and d.status != "offline":
                return {"ok": True, "online": True, "message": f"{d.name} is back online"}
            await _a.sleep(1)
        hint = "; ".join(s for s in steps if s) or "not on USB or Wi-Fi ADB"
        return {"ok": False, "online": False,
                "message": f"{dev.name} is still offline ({hint}). Open the app on the phone or plug it in by USB; it reconnects by itself."}

    @router.delete("/devices/{device_id}")
    async def remove_device(device_id: str):
        """Remove an OFFLINE phone from the list. It comes back automatically the next time it connects."""
        dev = resolve_device(device_id)
        if dev.status != "offline":
            raise HTTPException(status_code=409, detail="Only offline phones can be removed")
        device_manager.devices.pop(dev.id, None)
        for k in [k for k, v in device_manager.serial_to_id.items() if v == dev.id]:
            device_manager.serial_to_id.pop(k, None)
        if ws_hub:
            await ws_hub.broadcast_to_dashboards({"type": "DEVICE_REMOVED", "data": {"id": dev.id}})
        return {"ok": True}

    # 2. Live Screen Mirroring Snapshot
    @router.get("/devices/{device_id}/screen")
    async def get_screen_snapshot(device_id: str, width: int = Query(540, ge=240, le=1080)):
        dev = resolve_device(device_id)
        try:
            jpeg_bytes, _ = await adb_bridge.capture_screen(dev.serial, scale_width=width, quality=80)
            return Response(content=jpeg_bytes, media_type="image/jpeg")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to capture screen: {str(e)}")

    # 3. Input & Interaction
    @router.post("/devices/{device_id}/action/touch")
    async def inject_touch(device_id: str, action: TouchAction):
        dev = resolve_device(device_id)
        if action.action == "swipe" and action.x2 is not None and action.y2 is not None:
            dur = action.duration_ms or 300
            await adb_bridge.inject_swipe(dev.serial, action.x, action.y, action.x2, action.y2, dur)
        else:
            await adb_bridge.inject_tap(dev.serial, action.x, action.y)
        return {"status": "success", "action": action.action}

    @router.post("/devices/{device_id}/action/key")
    async def inject_key(device_id: str, action: KeyAction):
        dev = resolve_device(device_id)
        await adb_bridge.inject_key(dev.serial, action.key)
        return {"status": "success", "key": action.key}

    @router.post("/devices/{device_id}/action/text")
    async def inject_text(device_id: str, action: TextAction):
        dev = resolve_device(device_id)
        try:
            await adb_bridge.inject_text(dev.serial, action.text)
        except UnsupportedText as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"status": "success"}

    # 4. App Management
    @router.get("/devices/{device_id}/apps", response_model=List[AppInfo])
    async def get_installed_apps(device_id: str):
        dev = resolve_device(device_id)
        return await adb_bridge.get_installed_apps(dev.serial)

    @router.post("/devices/{device_id}/apps/launch")
    async def launch_app(device_id: str, action: LaunchAppAction):
        dev = resolve_device(device_id)
        success = await adb_bridge.launch_package(dev.serial, action.package_name)
        if not success:
            raise HTTPException(status_code=400, detail=f"Failed to launch app '{action.package_name}'")
        return {"status": "success", "launched": action.package_name}

    # 5. UI Hierarchy Inspection
    @router.get("/devices/{device_id}/ui", response_model=List[UINode])
    async def get_ui_hierarchy(device_id: str):
        dev = resolve_device(device_id)
        try:
            return await adb_bridge.dump_ui_hierarchy(dev.serial)
        except UiDumpUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))

    # 6. File Manager
    @router.get("/devices/{device_id}/files")
    async def list_device_files(device_id: str, path: str = Query("/sdcard/Download")):
        dev = resolve_device(device_id)
        return await adb_bridge.list_files(dev.serial, path)

    @router.post("/devices/{device_id}/files/upload")
    async def upload_file(device_id: str, file: UploadFile = File(...), target_path: str = "/sdcard/Download"):
        dev = resolve_device(device_id)
        local_target = UPLOADS_DIR / file.filename
        with open(local_target, "wb") as f:
            f.write(await file.read())
        remote_dest = f"{target_path.rstrip('/')}/{file.filename}"
        success = await adb_bridge.push_file(dev.serial, local_target, remote_dest)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to push file to Android device")
        return {"status": "success", "destination": remote_dest}

    @router.get("/waitlist")
    async def waitlist_count():
        from ..services import waitlist
        return {"count": waitlist.count()}

    @router.post("/app/notify-update")
    async def notify_update():
        from ..services import app_update
        notified = await ws_hub.notify_update_all() if ws_hub else 0
        latest = {ed: (app_update.latest(ed) or {}).get("version_name") for ed in ("full", "lite")}
        return {"notified": notified, "latest": latest}

    @router.get("/app/latest-info")
    async def latest_info():
        from ..services import app_update
        return {ed: {k: v for k, v in (app_update.latest(ed) or {}).items() if k != "path"} for ed in ("full", "lite")}

    @router.get("/captures")
    async def list_captures(limit: int = 2000, device_id: Optional[str] = None):
        """Photos/videos captured from phones. Each phone has its own folder under storage/downloads;
        files from before per-phone folders sit at the top level ("Earlier captures")."""
        from ..config import device_capture_dir
        root = DOWNLOADS_DIR.resolve()
        base = device_capture_dir(device_id) if device_id else root
        items = []
        for f in base.rglob("*"):
            if (f.is_file() and not f.name.startswith(".") and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4", ".webm")
                    and f.stat().st_size > 0 and ".thumbs" not in f.parts):
                st = f.stat()
                rel = f.resolve().relative_to(root)
                items.append({"filename": f.name, "url": download_url(f), "size": st.st_size, "mtime": int(st.st_mtime),
                              "device": rel.parts[0].replace("_", " ") if len(rel.parts) > 1 else "Earlier captures",
                              "kind": "video" if f.suffix.lower() in (".mp4", ".webm") else "photo"})
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return {"folder": str(base), "total": len(items), "items": items[:max(1, min(limit, 5000))]}

    @router.get("/captures/thumb")
    async def capture_thumb(path: str):
        """Small JPEG of a captured video's first second (made once with ffmpeg, then cached)."""
        import asyncio, hashlib, urllib.parse
        from ..services.cam_record import FFMPEG
        rel = urllib.parse.unquote(path.split("/storage/downloads/", 1)[-1])
        src = (DOWNLOADS_DIR / rel).resolve()
        if DOWNLOADS_DIR.resolve() not in src.parents or not src.is_file():
            raise HTTPException(status_code=404, detail="not found")
        cache = DOWNLOADS_DIR / ".thumbs"
        cache.mkdir(exist_ok=True)
        out = cache / (hashlib.sha1(f"v2:{src}:{src.stat().st_mtime}".encode()).hexdigest() + ".jpg")
        if not out.exists() and FFMPEG:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-y", "-ss", "1", "-i", str(src), "-frames:v", "1", "-vf", "thumbnail=30,scale=240:-2", "-q:v", "5", str(out),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), 20)
            except asyncio.TimeoutError:
                proc.kill()
        if not out.exists():
            raise HTTPException(status_code=404, detail="no thumbnail")
        return Response(out.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    # 7. AI Task Operator
    @router.get("/tasks", response_model=List[Task])
    async def list_tasks():
        return ai_agent.get_all_tasks()

    class TaskCreateRequest(BaseModel):
        instruction: Optional[str] = None
        target_device_id: Optional[str] = None

    @router.post("/tasks", response_model=Task)
    async def submit_task(
        req: Optional[TaskCreateRequest] = None,
        instruction: Optional[str] = None,
        target_device_id: Optional[str] = None
    ):
        inst = (req.instruction if req and req.instruction else instruction) or ""
        targ = (req.target_device_id if req and req.target_device_id else target_device_id) or ""
        if not inst:
            raise HTTPException(status_code=400, detail="Missing 'instruction'")
        dev = resolve_device(targ)
        task = await ai_agent.submit_task(inst, dev.id)
        return task

    @router.post("/tasks/{task_id}/approve")
    async def approve_task_step(task_id: str, decision: ApprovalDecision):
        task = ai_agent.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        ai_agent.handle_approval_response(task_id, decision)
        return {"status": "success", "decision": decision.decision}

    # 8. Call Management & Telephony
    @router.get("/devices/{device_id}/call/state")
    async def get_call_state(device_id: str):
        dev = resolve_device(device_id)
        state = await adb_bridge.get_telephony_state(dev.serial)
        return {"device_id": dev.id, "telephony": state}

    @router.post("/devices/{device_id}/call/answer")
    async def answer_call(device_id: str):
        dev = resolve_device(device_id)
        success = await adb_bridge.answer_incoming_call(dev.serial)
        return {"status": "success" if success else "failed", "action": "answer"}

    @router.post("/devices/{device_id}/call/end")
    async def end_call(device_id: str):
        dev = resolve_device(device_id)
        success = await adb_bridge.end_call(dev.serial)
        return {"status": "success" if success else "failed", "action": "end"}

    @router.post("/devices/{device_id}/call/dial")
    async def dial_call(device_id: str, number: str = Query(..., min_length=2, max_length=40)):
        dev = resolve_device(device_id)
        ok = await adb_bridge.start_phone_call(dev.serial, number)
        return {"status": "success" if ok else "failed", "action": "dial", "number": number}

    @router.post("/devices/{device_id}/call/simulate_incoming")
    async def simulate_incoming_call(device_id: str, phone_number: str = "+00 00000 00000", caller_name: str = "Test Caller"):
        dev = resolve_device(device_id)
        await device_manager._notify("INCOMING_CALL", {
            "device_id": dev.id,
            "device_name": dev.name,
            "phone_number": phone_number,
            "caller_name": caller_name,
            "state": "ringing"
        })
    # 9. Silent Headless Camera & Video Capture (Zero UI, Sensor Direct)
    async def require_companion_on_usb(dev: Device):
        """Camera capture runs inside the companion app: say so at once instead of timing out (43-63 s)."""
        if dev.transport == "adb" and dev.status != "offline" and not await adb_bridge.companion_installed(dev.serial):
            raise HTTPException(status_code=409, detail="Install the Operator Companion app on this phone to use the camera")

    @router.post("/devices/{device_id}/camera/photo")
    async def capture_silent_photo(device_id: str, lens: str = Query("back", pattern="^(back|front)$")):
        dev = resolve_device(device_id)
        await require_companion_on_usb(dev)
        if ws_hub:
            res = await ws_hub.capture_headless_photo(dev.id, lens=lens)
            if res.get("success"):
                return res
        dest = await adb_bridge.capture_headless_photo_adb(dev.serial, lens=lens)
        if dest and dest.exists():
            return {
                "success": True,
                "filename": dest.name,
                "local_path": str(dest),
                "url": download_url(dest),
                "device_id": dev.id,
                "lens": lens
            }
        raise HTTPException(status_code=500, detail="Headless camera photo capture failed or timed out")

    @router.post("/devices/{device_id}/camera/video")
    async def record_silent_video(device_id: str, lens: str = Query("back", pattern="^(back|front)$"), duration_sec: int = Query(5, ge=1, le=120)):
        dev = resolve_device(device_id)
        await require_companion_on_usb(dev)
        if ws_hub:
            res = await ws_hub.record_headless_video(dev.id, lens=lens, duration_sec=duration_sec)
            if res.get("success"):
                return res
        dest = await adb_bridge.record_headless_video_adb(dev.serial, lens=lens, duration_sec=duration_sec)
        if dest and dest.exists():
            return {
                "success": True,
                "filename": dest.name,
                "local_path": str(dest),
                "url": download_url(dest),
                "device_id": dev.id,
                "duration_sec": duration_sec,
                "lens": lens
            }
        raise HTTPException(status_code=500, detail="Headless video recording failed or timed out")

    # 10. Device Capability Matrix & Diagnostics
    @router.get("/devices/{device_id}/capabilities")
    async def get_device_capabilities(device_id: str):
        dev = resolve_device(device_id)
        return {
            "device_id": dev.id,
            "name": dev.name,
            "brand": dev.brand,
            "manufacturer": dev.manufacturer,
            "model": dev.model,
            "android_version": dev.android_version,
            "sdk_int": dev.sdk_int,
            "hardware_capabilities": dev.hardware_capabilities,
            "permissions": dev.permissions,
            "power_and_lockscreen": dev.power_and_lockscreen,
            "supported_features": dev.capabilities
        }

    @router.get("/devices/{device_id}/diagnostics")
    async def get_device_diagnostics(device_id: str):
        dev = resolve_device(device_id)
        return {
            "device_id": dev.id,
            "diagnostics": dev.diagnostic_history
        }

    @router.get("/diagnostics")
    async def get_all_diagnostics():
        return {
            "total_reports": len(device_manager.diagnostics_log),
            "reports": device_manager.diagnostics_log
        }

    @router.post("/devices/{device_id}/wake")
    async def wake_device(device_id: str):
        dev = resolve_device(device_id)
        await adb_bridge.wake(dev.serial)
        return {"status": "success", "message": f"Woke display rails on {dev.name}"}

    return router
