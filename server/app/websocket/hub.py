import uuid
import asyncio
import base64
import json
import shlex
import time
from typing import Dict, Set, Any, Optional
from fastapi import WebSocket, WebSocketDisconnect
from ..config import (DOWNLOADS_DIR, FAST_MIRROR, MIRROR_BITRATE, MIRROR_FPS, MIRROR_MAX_SIZE, device_capture_dir,
                      download_url)
from ..services.scrcpy_session import MirrorUnavailable, ScrcpySession, VideoPacket
from ..services.h264_recorder import AutoRecorder, H264Recorder

MAX_CAMERA_PACKET = 2 * 1024 * 1024
from .media_fanout import MediaFanout
from ..services.input_router import InputRouter
from ..services.device_manager import DeviceManager
from ..services.cam_record import display_name
from ..services.adb_bridge import ADBBridge, InputRejected, UnsupportedText
from ..services.storage_service import StorageError
from ..services.remote_control import COMPANION_MISSING
from ..services.ai_agent import AIAgentOperator
from ..models.schemas import ApprovalDecision

# Messages a phone (companion app on /ws/device) is allowed to send
PHONE_MESSAGES = {"DEVICE_HELLO", "RELAY_BOUND_ACK", "DEVICE_CAPABILITIES", "DEVICE_ERROR", "INCOMING_CALL",
                  "APP_UPDATE_CHECK", "APP_UPDATE_PULL", "APP_LIFECYCLE_STATUS", "WAITLIST_GET", "WAITLIST_JOIN",
                  "GAME_JOIN", "GAME_STRIKE", "GAME_LEAVE",
                  "CALL_ACTIVE", "CALL_ENDED", "PHONE_AUDIO_CHUNK", "PHOTO_CAPTURED", "VIDEO_RECORDED",
                  "CAMERA_FRAME", "CAMERA_STREAM_STATE", "CAMERA_AUDIO", "HUNT_STATE", "PHONE_AUDIO_STATE", "SCREEN_STATE"}
from ..services.pb_gateway import PHONE_TYPES as PB_PHONE_TYPES
PHONE_MESSAGES = PHONE_MESSAGES | PB_PHONE_TYPES


