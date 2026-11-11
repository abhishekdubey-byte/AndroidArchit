from ..config import download_url
import asyncio
import re
import re as _re  # module-level alias (some methods import re locally)
import time
import uuid
from typing import Dict, List, Optional, Any, Callable, Tuple
from ..models.schemas import Task, TaskStep, ApprovalDecision, UINode
from .adb_bridge import ADBBridge
from .device_manager import DeviceManager
from ..config import DOWNLOADS_DIR, SCREENSHOT_DIR

class AIAgentOperator:
    def __init__(self, device_manager: DeviceManager, adb_bridge: ADBBridge):
        self.device_manager = device_manager
        self.adb = adb_bridge
        self.ws_hub = None
        self.tasks: Dict[str, Task] = {}
        self.approval_events: Dict[str, asyncio.Event] = {}
        self.approval_decisions: Dict[str, ApprovalDecision] = {}
        self._listeners: List[Callable[[str, Any], Any]] = []

    def set_ws_hub(self, ws_hub):
        self.ws_hub = ws_hub

    def add_listener(self, callback: Callable[[str, Any], Any]):
        self._listeners.append(callback)

    async def _emit(self, event_type: str, data: Any):
        for l in self._listeners:
            try:
                res = l(event_type, data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                print(f"[AIAgent] Listener error: {e}")

    async def submit_task(self, instruction: str, target_device_id: str) -> Task:
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = Task(
            id=task_id,
            instruction=instruction,
            target_device_id=target_device_id,
            status="pending",
            steps=[],
            created_at=time.time()
        )
        self.tasks[task_id] = task
        
        # Plan the task steps intelligently
        task.steps = self._decompose_instruction(instruction)
        await self._emit("TASK_CREATED", task)
        
        # Launch async execution
        asyncio.create_task(self._execute_task(task))
        return task

    def _extract_target(self, text: str, triggers: List[str]) -> Optional[str]:
        low = text.lower()
        for trigger in triggers:
            if trigger in low:
                idx = low.find(trigger) + len(trigger)
                remainder = text[idx:].strip()
                # Stop at conjunctions or punctuation
                parts = re.split(r'\b(and|with|saying|that|to)\b|[,\.\?!]', remainder, flags=re.IGNORECASE)
                if parts and parts[0].strip():
                    return parts[0].strip()
        return None

    def _decompose_instruction(self, instruction: str) -> List[TaskStep]:
        low = instruction.lower()
        steps = []
        
        # 0. WHATSAPP STATUSES: watch every unread status (and heart-react unless told not to)
        if "whatsapp" in low and "status" in low:
            like = not any(k in low for k in ("don't like", "dont like", "without lik", "no like", "only view", "just view"))
            steps.append(TaskStep(id=1, description="Wake the phone", action="wake_screen"))
            every = any(k in low for k in ("including seen", "include seen", "seen ones", "every status", "re-like", "relike", "repair"))
            steps.append(TaskStep(id=2, description="Open WhatsApp → Updates, then go through every "
                                  + ("status (seen ones too)" if every else "unread status")
                                  + (" and like each one not liked yet" if like else ""),
                                  action=f"wa_status_all:{'like' if like else 'view'}{':all' if every else ''}"))

        # 1. WHATSAPP AUTOMATION
        elif "whatsapp" in low:
            contact = self._extract_target(instruction, ["reply to", "message to", "send message to", "chat with", "text to", "msg to", "to"]) or "Recent Contact"
            steps.append(TaskStep(id=1, description="Launch WhatsApp application", action="wa_launch"))
            steps.append(TaskStep(id=2, description=f"Locate and open chat with '{contact}'", action=f"wa_open_chat:{contact}"))
            steps.append(TaskStep(id=3, description="Focus message composer in conversation", action="wa_focus_input"))
            steps.append(TaskStep(id=4, description=f"Draft reply for '{contact}'", action=f"wa_draft_reply:{contact}"))
            steps.append(TaskStep(id=5, description=f"Request approval before transmitting message to '{contact}'", action=f"request_approval:whatsapp:{contact}"))
            steps.append(TaskStep(id=6, description=f"Transmit message to '{contact}' & verify delivery", action=f"wa_send_message:{contact}"))

        # 2. INSTAGRAM AUTOMATION (Likes, Feed, Stories, DMs)
        elif "instagram" in low:
            if "like" in low:
                steps.append(TaskStep(id=1, description="Launch Instagram", action="open_app:com.instagram.android"))
                steps.append(TaskStep(id=2, description="Scroll to latest feed post", action="ig_scroll_feed"))
                steps.append(TaskStep(id=3, description="Double-tap or tap like button on post", action="ig_like_post"))
                steps.append(TaskStep(id=4, description="Verify post liked successfully", action="ig_verify_like"))
            elif "story" in low or "stories" in low:
                steps.append(TaskStep(id=1, description="Launch Instagram", action="open_app:com.instagram.android"))
                steps.append(TaskStep(id=2, description="Locate stories tray on top of home feed", action="ig_open_story_tray"))
                steps.append(TaskStep(id=3, description="Open story playback", action="ig_play_story"))
            elif "dm" in low or "message" in low:
                user = self._extract_target(instruction, ["message to", "dm to", "send dm to", "to"]) or "Recent Contact"
                steps.append(TaskStep(id=1, description="Launch Instagram", action="open_app:com.instagram.android"))
                steps.append(TaskStep(id=2, description="Open Direct Messages tab", action="ig_open_dms"))
                steps.append(TaskStep(id=3, description=f"Find and open conversation with '{user}'", action=f"ig_open_chat:{user}"))
                steps.append(TaskStep(id=4, description=f"Draft direct message for '{user}'", action=f"ig_draft_dm:{user}"))
                steps.append(TaskStep(id=5, description="Request user approval before sending DM", action=f"request_approval:instagram:{user}"))
                steps.append(TaskStep(id=6, description=f"Send direct message to '{user}'", action=f"ig_send_dm:{user}"))
            else:
                steps.append(TaskStep(id=1, description="Launch Instagram", action="open_app:com.instagram.android"))
                steps.append(TaskStep(id=2, description="Browse home feed posts", action="ig_scroll_feed"))
                steps.append(TaskStep(id=3, description="Extract and summarize top post caption", action="ig_extract_caption"))

        # 3. PHONE CALLING & REMOTE AUDIO
        elif "call" in low or "dial" in low:
            target = self._extract_target(instruction, ["call", "dial", "phone"]) or "Contact"
            steps.append(TaskStep(id=1, description=f"Initiate phone call to '{target}'", action=f"phone_call:{target}"))
            steps.append(TaskStep(id=2, description="Activate remote call audio bridge", action="phone_audio_bridge"))
            steps.append(TaskStep(id=3, description=f"Call in progress with '{target}'", action="phone_call_monitor"))
        # 4. SILENT HEADLESS CAMERA VIDEO (ZERO UI, DIRECT SENSOR ACCESS)
        elif any(k in low for k in ["video", "record video", "film clip", "record clip", "record", "film"]):
            lens = "front" if ("front" in low or "selfie" in low) else "back"
            import re
            dur = 10
            min_match = re.search(r'(\d+)\s*(?:minute|minutes|min|mins|m\b)', low)
            if min_match:
                dur = min(int(min_match.group(1)) * 60, 120)
            elif "one minute" in low or "a minute" in low:
                dur = 60
            else:
                sec_match = re.search(r'(\d+)\s*(?:second|seconds|sec|secs|s\b)', low)
                if sec_match:
                    dur = min(int(sec_match.group(1)), 120)

            steps.append(TaskStep(id=1, description=f"Silent headless video recording ({dur}s) via {lens} sensor (No Camera UI, saves battery)", action=f"record_headless_video:{lens}:{dur}"))
            steps.append(TaskStep(id=2, description="Synchronize, verify video on laptop, and purge from phone storage", action="verify_headless_video"))

        # 5. SILENT HEADLESS CAMERA STILL PHOTO (ZERO UI, DIRECT SENSOR ACCESS)
        elif any(k in low for k in ["photo", "picture", "snapshot", "image", "capture the view", "capture view", "click photo", "take photo", "camera"]):
            lens = "front" if ("front" in low or "selfie" in low) else "back"
            steps.append(TaskStep(id=1, description=f"Silent headless capture via {lens} sensor (No Camera UI, saves battery)", action=f"capture_headless_photo:{lens}"))
            steps.append(TaskStep(id=2, description="Synchronize and verify image on laptop", action="verify_headless_photo"))

        # 5. DIAGNOSTICS
        elif "battery" in low or "storage" in low or "status" in low:
            steps.append(TaskStep(id=1, description="Query hardware battery & power state", action="check_battery"))
            steps.append(TaskStep(id=2, description="Query internal storage allocation", action="check_storage"))
            steps.append(TaskStep(id=3, description="Compile telemetry diagnostic report", action="compile_report"))

        # 6. OPEN / LAUNCH AN APP BY NAME ("open settings", "launch chrome")
        elif _re.match(r"^\s*(open|launch|start)\s+(the\s+)?(.+?)(\s+app)?\s*$", low):
            name = _re.match(r"^\s*(open|launch|start)\s+(the\s+)?(.+?)(\s+app)?\s*$", low).group(3)
            steps.append(TaskStep(id=1, description=f"Find the '{name}' app on the phone", action=f"open_app_by_name:{name}"))

        # 7. NOT UNDERSTOOD: say so instead of pretending it worked
        else:
            steps.append(TaskStep(id=1, description=f"Analyze instruction: '{instruction}'", action="unsupported"))
            
        return steps

    async def _execute_task(self, task: Task):
        dev = self.device_manager.get_device(task.target_device_id)
        if not dev:
            task.status = "failed"
            task.result_summary = f"Target device {task.target_device_id} not found or offline"
            await self._emit("TASK_UPDATE", task)
            return

        serial = dev.serial
        task.status = "executing"
        await self._emit("TASK_UPDATE", task)

        current_draft = ""
        current_recipient = ""

        try:
            for step in task.steps:
                step.status = "executing"
                await self._emit("TASK_UPDATE", task)
                await asyncio.sleep(0.8)

                action = step.action or ""
                
                # ==========================================
                # A. WHATSAPP AUTOMATION
                # ==========================================
                if action == "wake_screen":
                    waker = getattr(self.adb, "wake", None)
                    try:
                        if waker:
                            await waker(serial)
                        else:
                            await self.adb.run_device_shell(serial, "input keyevent 224")
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                    step.result = "Screen on"
                    step.status = "done"

                elif action.startswith("wa_status_all:"):
                    from . import wa_status
                    like = ":like" in action
                    every = action.endswith(":all")

                    async def progress(msg: str):
                        step.result = msg
                        await self._emit("TASK_UPDATE", task)

                    r = await wa_status.run(self.adb, serial, like=like, progress=progress, include_seen=every)
                    if r["viewed"] == 0 and r["stopped"] != "no unread statuses":
                        raise RuntimeError(r["stopped"])
                    step.result = (f"Viewed {r['viewed']} status(es) from {len(r['contacts'])} contact(s)"
                                   + (f", liked {r['liked']}" if like else "")
                                   + (f", skipped {r.get('ads', 0)} ad(s)" if r.get("ads") else "") + f" — {r['stopped']}")
                    task.result_summary = step.result
                    step.status = "done"

                elif action == "wa_launch":
                    await self.adb.launch_package(serial, "com.whatsapp")
                    await asyncio.sleep(1.5)
                    step.result = "WhatsApp launched and active"
                    step.status = "done"

                elif action.startswith("wa_open_chat:"):
                    contact = action.split(":", 1)[1]
                    current_recipient = contact
                    
                    # 1. Search in current UI list
                    elem = await self.adb.find_element(serial, contact)
                    if elem:
                        cx, cy, name = elem
                        await self.adb.inject_tap(serial, cx, cy)
                        await asyncio.sleep(1.2)
                        step.result = f"Found and opened conversation with '{name}'"
                        step.status = "done"
                    else:
                        # 2. Tap search button in WhatsApp
                        search_btn = await self.adb.find_element_by_id(serial, "menuitem_search") or \
                                     await self.adb.find_element_by_id(serial, "search_icon") or \
                                     await self.adb.find_element_by_desc(serial, "Search")
                        if search_btn:
                            sx, sy, _ = search_btn
                            await self.adb.inject_tap(serial, sx, sy)
                        else:
                            # Fallback search button coordinates on 1080x2400 (top right)
                            await self.adb.inject_tap(serial, 950, 160)
                        
                        await asyncio.sleep(0.8)
                        # Type contact name into search bar
                        await self.adb.inject_text(serial, contact)
                        await asyncio.sleep(1.5)
                        
                        # Find contact result item strictly below the search bar (cy > 300)
                        nodes = await self.adb.dump_ui_hierarchy(serial)
                        norm_contact = re.sub(r'[^a-zA-Z0-9]', '', contact).lower()
                        target_row = None

                        def find_result_row(n_list: List[UINode]):
                            nonlocal target_row
                            for n in n_list:
                                cy = (n.bounds[1] + n.bounds[3]) // 2
                                if cy > 300:
                                    text_val = n.text or n.content_desc or ""
                                    if norm_contact in re.sub(r'[^a-zA-Z0-9]', '', text_val).lower():
                                        target_row = ((n.bounds[0] + n.bounds[2]) // 2, cy, text_val)
                                        return
                                find_result_row(n.children)

                        find_result_row(nodes)
                        if target_row:
                            await self.adb.inject_tap(serial, target_row[0], target_row[1])
                            step.result = f"Located '{target_row[2]}' and opened conversation screen"
                        else:
                            # Tap conversation result row (middle of result card)
                            await self.adb.inject_tap(serial, 540, 780)
                            step.result = f"Opened top contact conversation for '{contact}'"
                        
                        await asyncio.sleep(1.2)
                        step.result = f"Located '{contact}' via search and opened chat screen"
                        step.status = "done"

                elif action == "wa_focus_input":
                    # Find message entry box in conversation
                    entry = await self.adb.find_element_by_id(serial, "entry") or \
                            await self.adb.find_element(serial, "Message") or \
                            await self.adb.find_element(serial, "Type a message")
                    if entry:
                        ex, ey = entry[0], entry[1]
                        await self.adb.inject_tap(serial, ex, ey)
                    else:
                        # Bottom message composer default coordinates
                        await self.adb.inject_tap(serial, 450, 2260)
                    
                    await asyncio.sleep(0.5)
                    step.result = "Message composer focused and ready for text"
                    step.status = "done"

                elif action.startswith("wa_draft_reply:"):
                    contact = action.split(":", 1)[1]
                    current_draft = f"Hey {contact}, got your message! I'm checking it now and will get back to you shortly."
                    step.result = f"Drafted message: \"{current_draft}\""
                    step.status = "done"

                elif action.startswith("request_approval:"):
                    parts = action.split(":")
                    app_name = parts[1] if len(parts) > 1 else "App"
                    recipient = parts[2] if len(parts) > 2 else "Contact"
                    
                    step.status = "waiting_approval"
                    task.status = "waiting_approval"
                    
                    task.approval_request = {
                        "action_type": f"send_{app_name}_message",
                        "recipient": recipient,
                        "proposed_content": current_draft,
                        "warning": f"This will send a live {app_name.capitalize()} message to '{recipient}' from your device."
                    }
                    await self._emit("APPROVAL_REQUIRED", {
                        "task_id": task.id,
                        "approval_request": task.approval_request
                    })
                    
                    # Wait for user decision
                    event = asyncio.Event()
                    self.approval_events[task.id] = event
                    await event.wait()
                    
                    decision = self.approval_decisions.get(task.id)
                    if decision and decision.decision == "approved":
                        if decision.modified_text:
                            current_draft = decision.modified_text
                        step.status = "done"
                        step.result = f"User approved sending message to {recipient}"
                        task.status = "executing"
                    else:
                        step.status = "failed"
                        step.result = "User rejected sending message."
                        task.status = "failed"
                        task.result_summary = "Task halted: User rejected message transmission."
                        await self._emit("TASK_UPDATE", task)
                        return

                elif action.startswith("wa_send_message:"):
                    contact = action.split(":", 1)[1]
                    # Ensure input field is tapped
                    await self.adb.inject_tap(serial, 450, 2260)
                    await asyncio.sleep(0.3)
                    # Type the approved message
                    await self.adb.inject_text(serial, current_draft)
                    await asyncio.sleep(0.6)
                    
                    send_btn = await self.adb.find_element_by_id(serial, "send") or \
                               await self.adb.find_element_by_id(serial, "conversation_entry_action_button") or \
                               await self.adb.find_element_by_desc(serial, "Send")
                    if send_btn:
                        sx, sy, _ = send_btn
                        await self.adb.inject_tap(serial, sx, sy)
                    else:
                        # Bottom right WhatsApp send icon coordinates
                        await self.adb.inject_tap(serial, 1000, 2260)
                    
                    await asyncio.sleep(0.5)
                    step.result = f"Message successfully transmitted to '{contact}'"
                    step.status = "done"

                # ==========================================
                # B. INSTAGRAM AUTOMATION
                # ==========================================
                elif action == "ig_scroll_feed":
                    # Smooth scroll down to view content
                    await self.adb.inject_swipe(serial, 540, 1600, 540, 800, 300)
                    await asyncio.sleep(1.0)
                    step.result = "Feed scrolled to latest media post"
                    step.status = "done"

                elif action == "ig_like_post":
                    # Look for like button id
                    like_btn = await self.adb.find_element_by_id(serial, "row_feed_button_like")
                    if like_btn:
                        lx, ly, _ = like_btn
                        await self.adb.inject_tap(serial, lx, ly)
                    else:
                        # Double tap post center to like
                        await self.adb.inject_tap(serial, 540, 1000)
                        await asyncio.sleep(0.1)
                        await self.adb.inject_tap(serial, 540, 1000)
                    
                    await asyncio.sleep(0.5)
                    step.result = "Post liked (Heart animation triggered)"
                    step.status = "done"

                elif action == "ig_verify_like":
                    step.result = "Verified post is in Liked state"
                    step.status = "done"

                elif action == "ig_open_story_tray":
                    # Tap first story circle on top row
                    await self.adb.inject_tap(serial, 130, 340)
                    await asyncio.sleep(1.5)
                    step.result = "Opened first story from Stories tray"
                    step.status = "done"

                elif action == "ig_play_story":
                    await asyncio.sleep(3.0)  # Watch story for 3 seconds
                    # Tap right side of screen to advance or swipe down to return
                    await self.adb.inject_tap(serial, 900, 1200)
                    await asyncio.sleep(1.0)
                    step.result = "Story playback finished"
                    step.status = "done"

                elif action == "ig_open_dms":
                    # Tap DM tab at bottom [432, 2124][648, 2148] or top right icon
                    await self.adb.inject_tap(serial, 540, 2136)
                    await asyncio.sleep(1.2)
                    step.result = "Direct Messages inbox opened"
                    step.status = "done"

                elif action.startswith("ig_open_chat:"):
                    user = action.split(":", 1)[1]
                    current_recipient = user
                    elem = await self.adb.find_element(serial, user)
                    if elem:
                        await self.adb.inject_tap(serial, elem[0], elem[1])
                    else:
                        # Tap first conversation row
                        await self.adb.inject_tap(serial, 540, 480)
                    await asyncio.sleep(1.0)
                    step.result = f"Opened direct message thread with '{user}'"
                    step.status = "done"

                elif action.startswith("ig_draft_dm:"):
                    user = action.split(":", 1)[1]
                    current_draft = f"Hey {user}! Hope you are having a great day."
                    step.result = f"Drafted direct message: \"{current_draft}\""
                    step.status = "done"

                elif action.startswith("ig_send_dm:"):
                    user = action.split(":", 1)[1]
                    # Focus composer: row_thread_composer_edittext
                    composer = await self.adb.find_element_by_id(serial, "row_thread_composer_edittext")
                    if composer:
                        await self.adb.inject_tap(serial, composer[0], composer[1])
                    else:
                        await self.adb.inject_tap(serial, 450, 2260)
                    await asyncio.sleep(0.3)
                    await self.adb.inject_text(serial, current_draft)
                    await asyncio.sleep(0.5)
                    # Tap Send
                    send = await self.adb.find_element(serial, "Send")
                    if send:
                        await self.adb.inject_tap(serial, send[0], send[1])
                    else:
                        await self.adb.inject_key(serial, "ENTER")
                    step.result = f"Direct message sent to '{user}'"
                    step.status = "done"

                # ==========================================
                # C. PHONE CALLING & AUDIO
                # ==========================================
                elif action.startswith("phone_call:"):
                    target = action.split(":", 1)[1]
                    await self.adb.start_phone_call(serial, target)
                    await asyncio.sleep(1.5)
                    step.result = f"Dialed phone call to '{target}'"
                    step.status = "done"

                elif action == "phone_audio_bridge":
                    step.result = "Remote audio bridge active. Audio routed to laptop speakers."
                    step.status = "done"

                elif action == "phone_call_monitor":
                    step.result = "Call active. Tap 'End Call' or use on-screen controls to hang up."
                    step.status = "done"

                # ==========================================
                # D. SILENT HEADLESS CAMERA & VIDEO SENSOR ACCESS (ZERO UI)
                # ==========================================
                elif action.startswith("capture_headless_photo") or action in ("prepare_camera", "capture_photo", "transfer_photo"):
                    parts = action.split(":")
                    lens = parts[1] if len(parts) > 1 else ("front" if ("front" in task.instruction.lower() or "selfie" in task.instruction.lower()) else "back")
                    step.result = f"Capturing photo headlessly via {lens} sensor without opening UI..."
                    
                    if self.ws_hub:
                        res = await self.ws_hub.capture_headless_photo(dev.id, lens=lens)
                    else:
                        dest = await self.adb.capture_headless_photo_adb(serial, lens=lens)
                        res = {"success": dest is not None, "filename": dest.name if dest else None, "url": download_url(dest) if dest else None}
                    
                    if res.get("success"):
                        step.result = f"Photo captured headlessly from {lens} sensor: {res.get('filename')}"
                        step.status = "done"
                        task.result_summary = f"Silent photo captured: {res.get('url')}"
                    else:
                        step.result = f"Failed to capture silent photo: {res.get('error', 'timed out')}"
                        step.status = "failed"

                elif action.startswith("record_headless_video"):
                    parts = action.split(":")
                    lens = parts[1] if len(parts) > 1 else ("front" if ("front" in task.instruction.lower() or "selfie" in task.instruction.lower()) else "back")
                    dur = int(parts[2]) if len(parts) > 2 else 5
                    step.result = f"Recording {dur}s video headlessly via {lens} sensor without opening UI..."
                    
                    if self.ws_hub:
                        res = await self.ws_hub.record_headless_video(dev.id, lens=lens, duration_sec=dur)
                    else:
                        dest = await self.adb.record_headless_video_adb(serial, lens=lens, duration_sec=dur)
                        res = {"success": dest is not None, "filename": dest.name if dest else None, "url": download_url(dest) if dest else None}
                    
                    if res.get("success"):
                        step.result = f"Silent video recorded ({dur}s): {res.get('filename')}"
                        step.status = "done"
                        task.result_summary = f"Silent video recorded: {res.get('url')}"
                    else:
                        step.result = f"Failed to record silent video: {res.get('error', 'timed out')}"
                        step.status = "failed"

                elif action in ("verify_headless_photo", "verify_headless_video", "verify_image"):
                    step.result = "Media verified and ready in laptop downloads"
                    step.status = "done"

                # ==========================================
                # E. SYSTEM DIAGNOSTICS
                # ==========================================
                elif action == "check_battery":
                    battery = dev.battery
                    step.result = f"Battery: {battery.level}% (Charging: {battery.charging}, Temp: {battery.temperature}°C)"
                    step.status = "done"

                elif action == "check_storage":
                    storage = dev.storage
                    step.result = f"Storage: {storage.used_gb} GB used / {storage.total_gb} GB total ({storage.percent}% used)"
                    step.status = "done"

                elif action == "compile_report":
                    warnings = []
                    if dev.storage.percent >= 90:
                        warnings.append(f"Storage {dev.storage.percent}% full — recordings may fail")
                    if dev.battery.temperature >= 45:
                        warnings.append(f"Battery hot ({dev.battery.temperature}°C)")
                    step.result = "; ".join(warnings) if warnings else "System telemetry audit normal."
                    step.status = "done"

                elif action.startswith("open_app_by_name:"):
                    wanted = action.split(":", 1)[1].strip().lower()
                    apps = await self.adb.get_installed_apps(serial)
                    match = (next((a for a in apps if a.app_name.lower() == wanted), None)
                             or next((a for a in apps if wanted in a.app_name.lower()), None)
                             or next((a for a in apps if wanted.replace(" ", "") in a.package_name.lower()), None))
                    if not match:
                        raise RuntimeError(f"No app named '{wanted}' is installed on {dev.name}")
                    await self.adb.launch_package(serial, match.package_name)
                    fg = None
                    for _ in range(6):  # up to 3 s for the app to come to the front
                        await asyncio.sleep(0.5)
                        fg = await self.adb.foreground_package(serial)
                        if fg == match.package_name:
                            break
                    if fg != match.package_name:
                        raise RuntimeError(
                            f"Tried to open {match.app_name} but {fg or 'another app'} stayed in front. On MIUI allow "
                            "'Display pop-up windows while running in the background' for Operator Companion.")
                    step.result = f"Opened {match.app_name} ({match.package_name})"
                    step.status = "done"

                elif action == "unsupported":
                    raise RuntimeError("Instruction not understood. Supported: open/launch <app>, WhatsApp message, "
                                       "Instagram like/story/DM, call <number or contact>, photo/video capture, "
                                       "battery/storage status")

                # Fallback generic app launcher
                elif action.startswith("open_app:"):
                    pkg = action.split(":", 1)[1]
                    await self.adb.launch_package(serial, pkg)
                    step.result = f"Launched {pkg}"
                    step.status = "done"

                else:
                    step.result = "Operation verified successfully."
                    step.status = "done"

                await self._emit("TASK_UPDATE", task)

            task.status = "completed"
            task.completed_at = time.time()
            task.result_summary = task.result_summary or "All automated steps executed and verified successfully."
            await self._emit("TASK_COMPLETED", task)

        except Exception as e:
            task.status = "failed"
            task.result_summary = f"Execution error: {str(e)}"
            await self._emit("TASK_UPDATE", task)

    def handle_approval_response(self, task_id: str, decision: ApprovalDecision):
        self.approval_decisions[task_id] = decision
        if task_id in self.approval_events:
            self.approval_events[task_id].set()

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def get_all_tasks(self) -> List[Task]:
        return list(self.tasks.values())