def parse_device_binary(data: bytes):
    """A phone's binary frame: [u16 header length][JSON header][payload] → (header dict, payload); (None, b"") if malformed."""
    if len(data) < 2:
        return None, b""
    n = int.from_bytes(data[:2], "big")
    if len(data) < 2 + n:
        return None, b""
    try:
        header = json.loads(data[2:2 + n].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, b""
    return (header, bytes(data[2 + n:])) if isinstance(header, dict) else (None, b"")


class WebSocketHub:
    def __init__(self, device_manager: DeviceManager, adb_bridge: ADBBridge, ai_agent: AIAgentOperator):
        self.device_manager = device_manager
        self.adb = adb_bridge
        self.ai_agent = ai_agent
        self.dashboard_clients: Set[WebSocket] = set()
        self.device_clients: Dict[str, WebSocket] = {}
        self.streaming_tasks: Dict[str, asyncio.Task] = {}
        self.active_streaming_devices: Set[str] = set()
        self.pending_photo_futures: Dict[str, asyncio.Future] = {}
        self.pending_video_futures: Dict[str, asyncio.Future] = {}
        self.relays = None  # RelayRegistry, attached in main.py
        self._caps_waiters: Dict[str, asyncio.Event] = {}  # device id -> fresh DEVICE_CAPABILITIES arrived
        from ..services.game import GameState
        self.game = GameState()
        self.cam_recorders: Dict[str, Any] = {}  # device_id -> CamRecorder (live preview saved to laptop)
        self.fanout = MediaFanout()                # H.264 packets → dashboards (per-viewer backpressure)
        self.mirror_sessions: Dict[str, ScrcpySession] = {}  # device_id -> running fast USB mirror
        self._fast_mirror_failed: Dict[str, float] = {}      # device_id -> when fast mirroring last failed
        self._fast_mirror_retry: Dict[str, float] = {}       # device_id -> seconds to wait before trying again
        self._cam_codec: Dict[str, str] = {}                  # device_id -> "h264" / "jpeg" (live camera format)
        self._cam_config: Dict[str, bytes] = {}               # device_id -> latest H.264 SPS/PPS from the phone
        self._cam_rotation: Dict[str, int] = {}               # device_id -> camera sensor rotation (degrees cw)
        self._cam_state: Dict[str, dict] = {}                 # device_id -> last state of a live camera (for newcomers)
        # Phone microphone as AAC (stream '<device_id>#mic'): heard with the live camera and by "Listen Live"
        self._mic_config: Dict[str, bytes] = {}               # device_id -> AudioSpecificConfig
        self._listeners: Dict[str, Set[Any]] = {}             # device_id -> dashboards with Listen Live on
        self._legacy_pcm: Set[str] = set()                    # phones whose camera audio is old 16 kHz PCM
        self._phone_media: Dict[str, Set[str]] = {}           # companion id -> live media it can send ("aac", ...)
        # Remote screen as H.264 from the companion (MediaProjection), for phones not on USB
        self._remote_screen: Dict[str, dict] = {}             # device_id -> {"state", "error", "frames", "changed"}
        self._remote_screen_failed: Dict[str, float] = {}     # device_id -> when the phone last refused/failed
        self.input_router = InputRouter(adb_bridge, device_manager, self.mirror_sessions)
        self.pb = None  # PowerBoosters gateway, attached in main.py

        # Subscribe to device manager and AI agent events
        self.device_manager.add_listener(self._handle_device_event)
        self.ai_agent.add_listener(self._handle_ai_event)

    def companion_connected(self, dev) -> bool:
        return self._get_companion_socket(dev) is not None

    async def request_capabilities(self, dev, timeout: float = 6.0) -> bool:
        """Ask the phone's app for a fresh capability/permission report and wait for it (Permissions page)."""
        sock = self._get_companion_socket(dev)
        if not sock:
            return False
        ev = self._caps_waiters.setdefault(dev.id, asyncio.Event())
        ev.clear()
        try:
            await sock.send_json({"type": "GET_CAPABILITIES"})
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except Exception:
            return False

    def _get_companion_socket(self, dev: Optional[Any] = None) -> Optional[WebSocket]:
        if not self.device_clients:
            return None
        if dev:
            for key in (getattr(dev, 'relay_id', None), getattr(dev, 'id', '')):
                if key and key in self.device_clients:
                    return self.device_clients[key]
            return None  # exact match only: never send one phone's command to another of the same model
        # No target given: only unambiguous when exactly one companion is connected
        return next(iter(self.device_clients.values())) if len(self.device_clients) == 1 else None

    async def _handle_device_event(self, event_type: str, data: Any):
        payload = data.dict() if hasattr(data, 'dict') else data
        if event_type == "DEVICE_CONNECTED" and isinstance(payload, dict) and payload.get("id"):
            # a replug starts clean: no 60 s of slow snapshots because the last attempt hit the unplug
            self._fast_mirror_failed.pop(payload["id"], None)
            self._fast_mirror_retry.pop(payload["id"], None)
            self.input_router.forget_device(payload["id"])
        await self.broadcast_to_dashboards({
            "type": event_type,
            "data": payload,
            "payload": payload
        })

    async def _handle_ai_event(self, event_type: str, data: Any):
        payload = data.dict() if hasattr(data, 'dict') else data
        await self.broadcast_to_dashboards({
            "type": event_type,
            "data": payload
        })

    async def register_dashboard(self, websocket: WebSocket, role: str = "dashboard"):
        await websocket.accept()
        if role == "dashboard":
            self.dashboard_clients.add(websocket)
            print(f"[WebSocketHub] Dashboard client connected. Total: {len(self.dashboard_clients)}")
            await websocket.send_json({
                "type": "INITIAL_STATE",
                "data": {
                    "devices": [d.dict() for d in self.device_manager.get_all_devices()],
                    "tasks": [t.dict() for t in self.ai_agent.get_all_tasks()],
                    "diagnostics": self.device_manager.diagnostics_log
                }
            })
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if role == "device" and await self.handle_device_binary(websocket, message["bytes"]):
                        continue
                    if role == "device" and self.pb and self.relays:
                        me = self.relays.for_socket(websocket)
                        if me and await self.pb.handle_chunk(websocket, me.relay_id, message["bytes"]):
                            continue
                    if self.relays:
                        self.relays.on_binary(websocket, message["bytes"])
                    continue
                try:
                    data = json.loads(message.get("text") or "")
                except ValueError:
                    continue
                if self.relays and self.relays.on_text(websocket, data):
                    continue
                if role == "device":
                    if data.get("type") not in PHONE_MESSAGES:
                        continue  # a phone may never drive the dashboard API (other phones, tasks, input)
                    me = self.relays.for_socket(websocket) if self.relays else None
                    if me is not None:
                        data["device_id"] = me.relay_id  # a phone can only speak for itself
                    elif data.get("type") not in ("DEVICE_HELLO", "APP_UPDATE_CHECK", "APP_UPDATE_PULL", "WAITLIST_GET", "WAITLIST_JOIN", "GAME_JOIN", "GAME_STRIKE", "GAME_LEAVE"):
                        continue
                if data.get("type") in ("PHOTO_CAPTURED", "VIDEO_RECORDED"):
                    # These fetch the file over this same socket: handle them in the background so the
                    # loop keeps reading the phone's replies (awaiting here would deadlock the relay).
                    asyncio.create_task(self._process_safely(websocket, data))
                    continue
                await self._process_safely(websocket, data)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            print(f"[WebSocketHub] Client error: {e}")
        finally:
            await self.dashboard_left(websocket)
            for k, v in list(self.device_clients.items()):
                if v is websocket:
                    del self.device_clients[k]
            rid = self.relays.detach(websocket) if self.relays else None
            if rid:
                await self.device_manager.unregister_relay(rid)
                if self.pb:
                    await self.pb.on_disconnect(rid)
                _rd = self._device_for_companion(rid)
                _rkey = _rd.id if _rd else rid
                if _rkey in self.cam_recorders:
                    self._finalize_recording_soon(_rkey)  # phone dropped mid-recording → save what we have
                print(f"[WebSocketHub] Companion {rid} disconnected")
            else:
                print(f"[WebSocketHub] Client disconnected. Dashboards remaining: {len(self.dashboard_clients)}")

    async def _process_safely(self, websocket: WebSocket, data: Dict[str, Any]):
        try:
            await self._process_dashboard_message(websocket, data)
        except Exception as e:  # one bad message must not drop a phone's relay
            print(f"[WebSocketHub] Error handling {data.get('type')}: {e}")

    async def _process_dashboard_message(self, websocket: WebSocket, msg: Dict[str, Any]):
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        device_id = msg.get("device_id")

        # 1. Screen Streaming controls
        if msg_type == "START_SCREEN_STREAM":
            target = self.device_manager.get_device(device_id) if device_id else None
            if target and target.transport != "adb":
                await websocket.send_json({"type": "SCREEN_UNAVAILABLE", "device_id": device_id,
                                           "payload": {"reason": "Connecting to the phone's screen remotely…"}})
            if device_id:
                if target is not None:
                    device_id = target.id
                self.fanout.subscribe(websocket, device_id)
                if device_id not in self._remote_screen:
                    self._remote_screen_failed.pop(device_id, None)   # the user asked again: the phone may ask again
                self.active_streaming_devices.add(device_id)
                if device_id not in self.streaming_tasks or self.streaming_tasks[device_id].done():
                    self.streaming_tasks[device_id] = asyncio.create_task(self._screen_stream_loop(device_id))

        elif msg_type == "HUNT_COMMAND":
            # Crystal Mines HUNT: an instruction from the dashboard (text and/or arrow) shown inside the game
            dev = self.device_manager.get_device(device_id)
            comp_sock = self._get_companion_socket(dev)
            if comp_sock:
                await comp_sock.send_json({"type": "HUNT_COMMAND", "payload": {
                    "text": str(payload.get("text") or "")[:140], "arrow": str(payload.get("arrow") or ""),
                    "steer": str(payload.get("steer") or "")}})   # steer = move the creature (left/right/…/summon/freeze)

        elif msg_type == "HUNT_STATE":
            # From the phone: live hunt state (DNA count, target, creatures) → dashboards watching that phone
            _d = self._device_for_companion(device_id)
            await self.broadcast_to_dashboards({"type": "HUNT_STATE", "device_id": _d.id if _d else device_id, "payload": payload})

        elif msg_type in ("START_CAMERA_STREAM", "SET_CAMERA_LENS", "STOP_CAMERA_STREAM"):
            # Live camera preview: the phone's app is the frame source (works USB and remote).
            lens = "front" if payload.get("lens") == "front" else "back"
            dev = self.device_manager.get_device(device_id)
            comp_sock = self._get_companion_socket(dev)
            cam_stream = f"{dev.id if dev else device_id}#cam"
            did = dev.id if dev else device_id
            if msg_type == "START_CAMERA_STREAM":
                self.fanout.subscribe(websocket, cam_stream)
                self.fanout.subscribe(websocket, f"{did}#mic")   # the camera's sound (AAC from app 1.9.29+)
                state = self._cam_state.setdefault(did, {})   # marks the camera as started (stopped if every viewer leaves)
                if state:                                # camera already live: tell this newcomer how to draw it
                    await websocket.send_json({"type": "CAMERA_STREAM_STATE", "device_id": did, "payload": state})
            elif msg_type == "STOP_CAMERA_STREAM":
                self.fanout.unsubscribe(websocket, cam_stream)
                if websocket not in self._listeners.get(did, ()):
                    self.fanout.unsubscribe(websocket, f"{did}#mic")
                if self.fanout.subscribers(cam_stream):
                    return                               # another dashboard is still watching this camera
                self._forget_camera(did)
            if comp_sock:
                cam_payload = {"lens": lens}
                if msg_type == "START_CAMERA_STREAM":
                    # H.264 only when every viewer can decode it (one stream per camera); otherwise JPEG for all
                    everyone_h264 = self.fanout.wants_h264(websocket) and self.fanout.jpeg_viewers(cam_stream) == 0
                    cam_payload["codec"] = "h264" if everyone_h264 else "jpeg"
                try:
                    await comp_sock.send_json({"type": msg_type, "payload": cam_payload})
                except Exception as e:
                    print(f"[WebSocketHub] Error dispatching {msg_type}: {e}")
            else:
                await websocket.send_json({"type": "CAMERA_STREAM_STATE", "device_id": device_id,
                                           "payload": {"active": False, "error": "Phone app is not connected"}})

        elif msg_type in ("START_PHONE_AUDIO", "STOP_PHONE_AUDIO"):
            # "Listen Live": the phone microphone, live, in this dashboard
            dev = self.device_manager.get_device(device_id)
            did = dev.id if dev else device_id
            if not did:
                return
            comp_sock = self._get_companion_socket(dev)
            if msg_type == "START_PHONE_AUDIO":
                error = None
                if not comp_sock:
                    error = "Phone app is not connected"
                elif "aac" not in self._phone_media.get(dev.relay_id if dev else did, set()):
                    error = "Update the phone app (1.9.29 or newer) to listen live"
                elif not self.fanout.wants_h264(websocket):
                    error = "Listening needs a secure dashboard (open it on localhost or https)"
                if error:
                    await websocket.send_json({"type": "PHONE_AUDIO_STATE", "device_id": did,
                                               "payload": {"active": False, "error": error}})
                    return
                first = not self._listeners.get(did)
                self._listeners.setdefault(did, set()).add(websocket)
                self.fanout.subscribe(websocket, f"{did}#mic")
                if first:
                    try:
                        await comp_sock.send_json({"type": "START_PHONE_AUDIO", "payload": {}})
                    except Exception as e:
                        print(f"[WebSocketHub] Error starting phone audio: {e}")
            else:
                await self._stop_listening(did, websocket)

        elif msg_type == "PHONE_AUDIO_STATE":
            # From the phone: Listen Live started / stopped / failed
            _d = self._device_for_companion(device_id)
            did = _d.id if _d else device_id
            if payload.get("active") is False:
                for ws in list(self._listeners.pop(did, ())):
                    if self.fanout.subscribers(f"{did}#cam") == 0 or ws not in self.fanout.subscriber_sockets(f"{did}#cam"):
                        self.fanout.unsubscribe(ws, f"{did}#mic")
                self._maybe_forget_mic(did)
            await self.broadcast_to_dashboards({"type": "PHONE_AUDIO_STATE", "device_id": did, "payload": payload})

        elif msg_type == "SCREEN_STATE":
            # From the phone: remote screen sharing started / refused / stopped
            _d = self._device_for_companion(device_id)
            did = _d.id if _d else device_id
            st = self._remote_screen.get(did)
            if st is not None:
                if payload.get("active"):
                    st["state"] = "live"
                    st["info"] = payload
                else:
                    st["state"] = "failed" if payload.get("error") else "stopped"
                    st["error"] = payload.get("error")
                st["changed"].set()

        elif msg_type == "START_CAMERA_SAVE":
            from ..services.cam_record import CamRecorder, FFMPEG
            dev = self.device_manager.get_device(device_id)
            if not FFMPEG:
                await websocket.send_json({"type": "CAMERA_SAVE_STATE", "device_id": device_id,
                                           "payload": {"saving": False, "error": "ffmpeg is not installed on the laptop"}})
            elif device_id and device_id not in self.cam_recorders:
                folder = device_capture_dir(dev or device_id)
                # picks H.264 copy or JPEG re-encode from the first frame (Save may come before it) and starts a
                # new upright file when the lens changes
                self.cam_recorders[device_id] = AutoRecorder(
                    dev.name if dev else device_id, folder=folder, config=self._cam_config.get(device_id),
                    rotation=self._cam_rotation.get(device_id, 0), audio_config=self._mic_config.get(device_id),
                    legacy_audio=device_id in self._legacy_pcm)
                sock = self._get_companion_socket(dev) if dev else None
                if sock and self._cam_codec.get(device_id) == "h264":   # start at a key frame now, not up to 2 s later
                    try:
                        await sock.send_json({"type": "CAMERA_KEYFRAME"})
                    except Exception:
                        pass
                await self.broadcast_to_dashboards({"type": "CAMERA_SAVE_STATE", "device_id": device_id,
                                                    "payload": {"saving": True}})

        elif msg_type == "STOP_CAMERA_SAVE":
            self._finalize_recording_soon(device_id)  # never stall this socket while ffmpeg finishes

        elif msg_type in ("CAMERA_FRAME", "CAMERA_STREAM_STATE", "CAMERA_AUDIO"):
            # From the phone → fan out to every dashboard watching this device.
            _d = self._device_for_companion(device_id)
            did = _d.id if _d else device_id
            rec = self.cam_recorders.get(did)
            if msg_type == "CAMERA_STREAM_STATE":
                if payload.get("active") is False:
                    self._forget_camera(did)
                else:
                    self._cam_state[did] = {**self._cam_state.get(did, {}),
                                            **{k: v for k, v in payload.items() if k != "error"}}
            if msg_type == "CAMERA_STREAM_STATE" and payload.get("codec") == "h264" and "rotation" in payload:
                try:
                    self._cam_rotation[did] = int(payload["rotation"])
                    if rec is not None and hasattr(rec, "set_rotation"):
                        rec.set_rotation(self._cam_rotation[did])   # lens switch: continue in a new upright file
                except (TypeError, ValueError):
                    pass
            if msg_type == "CAMERA_FRAME":
                self._cam_codec[did] = "jpeg"
            if msg_type == "CAMERA_FRAME" and rec and payload.get("data_base64"):
                try:
                    rec.submit(base64.b64decode(payload["data_base64"]))  # never blocks the event loop
                except Exception:
                    pass
            if msg_type == "CAMERA_AUDIO":
                self._legacy_pcm.add(did)                # an older app: its recordings keep the PCM path
            if msg_type == "CAMERA_AUDIO" and rec and payload.get("pcm_base64"):
                try:
                    rec.submit_audio(base64.b64decode(payload["pcm_base64"]), int(payload.get("sample_rate") or 16000))
                except Exception:
                    pass
            if msg_type == "CAMERA_STREAM_STATE" and payload.get("active") is False and did in self.cam_recorders:
                self._finalize_recording_soon(did)  # stream ended → close the file (in the background)
            await self.broadcast_to_dashboards({"type": msg_type, "device_id": did, "payload": payload})

        elif msg_type == "STOP_SCREEN_STREAM":
            if device_id:
                self.fanout.unsubscribe(websocket, device_id)
                await self._stop_unwatched(device_id)

        elif msg_type == "MIRROR_RESYNC":
            if device_id and websocket in self.dashboard_clients:
                self.fanout.request_resync(websocket, device_id)

        elif msg_type == "CLIENT_CAPS":
            # The browser says whether it can decode H.264 (WebCodecs in a secure context)
            if websocket in self.dashboard_clients:
                self.fanout.add(websocket, bool(payload.get("h264")))

        # 2. Touch / Input gestures
        elif msg_type == "INPUT_TOUCH":
            dev = self.device_manager.get_device(device_id)
            if dev:
                x, y = int(payload.get("x", 0)), int(payload.get("y", 0))
                await self.input_router.tap(dev.id, x, y)

        elif msg_type == "INPUT_SWIPE":
            dev = self.device_manager.get_device(device_id)
            if dev:
                x1, y1 = int(payload.get("x1", 0)), int(payload.get("y1", 0))
                x2, y2 = int(payload.get("x2", 0)), int(payload.get("y2", 0))
                dur = int(payload.get("duration_ms", 300))
                await self.input_router.swipe(dev.id, x1, y1, x2, y2, dur)

        elif msg_type == "INPUT_POINTER":
            # live finger from the dashboard canvas: down / move / up as they happen
            dev = self.device_manager.get_device(device_id)
            action = payload.get("action")
            if dev and action in ("down", "move", "up"):
                try:
                    x, y = int(payload.get("x", 0)), int(payload.get("y", 0))
                except (TypeError, ValueError):
                    return
                await self.input_router.pointer(dev.id, action, x, y)

        elif msg_type == "INPUT_SCROLL":
            dev = self.device_manager.get_device(device_id)
            if dev:
                try:
                    x, y, v = int(payload.get("x", 0)), int(payload.get("y", 0)), float(payload.get("v", 0))
                except (TypeError, ValueError):
                    return
                await self.input_router.scroll(dev.id, x, y, v)

        elif msg_type == "INPUT_KEY":
            dev = self.device_manager.get_device(device_id)
            if dev:
                key = payload.get("key", "BACK")
                await self.input_router.key(dev.id, key)

        elif msg_type == "INPUT_TEXT":
            dev = self.device_manager.get_device(device_id)
            if dev:
                text = payload.get("text", "")
                try:
                    await self.input_router.text(dev.id, text)
                except StorageError as e:   # the companion refused it (e.g. no text field focused)
                    await websocket.send_json({"type": "INPUT_ERROR", "device_id": device_id,
                                               "payload": {"message": e.message}})
                except (UnsupportedText, InputRejected) as e:
                    await websocket.send_json({"type": "INPUT_ERROR", "device_id": device_id,
                                               "payload": {"message": str(e)}})

        # 3. AI Operator Tasks
        elif msg_type == "SUBMIT_TASK":
            instruction = payload.get("instruction", "")
            target_device = payload.get("target_device_id", device_id)
            if instruction and target_device:
                await self.ai_agent.submit_task(instruction, target_device)

        elif msg_type == "APPROVAL_RESPONSE":
            task_id = payload.get("task_id", "")
            decision_str = payload.get("decision", "rejected")
            modified_text = payload.get("modified_text")
            decision = ApprovalDecision(decision=decision_str, modified_text=modified_text)
            self.ai_agent.handle_approval_response(task_id, decision)

        # 4. Telephony & Live Call Handling
        elif msg_type == "ACCEPT_CALL":
            dev = self.device_manager.get_device(device_id)
            if dev:
                print(f"[WebSocketHub] Accepting call on {dev.name} ({dev.serial})")
                await self.broadcast_to_dashboards({
                    "type": "CALL_ACTIVE",
                    "device_id": dev.id,
                    "payload": {"state": "active"}
                })
                # One path only: ADB keyevent over USB, the companion app's call bridge when remote
                asyncio.create_task(self.adb.answer_incoming_call(dev.serial))

        elif msg_type == "END_CALL":
            dev = self.device_manager.get_device(device_id)
            if dev:
                print(f"[WebSocketHub] Ending call on {dev.name} ({dev.serial})")
                await self.adb.end_call(dev.serial)  # ADB over USB, companion app when remote
                await self.broadcast_to_dashboards({
                    "type": "CALL_ENDED",
                    "device_id": dev.id,
                    "payload": {"state": "idle"}
                })

        # 5. Bidirectional Call Audio Streaming
        elif msg_type == "LAPTOP_MIC_AUDIO":
            # Laptop microphone PCM audio streamed to phone during active call
            dev = self.device_manager.get_device(device_id)
            comp_sock = self._get_companion_socket(dev)
            if comp_sock:
                try:
                    await comp_sock.send_json({
                        "type": "PLAY_AUDIO",
                        "payload": payload
                    })
                except Exception as e:
                    print(f"[WebSocketHub] Error forwarding mic audio to companion: {e}")

        elif msg_type == "PHONE_AUDIO_CHUNK":
            # Phone call audio chunk streamed from companion app to laptop speakers
            await self.broadcast_to_dashboards({
                "type": "PHONE_AUDIO_CHUNK",
                "device_id": device_id,
                "payload": payload
            })

        elif msg_type == "DEVICE_HELLO":
            comp_id = msg.get("device_id")
            if comp_id:
                if self.relays:
                    # Verify identity BEFORE touching any routing table (an impostor must not displace the real phone)
                    allowed, new_secret = self.relays.authorize(comp_id, payload.get("relay_secret"), websocket)
                    if not allowed:
                        print(f"[WebSocketHub] Refused {comp_id}: identity is bound to another install")
                        self.dashboard_clients.discard(websocket)
                        await websocket.close(code=4403)
                        return
                self.device_clients[comp_id] = websocket
                media = payload.get("media")
                self._phone_media[comp_id] = {str(m) for m in media} if isinstance(media, list) else set()
                # A phone is not a dashboard: stop sending it screen frames/broadcasts.
                self.dashboard_clients.discard(websocket)
                if self.relays:
                    if new_secret:
                        await websocket.send_json({"type": "RELAY_BOUND", "payload": {"secret": new_secret}})
                    self.relays.attach(comp_id, websocket)
                    await self.device_manager.register_relay(comp_id, payload)
                print(f"[WebSocketHub] Registered companion app socket for {comp_id}")
                if self.pb and self.relays:
                    await self.pb.on_connect(comp_id)
                # Request fresh capability matrix from connected device
                try:
                    await websocket.send_json({"type": "GET_CAPABILITIES"})
                except Exception:
                    pass

        elif msg_type in PB_PHONE_TYPES:
            me = self.relays.for_socket(websocket) if self.relays else None
            if self.pb and me:  # only an authenticated phone, and only about itself
                await self.pb.handle_phone(websocket, me.relay_id, msg)

        elif msg_type in ("GAME_JOIN", "GAME_STRIKE", "GAME_LEAVE"):
            did = msg.get("device_id") or payload.get("device_id") or ""
            name = payload.get("name", "")
            if msg_type == "GAME_JOIN":
                self.game.join(did, name)
            elif msg_type == "GAME_LEAVE":
                self.game.leave(did)
            else:
                self.game.strike(did, name, payload.get("power", 1))
            await self._broadcast_game()

        elif msg_type == "WAITLIST_GET":
            from ..services import waitlist
            did = msg.get("device_id") or payload.get("device_id") or ""
            await websocket.send_json({"type": "WAITLIST_COUNT",
                                       "payload": {"count": waitlist.count(), "joined": waitlist.has(did)}})

        elif msg_type == "WAITLIST_JOIN":
            from ..services import waitlist
            did = msg.get("device_id") or payload.get("device_id") or ""
            total = waitlist.join(did)
            # everyone's counter ticks up
            for sock in list(self.device_clients.values()):
                try:
                    await sock.send_json({"type": "WAITLIST_COUNT", "payload": {"count": total, "joined": sock is websocket or None}})
                except Exception:
                    pass
            await websocket.send_json({"type": "WAITLIST_COUNT", "payload": {"count": total, "joined": True}})

        elif msg_type == "APP_UPDATE_CHECK":
            from ..services import app_update
            info = app_update.latest(payload.get("edition"))
            if info:
                await websocket.send_json({"type": "APP_UPDATE_INFO", "payload": {
                    "version_code": info["version_code"], "version_name": info["version_name"],
                    "size": info["size"], "sha256": info["sha256"]}})

        elif msg_type == "APP_UPDATE_PULL":
            from ..services import app_update
            from ..services.relay import pack
            edition = payload.get("edition")
            offset = max(0, int(payload.get("offset", 0)))
            chunk = app_update.read_chunk(edition, offset, 512 * 1024)
            info = app_update.latest(edition)
            size = info["size"] if info else 0
            eof = not chunk or (offset + len(chunk)) >= size
            await websocket.send_bytes(pack({"type": "APP_CHUNK", "offset": offset, "eof": eof}, chunk))

        elif msg_type == "APP_LIFECYCLE_STATUS":
            # Phone's answer to APP_UNINSTALL / APP_UPDATE_FORCE: accepted / done / needs_tap / failed (+ reason)
            me = self.relays.for_socket(websocket) if self.relays else None
            if me:
                dev = self._device_for_companion(me.relay_id)
                if dev and "device_owner" in payload:
                    dev.device_owner = bool(payload.get("device_owner"))
                print(f"[AppLifecycle] {dev.name if dev else me.relay_id}: {payload.get('action')} "
                      f"{payload.get('status')} {payload.get('reason') or ''}".rstrip())
                await self.broadcast_to_dashboards({"type": "APP_LIFECYCLE_STATUS",
                                                    "device_id": dev.id if dev else me.relay_id,
                                                    "payload": {k: payload.get(k) for k in ("action", "status", "reason")}})

        elif msg_type == "RELAY_BOUND_ACK":
            client = self.relays.for_socket(websocket) if self.relays else None
            if client and client.relay_id == msg.get("device_id") and payload.get("h"):
                self.relays.confirm(client.relay_id, payload["h"])

        elif msg_type == "DEVICE_CAPABILITIES":
            caps = msg.get("payload", {})
            # a relayed phone may only report about itself: use the identity it authenticated with
            client = self.relays.for_socket(websocket) if self.relays else None
            comp_id = client.relay_id if client else msg.get("device_id", device_id)
            updated_dev = self.device_manager.update_device_capabilities(comp_id, caps)
            if updated_dev and updated_dev.id in self._caps_waiters:
                self._caps_waiters[updated_dev.id].set()
            if updated_dev:
                print(f"[WebSocketHub] Updated device capabilities for {updated_dev.name}")
                await self.broadcast_to_dashboards({
                    "type": "DEVICE_CAPABILITIES_UPDATED",
                    "device_id": updated_dev.id,
                    "payload": {
                        "device_id": updated_dev.id,
                        "hardware_capabilities": updated_dev.hardware_capabilities,
                        "permissions": updated_dev.permissions,
                        "power_and_lockscreen": updated_dev.power_and_lockscreen,
                        "manufacturer": updated_dev.manufacturer,
                        "sdk_int": updated_dev.sdk_int
                    }
                })

        elif msg_type == "DEVICE_ERROR":
            # Centralized Error Telemetry from Android Companion
            _d = self._device_for_companion(device_id or msg.get("device_id"))
            matched_id = _d.id if _d else (device_id or msg.get("device_id"))

            report = {
                "device_id": matched_id or "unknown_device",
                "timestamp": msg.get("timestamp", time.time() * 1000),
                "module": msg.get("module", "system"),
                "operation": msg.get("operation", "unknown"),
                "error_code": msg.get("error_code", "ERROR_GENERIC"),
                "description": msg.get("description", "An error occurred on the device"),
                "suggested_resolution": msg.get("suggested_resolution", "Inspect diagnostic telemetry"),
                "device_info": msg.get("device_info", {}),
                "stack_trace": msg.get("stack_trace")
            }
            self.device_manager.add_diagnostic_report(report)
            print(f"[WebSocketHub] ⚠️ DEVICE_ERROR [{report['device_id']}] {report['module']} -> {report['operation']} | {report['error_code']}: {report['description']}")
            await self.broadcast_to_dashboards({
                "type": "DEVICE_ERROR",
                "device_id": matched_id,
                "payload": report
            })

        elif msg_type == "GET_CAPABILITIES":
            dev = self.device_manager.get_device(device_id)
            comp_sock = self._get_companion_socket(dev)
            if comp_sock:
                try:
                    await comp_sock.send_json({"type": "GET_CAPABILITIES"})
                except Exception:
                    pass

        elif msg_type in ("INCOMING_CALL", "CALL_ACTIVE", "CALL_ENDED"):
            _d = self._device_for_companion(device_id)
            matched_id = _d.id if _d else device_id
            # remember it: remote phones have no ADB telephony polling
            self.device_manager.device_call_states[matched_id] = {
                "state": {"INCOMING_CALL": 1, "CALL_ACTIVE": 2}.get(msg_type, 0),
                "incoming_number": payload.get("phone_number", "") if msg_type != "CALL_ENDED" else ""}
            await self.broadcast_to_dashboards({
                "type": msg_type,
                "device_id": matched_id,
                "payload": payload
            })

        # 6. Silent Background Camera / Video Handlers
        elif msg_type == "CAPTURE_PHOTO":
            lens = "front" if payload.get("lens") == "front" else "back"  # never pass free text to a shell
            dev = self.device_manager.get_device(device_id)
            comp_sock = self._get_companion_socket(dev)
            if dev and self._on_usb(dev):
                # USB/ADB: the shell may always open the headless camera screen (an app in the
                # background may not, since Android 10)
                asyncio.create_task(self._capture_photo_adb_fallback(dev.id, dev.serial, lens))
            elif comp_sock:
                try:
                    await comp_sock.send_json({"type": "CAPTURE_PHOTO", "payload": {"lens": lens}})
                except Exception as e:
                    print(f"[WebSocketHub] Error dispatching CAPTURE_PHOTO: {e}")
            elif dev:
                asyncio.create_task(self._capture_photo_adb_fallback(dev.id, dev.serial, lens))

        elif msg_type == "RECORD_VIDEO":
            lens = "front" if payload.get("lens") == "front" else "back"
            try:  # 0 = record until STOP_VIDEO
                duration_sec = max(0, min(int(payload.get("duration_sec", 5)), 3600))
            except (TypeError, ValueError):
                duration_sec = 5
            dev = self.device_manager.get_device(device_id)
            comp_sock = self._get_companion_socket(dev)
            if dev and self._on_usb(dev):
                asyncio.create_task(self._record_video_adb_fallback(dev.id, dev.serial, lens, duration_sec))
            elif comp_sock:
                try:
                    await comp_sock.send_json({"type": "RECORD_VIDEO", "payload": {"lens": lens, "duration_sec": duration_sec}})
                except Exception as e:
                    print(f"[WebSocketHub] Error dispatching RECORD_VIDEO: {e}")
            elif dev:
                asyncio.create_task(self._record_video_adb_fallback(dev.id, dev.serial, lens, duration_sec))

        elif msg_type == "STOP_VIDEO":
            dev = self.device_manager.get_device(device_id) if device_id else None
            if dev:
                ev = getattr(self, "_video_stop", {}).get(dev.id)
                if ev is not None:
                    ev.set()  # USB recording: the ADB loop sends the stop to the phone
                else:
                    comp_sock = self._get_companion_socket(dev)
                    if comp_sock:
                        await comp_sock.send_json({"type": "STOP_VIDEO"})

        elif msg_type == "SWITCH_VIDEO_LENS":
            # Move a running Record Video to the other camera (the phone starts a new clip; joined at the end)
            lens = "front" if payload.get("lens") == "front" else "back"
            dev = self.device_manager.get_device(device_id) if device_id else None
            if dev:
                if dev.id in getattr(self, "_video_stop", {}):   # USB recording
                    await self.adb.run_device_shell(dev.serial, "am broadcast -n com.operator.companion/.receiver.HeadlessCameraReceiver "
                                                                f"-a com.operator.companion.ACTION_SWITCH_VIDEO_LENS --es lens {lens}")
                else:
                    comp_sock = self._get_companion_socket(dev)
                    if comp_sock:
                        await comp_sock.send_json({"type": "SWITCH_VIDEO_LENS", "payload": {"lens": lens}})

        elif msg_type == "PHOTO_CAPTURED":
            b64_data = payload.get("data_base64")
            remote_path = payload.get("file_path")
            success = payload.get("success", False)
            hunt = payload.get("hunt_species")
            safe = "".join(ch for ch in str(hunt) if ch.isalnum())[:30] if hunt else ""
            dest_filename = (f"hunt_{safe}_" if hunt else ("hunt_photo_" if "hunt_species" in payload else "capture_silent_")) \
                + f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.jpg"
            dev = self._device_for_companion(device_id)
            matched_id = dev.id if dev else device_id
            if self._duplicate_of_adb_capture(matched_id):
                return  # the USB capture already took this file and reports the result itself
            dest_path = device_capture_dir(dev or device_id) / dest_filename   # this phone's own folder

            if b64_data:
                try:
                    dest_path.write_bytes(base64.b64decode(b64_data))
                except Exception as e:
                    print(f"[WebSocketHub] Error saving base64 photo: {e}")
            saved = dest_path.exists() and dest_path.stat().st_size > 0
            if success and remote_path and dev and not saved:
                saved = await self._fetch_capture(dev, remote_path, dest_path)
            if success and saved:
                print(f"[WebSocketHub] Photo from {dev.name if dev else device_id} saved to laptop: {dest_filename}")

            result_info = {
                "success": success and saved,
                "error": None if (success and saved) else (payload.get("error") or "Photo could not be copied to the laptop"),
                "filename": dest_filename,
                "local_path": str(dest_path),
                "url": download_url(dest_path),
                "device_id": matched_id,
                "lens": payload.get("lens", "back")
            }

            if matched_id in self.pending_photo_futures and not self.pending_photo_futures[matched_id].done():
                self.pending_photo_futures[matched_id].set_result(result_info)

            await self.broadcast_to_dashboards({
                "type": "PHOTO_CAPTURED",
                "device_id": matched_id,
                "payload": result_info
            })

        elif msg_type == "VIDEO_RECORDED":
            remote_path = payload.get("file_path")
            success = payload.get("success", False)
            dest_filename = f"video_silent_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.mp4"
            dev = self._device_for_companion(device_id)
            matched_id = dev.id if dev else device_id
            dest_path = device_capture_dir(dev or device_id) / dest_filename   # this phone's own folder
            if self._duplicate_of_adb_capture(matched_id):
                return
            parts = [p for p in (payload.get("parts") or []) if isinstance(p, str) and p]
            if success and dev and len(parts) > 1:   # camera was switched mid-recording: fetch every clip, then join
                from ..services.video_merge import merge_parts
                local = []
                for i, rp in enumerate(parts):
                    lp = DOWNLOADS_DIR / f".part_{uuid.uuid4().hex[:8]}_{i}.mp4"
                    if await self._fetch_capture(dev, rp, lp):
                        local.append(lp)
                saved = bool(local) and bool(await asyncio.get_event_loop().run_in_executor(None, merge_parts, local, dest_path))
            else:
                saved = bool(success and remote_path and dev) and await self._fetch_capture(dev, remote_path, dest_path)
            if success and saved:
                print(f"[WebSocketHub] Video from {dev.name if dev else device_id} saved to laptop: {dest_filename}")

            result_info = {
                "success": success and saved,
                "error": None if (success and saved) else (payload.get("error") or "Video could not be copied to the laptop"),
                "filename": dest_filename,
                "local_path": str(dest_path),
                "url": download_url(dest_path),
                "device_id": matched_id,
                "duration_sec": payload.get("duration_sec", 5),
                "lens": payload.get("lens", "back")
            }

            if matched_id in self.pending_video_futures and not self.pending_video_futures[matched_id].done():
                self.pending_video_futures[matched_id].set_result(result_info)

            await self.broadcast_to_dashboards({
                "type": "VIDEO_RECORDED",
                "device_id": matched_id,
                "payload": result_info
            })

    def _on_usb(self, dev) -> bool:
        uses = getattr(self.adb, "uses_adb", None)
        return uses(dev) if uses else (dev.transport == "adb" and dev.status != "offline")

    def _device_for_companion(self, comp_id: Optional[str]):
        """Map a companion app id (or a dashboard device id) to the registry entry."""
        for d in self.device_manager.get_all_devices():
            if comp_id and (d.relay_id == comp_id or d.id == comp_id):
                return d
        return self.device_manager.get_device(comp_id) if comp_id else None

    async def _fetch_capture(self, dev, remote_path: str, dest_path) -> bool:
        """Copy a capture to the laptop (USB/ADB, else the remote relay) and remove it from the phone."""
        try:
            if self._on_usb(dev):
                if await self.adb.pull_file(dev.serial, remote_path, dest_path) and dest_path.stat().st_size > 0:
                    await self.adb.run_device_shell(dev.serial, f"rm -f {shlex.quote(remote_path)} {shlex.quote(remote_path + '.done')}")
                    return True
            rs = getattr(self, "relay_storage", None)
            if rs and self.relays and self.relays.get(dev.relay_id):
                entry = await rs.stat(dev.relay_id, remote_path)
                with open(dest_path, "wb") as fh:
                    async for chunk in rs.read_range(dev.relay_id, entry["path"], 0, entry["size"]):
                        fh.write(chunk)
                if dest_path.stat().st_size == entry["size"] and entry["size"] > 0:
                    for leftover in (remote_path, remote_path + ".done"):
                        try:
                            await rs.delete(dev.relay_id, [leftover])
                        except Exception:
                            pass
                    return True
        except Exception as e:
            print(f"[WebSocketHub] Could not copy capture {remote_path} from {dev.name}: {e}")
        dest_path.unlink(missing_ok=True)  # never keep a partial file
        return False

    async def _screen_stream_loop(self, device_id: str):
        """Mirror while a dashboard has it switched on: ~10 fps over USB/ADB, ~1 fps from remote
        phones (Android limits accessibility screenshots to about one per second)."""
        dev = self.device_manager.get_device(device_id)
        if not dev:
            return
        print(f"[WebSocketHub] Starting screen stream for {dev.name}")
        notice = None    # last reason sent to dashboards (sent once per reason)
        failing = 0      # consecutive capture errors: logged once per streak, not per frame

        async def tell(reason):
            nonlocal notice
            if reason is None:                    # picture flowing (it paints over the notice): forget the reason
                notice = None
                return
            if reason != notice:
                notice = reason
                print(f"[WebSocketHub] Screen mirror for {dev.name}: {reason}")
                await self.broadcast_to_dashboards({"type": "SCREEN_UNAVAILABLE", "device_id": device_id,
                                                    "payload": {"reason": reason}})

        while device_id in self.active_streaming_devices and self.fanout.subscribers(device_id) > 0:
            dev = self.device_manager.get_device(device_id)
            if not dev:
                break
            on_usb = self._on_usb(dev)
            remote = not on_usb and self.relays is not None and self.relays.get(dev.relay_id) is not None
            if not on_usb and not remote:
                await tell("Phone is offline. Mirroring resumes when it reconnects.")
                await asyncio.sleep(2.0)
                continue
            if (on_usb and FAST_MIRROR and self.fanout.h264_viewers(device_id) > 0
                    and time.time() - self._fast_mirror_failed.get(device_id, 0) > self._fast_mirror_retry.get(device_id, 60.0)):
                if await self._run_fast_mirror(dev, device_id, tell):
                    notice = None
                    if device_id in self.active_streaming_devices and self.fanout.h264_viewers(device_id) > 0:
                        await asyncio.sleep(2.0)  # stream ended (unplugged / server died): retry shortly
                    continue
            if (remote and self.fanout.h264_viewers(device_id) > 0
                    and "screen_h264" in self._phone_media.get(dev.relay_id, set())
                    and time.time() - self._remote_screen_failed.get(device_id, 0) > self.REMOTE_SCREEN_RETRY):
                if await self._run_remote_screen(dev, device_id, tell):
                    notice = None
                    continue
            start_time = asyncio.get_event_loop().time()
            try:
                _, b64_frame = await self.adb.capture_screen(dev.serial, scale_width=540, quality=70 if on_usb else 55)
                await self._send_to(self.fanout.subscriber_sockets(device_id), {
                    "type": "SCREEN_FRAME",
                    "device_id": device_id,
                    "payload": {"width": 540, "data_base64": b64_frame}
                })
                if failing or notice:
                    print(f"[WebSocketHub] Screen mirror for {dev.name} is running again")
                failing, notice = 0, None
                elapsed = asyncio.get_event_loop().time() - start_time
                await asyncio.sleep(max(0.06, 0.1 - elapsed) if on_usb else max(0.0, 1.05 - elapsed))
            except Exception as e:
                status = getattr(e, "status", 0)
                if status == 429:  # remote screenshot requested too early: just wait a moment
                    await asyncio.sleep(0.4)
                    continue
                failing += 1
                if status in (403, 412, 501):  # needs a setting / not supported on this phone
                    await tell(str(getattr(e, "message", e)))
                    await asyncio.sleep(5.0)
                    continue
                if failing == 1:
                    print(f"[WebSocketHub] Frame capture error for {dev.name}: {e} (retrying quietly)")
                await asyncio.sleep(min(5.0, 0.5 * failing))

        print(f"[WebSocketHub] Screen stream stopped for {dev.name if dev else device_id}")

    async def handle_device_binary(self, websocket, data: bytes) -> bool:
        """Hardware-H.264 camera packets from a phone → H.264 viewers (stream '<device>#cam') and the recorder.
        Returns False for every other binary frame (file transfers, PowerBoosters chunks…)."""
        header, payload = parse_device_binary(data)
        if not header or header.get("type") not in ("CAMERA_PKT", "SCREEN_PKT") or not self.relays:
            return False
        me = self.relays.for_socket(websocket)
        if me is None:
            return True                                     # not a registered phone: drop
        dev = self._device_for_companion(me.relay_id)
        did = dev.id if dev else me.relay_id
        if len(payload) > MAX_CAMERA_PACKET:
            return True                                      # a 720p key frame is ~200 KB: anything this big is bogus
        try:
            kind, pts = int(header.get("k", 3)), int(header.get("pts", 0))
            if kind not in (1, 2, 3, 4, 5) or not 0 <= pts < 2 ** 62:
                return True
        except (TypeError, ValueError, OverflowError):
            return True
        if header.get("type") == "SCREEN_PKT":
            st = self._remote_screen.get(did)
            if st is None or kind not in (1, 2, 3):
                return True                                  # nobody asked for it (late packets after a stop)
            await self.fanout.publish(did, VideoPacket(pts, kind == 1, kind == 2, payload))
            if kind != 1:
                st["frames"] += 1
                if st["state"] != "live":
                    st["state"] = "live"
                    st["changed"].set()
            return True
        rec = self.cam_recorders.get(did)
        if kind in (4, 5):                                   # AAC microphone (same clock as the video)
            if kind == 4:
                self._mic_config[did] = payload
                self._legacy_pcm.discard(did)
            await self.fanout.publish_audio(f"{did}#mic", kind, pts, payload)
            if rec is not None and hasattr(rec, "submit_audio_packet"):
                rec.submit_audio_packet(kind, pts, payload)
            return True
        self._cam_codec[did] = "h264"
        if kind == 1:
            self._cam_config[did] = payload          # a recording started later needs it to begin
        await self.fanout.publish(f"{did}#cam", VideoPacket(pts, kind == 1, kind == 2, payload))
        if rec is not None and hasattr(rec, "submit_video"):
            rec.submit_video(kind, pts, payload)
        return True

    REMOTE_SCREEN_RETRY = 300.0        # after a refusal, don't ask the phone again for 5 min (unless the user restarts)
    REMOTE_SCREEN_CONSENT = 45.0       # seconds the user has to tap "Start now" on the phone

    async def _run_remote_screen(self, dev, device_id: str, tell) -> bool:
        """H.264 screen from the companion app (phone on Wi-Fi / relay) until it is no longer wanted.
        False = the phone refused or failed (caller falls back to Accessibility screenshots)."""
        sock = self._get_companion_socket(dev)
        if sock is None:
            return False
        st = {"state": "starting", "error": None, "frames": 0, "changed": asyncio.Event(), "info": None}
        self._remote_screen[device_id] = st
        try:
            await sock.send_json({"type": "START_SCREEN_PKT", "payload": {}})
        except Exception:
            self._remote_screen.pop(device_id, None)
            return False
        await tell("Connecting to the phone's screen… if the phone asks, tap “Start now”.")
        asked = time.monotonic()
        legacy = asyncio.create_task(self._snapshots_for_jpeg_viewers(device_id, dev.serial))
        ok = True
        try:
            while True:
                cur = self.device_manager.get_device(device_id)
                if cur is None or device_id not in self.active_streaming_devices or self.fanout.h264_viewers(device_id) == 0:
                    break
                if self._on_usb(cur) and FAST_MIRROR:
                    break                                    # plugged in: the USB mirror takes over
                if self._get_companion_socket(cur) is not sock:
                    break                                    # phone reconnected: start over with the new socket
                if st["state"] == "failed" or (st["state"] == "starting"
                                               and time.monotonic() - asked > self.REMOTE_SCREEN_CONSENT):
                    reason = st["error"] or "No answer on the phone"
                    await tell(f"{reason} — showing slower screenshots instead. Turn mirroring off and on to ask again.")
                    ok = False
                    break
                if st["state"] == "stopped":
                    await tell("Screen sharing ended on the phone — showing slower screenshots instead.")
                    ok = False
                    break
                if st["state"] == "live" and st["frames"]:
                    await tell(None)
                st["changed"].clear()
                try:
                    await asyncio.wait_for(st["changed"].wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            legacy.cancel()
            if self._remote_screen.get(device_id) is st:
                del self._remote_screen[device_id]
            if st["state"] in ("starting", "live"):
                try:
                    # nobody watching / USB took over: the phone pauses but keeps the user's consent for 5 min,
                    # so mirroring comes back without another "Start now"; a failure ends it
                    await sock.send_json({"type": "STOP_SCREEN_PKT", "payload": {"keep": ok}})
                except Exception:
                    pass
            self.fanout.forget_device(device_id)
        if not ok:
            self._remote_screen_failed[device_id] = time.time()
        return ok

    # adb errors of a phone that was plugged in a moment ago (not ready yet), not of a phone that can't mirror
    _ADB_NOT_READY = ("not found", "offline", "unauthorized", "no devices", "failed to get feature", "device still")

    def _mirror_failed(self, device_id: str, reason: str) -> bool:
        """Remember a fast-mirror failure. A not-ready adb is retried after 3 s (doubling to 60 s); anything else
        waits 60 s. Returns True for the not-ready case."""
        self._fast_mirror_failed[device_id] = time.time()
        transient = any(s in reason.lower() for s in self._ADB_NOT_READY)
        prev = self._fast_mirror_retry.get(device_id, 0.0)
        self._fast_mirror_retry[device_id] = min(60.0, max(3.0, prev * 2)) if transient else 60.0
        return transient

    async def _run_fast_mirror(self, dev, device_id: str, tell) -> bool:
        """Hardware-H.264 mirror over USB until it ends. False = could not start (caller uses snapshots)."""
        session = ScrcpySession(self.adb.adb_path, dev.serial, max_size=MIRROR_MAX_SIZE, bit_rate=MIRROR_BITRATE,
                                max_fps=MIRROR_FPS)
        try:
            await session.start()
        except Exception as e:
            if self._mirror_failed(device_id, str(e)):
                await tell("Phone just connected — starting the fast mirror…")
            else:
                await tell(f"Fast mirroring unavailable: {e} — using slow snapshots")
            return False
        self.mirror_sessions[device_id] = session
        self._fast_mirror_retry.pop(device_id, None)
        busy = getattr(self.device_manager, "busy_serials", None)
        if busy is not None:
            busy.add(dev.serial)
        print(f"[WebSocketHub] Fast mirroring (H.264 {session.width}x{session.height}) for {dev.name}")
        legacy = asyncio.create_task(self._snapshots_for_jpeg_viewers(device_id, dev.serial))
        frames = 0
        try:
            async for pkt in session.packets():
                await self.fanout.publish(device_id, pkt)
                if not pkt.is_config:
                    frames += 1
                if device_id not in self.active_streaming_devices or self.fanout.h264_viewers(device_id) == 0:
                    break
        finally:
            legacy.cancel()
            if busy is not None:
                busy.discard(dev.serial)
            await session.stop()
            if self.mirror_sessions.get(device_id) is session:
                del self.mirror_sessions[device_id]
            self.fanout.forget_device(device_id)
        wanted = device_id in self.active_streaming_devices and self.fanout.h264_viewers(device_id) > 0
        if frames == 0 and wanted:
            # the phone accepted the session but its encoder/capture died before the first frame
            self._fast_mirror_failed[device_id] = time.time()
            await tell("Fast mirroring unavailable: the phone's screen encoder stopped — using slow snapshots")
            return False
        return True

    async def _snapshots_for_jpeg_viewers(self, device_id: str, serial: str):
        """While H.264 runs, browsers without WebCodecs (e.g. a remote http:// dashboard) get ~1 fps JPEGs."""
        while True:
            jpeg_clients = self.fanout.jpeg_clients(device_id)   # only viewers that started this mirror
            if jpeg_clients:
                try:
                    _, b64 = await self.adb.capture_screen(serial, scale_width=540, quality=60)
                    msg = {"type": "SCREEN_FRAME", "device_id": device_id, "payload": {"width": 540, "data_base64": b64}}
                    for c in jpeg_clients:
                        try:
                            await asyncio.wait_for(c.send_json(msg), 2.0)
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            await asyncio.sleep(1.0)

    async def dashboard_left(self, websocket) -> None:
        """A dashboard disconnected: forget it; stop every mirror nobody watches any more."""
        self.dashboard_clients.discard(websocket)
        self.fanout.remove(websocket)
        for device_id in list(self.active_streaming_devices) + list(self.mirror_sessions):
            await self._stop_unwatched(device_id)
        for did in list(self._listeners):                    # nor its microphone
            await self._stop_listening(did, websocket)
        for did in list(self._cam_state):                   # a closed tab must not leave the phone's camera running
            if self.fanout.subscribers(f"{did}#cam") == 0:
                dev = self.device_manager.get_device(did)
                sock = self._get_companion_socket(dev) if dev else None
                if sock:
                    try:
                        await sock.send_json({"type": "STOP_CAMERA_STREAM", "payload": {}})
                    except Exception:
                        pass
                self._forget_camera(did)

    def _forget_camera(self, did: str) -> None:
        """The camera stopped: no stale picture or config may be replayed to the next session's viewers."""
        self._cam_state.pop(did, None)
        self._cam_config.pop(did, None)
        self.fanout.forget_device(f"{did}#cam")
        self._maybe_forget_mic(did)

    def _maybe_forget_mic(self, did: str) -> None:
        """The phone's mic runs for the camera and for Listen Live: forget its stream once neither uses it."""
        if did not in self._cam_state and not self._listeners.get(did):
            self._mic_config.pop(did, None)
            self.fanout.forget_device(f"{did}#mic")

    async def _stop_listening(self, did: str, websocket) -> None:
        listeners = self._listeners.get(did)
        if listeners is None or websocket not in listeners:
            return
        listeners.discard(websocket)
        if websocket not in self.fanout.subscriber_sockets(f"{did}#cam"):
            self.fanout.unsubscribe(websocket, f"{did}#mic")
        if listeners:
            return                                        # another dashboard is still listening
        del self._listeners[did]
        dev = self.device_manager.get_device(did)
        sock = self._get_companion_socket(dev) if dev else None
        if sock:
            try:
                await sock.send_json({"type": "STOP_PHONE_AUDIO", "payload": {}})
            except Exception:
                pass
        self._maybe_forget_mic(did)

    async def _stop_unwatched(self, device_id: str) -> None:
        if self.fanout.subscribers(device_id) == 0:
            self.active_streaming_devices.discard(device_id)
            session = self.mirror_sessions.get(device_id)
            if session:
                await session.stop()  # a static screen sends no packets: end the read loop now
        elif self.fanout.h264_viewers(device_id) == 0 and device_id in self.mirror_sessions:
            await self.mirror_sessions[device_id].stop()   # only JPEG viewers left: the loop switches to snapshots

    async def _send_to(self, sockets, message: Dict[str, Any]) -> None:
        for ws in sockets:
            try:
                await asyncio.wait_for(ws.send_json(message), 2.0)
            except Exception:
                pass

    async def broadcast_to_dashboards(self, message: Dict[str, Any]):
        clients = list(self.dashboard_clients)  # snapshot: the set may change while we await
        if not clients:
            return

        async def send(client):
            try:
                # A slow remote dashboard must not stall phone traffic handled on the same loop
                await asyncio.wait_for(client.send_json(message), 2.0)
            except Exception:
                # close it rather than keep a half-alive viewer: the browser reconnects and resubscribes
                self.dashboard_clients.discard(client)
                self.fanout.remove(client)
                close = getattr(client, "close", None)
                if close is not None:
                    asyncio.ensure_future(self._close_quietly(close))
        await asyncio.gather(*(send(c) for c in clients))

    def _finalize_recording_soon(self, device_id: Optional[str]) -> None:
        """Detach the recorder now (no more frames go to it) and finish the file in the background."""
        rec = self.cam_recorders.pop(device_id, None) if device_id else None
        if rec:
            asyncio.create_task(self._close_recording(device_id, rec))

    @staticmethod
    async def _close_quietly(close) -> None:
        try:
            await close(code=1011)
        except Exception:
            pass

    async def shutdown(self) -> None:
        """Server stopping: end every mirror and input session so no adb forward or phone process is left."""
        for session in list(self.mirror_sessions.values()):
            await session.stop()
        await self.input_router.close()

    async def _finalize_recording(self, device_id: Optional[str]):
        rec = self.cam_recorders.pop(device_id, None) if device_id else None
        if rec:
            await self._close_recording(device_id, rec)

    async def _close_recording(self, device_id: str, rec):
        path = await asyncio.get_event_loop().run_in_executor(None, rec.close)
        parts = [display_name(p) for p in (getattr(rec, "paths", None) or ([path] if path else []))]
        await self.broadcast_to_dashboards({"type": "CAMERA_SAVE_STATE", "device_id": device_id,
                                            "payload": {"saving": False, "path": path,
                                                        "name": display_name(path), "parts": parts}})

    async def _broadcast_game(self):
        msg = {"type": "GAME_STATE", "payload": self.game.snapshot()}
        await self.broadcast_to_dashboards(msg)
        for sock in list(self.device_clients.values()):
            try:
                await sock.send_json(msg)
            except Exception:
                pass

    async def notify_update_all(self) -> int:
        """Tell every connected companion to check for an app update right now."""
        n = 0
        for sock in list(self.device_clients.values()):
            try:
                await sock.send_json({"type": "APP_UPDATE_CHECK_NOW"})
                n += 1
            except Exception:
                pass
        return n

    async def close_remote_clients(self):
        """Token rotated: drop every non-local session (phones and remote dashboards) immediately."""
        from ..security import LOOPBACK
        socks = set(self.dashboard_clients) | set(self.device_clients.values())
        for ws in socks:
            host = ws.client.host if ws.client else ""
            if host not in LOOPBACK or "x-forwarded-for" in ws.headers:  # relayed phones arrive via loopback
                try:
                    await ws.close(code=4401)
                except Exception:
                    pass

    def _adb_capture(self, device_id: str, seconds: float):
        """Mark a USB capture as in progress: the camera screen also reports to the companion app,
        whose duplicate PHOTO_CAPTURED/VIDEO_RECORDED must be ignored (the file is already taken)."""
        if not hasattr(self, "_adb_capture_until"):
            self._adb_capture_until: Dict[str, float] = {}
        self._adb_capture_until[device_id] = time.time() + seconds

    def _duplicate_of_adb_capture(self, device_id: Optional[str]) -> bool:
        return time.time() < getattr(self, "_adb_capture_until", {}).get(device_id or "", 0)

    def _capture_lock(self, serial: str) -> asyncio.Lock:
        # One capture at a time per phone, so a slow photo can never be handed to the next request
        if not hasattr(self, "_capture_locks"):
            self._capture_locks: Dict[str, asyncio.Lock] = {}
        return self._capture_locks.setdefault(serial, asyncio.Lock())

    async def _capture_photo_adb_fallback(self, device_id: str, serial: str, lens: str):
        if not await self.adb.companion_installed(serial):
            await self.broadcast_to_dashboards({"type": "PHOTO_CAPTURED", "device_id": device_id, "payload": {
                "success": False, "error": COMPANION_MISSING, "filename": "", "local_path": "", "url": "",
                "device_id": device_id, "lens": lens}})
            return
        async with self._capture_lock(serial):
            self._adb_capture(device_id, 120)
            dest = await self.adb.capture_headless_photo_adb(serial, lens=lens, timeout=25.0)
            self._adb_capture(device_id, 6)
        ok = bool(dest and dest.exists())
        await self.broadcast_to_dashboards({
            "type": "PHOTO_CAPTURED",
            "device_id": device_id,
            "payload": {
                "success": ok,
                "error": None if ok else "The phone's camera did not deliver a photo within 25 s "
                                         "(camera in use by another app, or camera permission missing).",
                "filename": dest.name if ok else "",
                "local_path": str(dest) if ok else "",
                "url": download_url(dest) if ok else "",
                "device_id": device_id,
                "lens": lens
            }
        })

    async def _record_video_adb_fallback(self, device_id: str, serial: str, lens: str, duration_sec: int):
        if not await self.adb.companion_installed(serial):
            await self.broadcast_to_dashboards({"type": "VIDEO_RECORDED", "device_id": device_id, "payload": {
                "success": False, "error": COMPANION_MISSING, "filename": "", "local_path": "", "url": "",
                "device_id": device_id, "lens": lens, "duration_sec": duration_sec}})
            return
        if not hasattr(self, "_video_stop"):
            self._video_stop: Dict[str, asyncio.Event] = {}
        stop = asyncio.Event()
        started = time.time()
        async with self._capture_lock(serial):
            self._video_stop[device_id] = stop
            self._adb_capture(device_id, duration_sec + 120 if duration_sec else 7 * 3600)
            try:
                dest = await self.adb.record_headless_video_adb(serial, lens=lens, duration_sec=duration_sec, stop_event=stop)
            finally:
                if self._video_stop.get(device_id) is stop:
                    del self._video_stop[device_id]
                self._adb_capture(device_id, 6)
        if not duration_sec:
            duration_sec = max(1, int(time.time() - started) - 2)  # approximate length of an open-ended recording
        ok = bool(dest and dest.exists())
        await self.broadcast_to_dashboards({
            "type": "VIDEO_RECORDED",
            "device_id": device_id,
            "payload": {
                "success": ok,
                "error": None if ok else "The phone's camera did not deliver the video in time "
                                         "(camera in use by another app, or camera/microphone permission missing).",
                "filename": dest.name if ok else "",
                "local_path": str(dest) if ok else "",
                "url": download_url(dest) if ok else "",
                "device_id": device_id,
                "duration_sec": duration_sec,
                "lens": lens
            }
        })

    async def capture_headless_photo(self, device_id: str, lens: str = "back", timeout: float = 30.0) -> Dict[str, Any]:
        dev = self.device_manager.get_device(device_id)
        if not dev:
            return {"success": False, "error": f"Device {device_id} not found"}
        lens = "front" if lens == "front" else "back"
        on_usb = self._on_usb(dev)

        comp_sock = None if on_usb else self._get_companion_socket(dev)
        if comp_sock:
            fut = asyncio.get_event_loop().create_future()
            self.pending_photo_futures[dev.id] = fut
            try:
                await comp_sock.send_json({"type": "CAPTURE_PHOTO", "payload": {"lens": lens}})
                return await asyncio.wait_for(fut, timeout=timeout)
            except Exception as e:
                return {"success": False, "error": f"Remote photo failed: {e or 'timed out'}"}
            finally:
                self.pending_photo_futures.pop(dev.id, None)
        if not on_usb:
            return {"success": False, "error": f"{dev.name} is offline (not on USB and the companion app is not connected)"}
        if not await self.adb.companion_installed(dev.serial):
            return {"success": False, "error": COMPANION_MISSING}

        async with self._capture_lock(dev.serial):

            self._adb_capture(dev.id, 120)
            dest = await self.adb.capture_headless_photo_adb(dev.serial, lens=lens, timeout=timeout)
            self._adb_capture(dev.id, 6)
        if dest and dest.exists():
            return {
                "success": True,
                "filename": dest.name,
                "local_path": str(dest),
                "url": download_url(dest),
                "device_id": dev.id,
                "lens": lens
            }
        return {"success": False, "error": "Headless camera capture timed out"}

    async def record_headless_video(self, device_id: str, lens: str = "back", duration_sec: int = 5, timeout: Optional[float] = None) -> Dict[str, Any]:
        dev = self.device_manager.get_device(device_id)
        if not dev:
            return {"success": False, "error": f"Device {device_id} not found"}

        effective_timeout = max(timeout or 25.0, float(duration_sec) + 30.0)
        lens = "front" if lens == "front" else "back"
        on_usb = self._on_usb(dev)

        comp_sock = None if on_usb else self._get_companion_socket(dev)
        if comp_sock:
            fut = asyncio.get_event_loop().create_future()
            self.pending_video_futures[dev.id] = fut
            try:
                await comp_sock.send_json({"type": "RECORD_VIDEO", "payload": {"lens": lens, "duration_sec": duration_sec}})
                return await asyncio.wait_for(fut, timeout=effective_timeout)
            except Exception as e:
                return {"success": False, "error": f"Remote video failed: {e or 'timed out'}"}
            finally:
                self.pending_video_futures.pop(dev.id, None)
        if not on_usb:
            return {"success": False, "error": f"{dev.name} is offline (not on USB and the companion app is not connected)"}
        if not await self.adb.companion_installed(dev.serial):
            return {"success": False, "error": COMPANION_MISSING}

        async with self._capture_lock(dev.serial):

            self._adb_capture(dev.id, 120)
            dest = await self.adb.record_headless_video_adb(dev.serial, lens=lens, duration_sec=duration_sec, timeout=effective_timeout)
            self._adb_capture(dev.id, 6)
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
        return {"success": False, "error": "Headless video recording timed out"}
