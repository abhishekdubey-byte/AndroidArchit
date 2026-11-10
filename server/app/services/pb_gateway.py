"""PowerBoosters transport: phone messages on the existing /ws/device socket, live events to dashboards.

Every phone handler receives `device_id` = the relay identity the socket authenticated with (set by the
hub), never a value from the message, so a phone can only read and act on its own data.
"""
import asyncio
import time
from typing import Any, Dict, Optional

from .powerboosters import PBError, PowerBoosters
from .relay import pack, unpack

PHONE_TYPES = {"PB_SYNC", "PB_MSG_SEND", "PB_TYPING", "PB_READ", "PB_REACT", "PB_ACCEPT", "PB_SUBMIT",
               "PB_UPLOAD_INIT", "PB_FILE_PULL", "PB_LOG", "PB_NOTIF_READ", "PB_SET_USERNAME"}
PULL_CHUNK = 256 * 1024


class PBGateway:
    def __init__(self, store: PowerBoosters, hub, device_manager, relays):
        self.pb = store
        self.hub = hub
        self.dm = device_manager
        self.relays = relays

    # ---------- identity / presence ----------
    def device_name(self, device_id: str) -> str:
        for d in self.dm.get_all_devices():
            if device_id in (d.relay_id, d.id):
                return d.name
        return ""

    def online(self, device_id: str) -> bool:
        return self.relays.get(device_id) is not None

    def registered_devices(self):
        """Every phone known at this moment: PowerBoosters members plus all bound companion apps."""
        ids = {m["device_id"] for m in self.pb.members()} | set(self.relays.bindings.keys()) | set(self.relays.clients.keys())
        for i in ids:
            self.pb.ensure_member(i, self.device_name(i))
        return sorted(ids)

    # ---------- push ----------
    async def to_device(self, device_id: str, msg: Dict[str, Any]) -> bool:
        sock = self.hub.device_clients.get(device_id)
        if not sock:
            return False  # offline: everything is in SQLite and arrives with the next PB_SYNC
        try:
            await asyncio.wait_for(sock.send_json(msg), 5)
            return True
        except Exception:
            return False

    async def to_dashboards(self, event: str, data: Any):
        await self.hub.broadcast_to_dashboards({"type": "PB_EVENT", "event": event, "data": data})

    async def notify_device(self, device_id: str, kind: str, title: str, body: str = "", ref: str = ""):
        n = self.pb.notify(device_id, kind, title, body, ref)
        await self.to_device(device_id, {"type": "PB_NOTIFY", "payload": n})

    async def notify_commander(self, kind: str, title: str, body: str = "", ref: str = ""):
        n = self.pb.notify("commander", kind, title, body, ref)
        await self.to_dashboards("notification", n)

    async def on_connect(self, device_id: str):
        self.pb.ensure_member(device_id, self.device_name(device_id))
        self.pb.log_event(device_id, "connected")
        await self.to_dashboards("presence", {"device_id": device_id, "online": True})

    async def on_disconnect(self, device_id: str):
        try:
            self.pb.log_event(device_id, "disconnected")
            await self.to_dashboards("presence", {"device_id": device_id, "online": False})
        except Exception:
            pass

    async def push_state(self, device_id: str):
        await self.to_device(device_id, {"type": "PB_STATE", "payload": self.pb.device_state(device_id)})

    # ---------- phone → laptop ----------
    async def handle_phone(self, ws, device_id: str, msg: Dict[str, Any]):
        t = msg.get("type")
        p = msg.get("payload") or {}
        try:
            await self._handle(ws, device_id, t, p)
        except PBError as e:
            await ws.send_json({"type": "PB_ERROR", "payload": {"ref": p.get("client_id") or p.get("upload_id") or "",
                                                                 "op": t, "message": e.message, "status": e.status}})
            if t in ("PB_UPLOAD_INIT",):
                await self.notify_commander("upload_failed", f"Upload failed on {self.device_name(device_id) or device_id}", e.message)

    async def _handle(self, ws, device_id: str, t: str, p: Dict[str, Any]):
        pb = self.pb
        if t == "PB_SYNC":
            pb.ensure_member(device_id, self.device_name(device_id) or p.get("name", ""))
            state = pb.device_state(device_id, int(p.get("msg_after", 0) or 0), int(p.get("notif_after", 0) or 0))
            pb.mark_delivered(device_id, [a["id"] for a in state["assignments"]])
            if state["messages"]:
                pb.mark_read(device_id, "member", state["messages"][-1]["seq"], delivered_only=True)
                await self.to_dashboards("receipt", {"device_id": device_id, **pb.conversation_for(device_id)})
            await ws.send_json({"type": "PB_STATE", "payload": state})

        elif t == "PB_MSG_SEND":
            m = pb.send_message(device_id, "member", p.get("text", ""), p.get("client_id", ""), p.get("reply_to", ""),
                                p.get("attachments") or [])
            await ws.send_json({"type": "PB_MSG", "payload": m})
            await self.to_dashboards("message", m)
            name = self.device_name(device_id) or device_id
            await self.notify_commander("message", f"💬 {name}", (m["text"] or "📎 attachment")[:140], device_id)

        elif t == "PB_TYPING":
            await self.to_dashboards("typing", {"device_id": device_id, "typing": bool(p.get("typing"))})

        elif t == "PB_READ":
            conv = pb.mark_read(device_id, "member", int(p.get("seq", 0)), bool(p.get("delivered_only")))
            await self.to_dashboards("receipt", {"device_id": device_id, **conv})

        elif t == "PB_REACT":
            m = pb.react(device_id, "member", p.get("message_id", ""), p.get("emoji", ""))
            await ws.send_json({"type": "PB_MSG", "payload": m})
            await self.to_dashboards("message", m)

        elif t == "PB_ACCEPT":
            a = pb.accept(device_id, p.get("assignment_id", ""))
            await ws.send_json({"type": "PB_ASSIGNMENT", "payload": a})
            await self.to_dashboards("assignment", {"device_id": device_id, "assignment_id": a["id"], "status": a["status"],
                                                    "mission_id": a["mission_id"]})
            await self.notify_commander("mission_accepted", f"🎯 {self.device_name(device_id) or device_id} accepted a mission",
                                        a["mission"]["title"], a["mission_id"])

        elif t == "PB_SUBMIT":
            before = pb._one("SELECT id FROM pb_submissions WHERE device_id=? AND client_id=?", (device_id, p.get("client_id", "")))
            s = pb.submit(device_id, p.get("assignment_id", ""), p.get("client_id", ""), p.get("text", ""),
                          p.get("comment", ""), p.get("files") or [])
            a = pb.assignment(s["assignment_id"], device_id)
            await ws.send_json({"type": "PB_SUBMIT_OK", "payload": {"client_id": p.get("client_id", ""), "submission": s, "assignment": a}})
            if not before:  # a retried submit (lost ack) must not notify twice
                await self.to_dashboards("submission", {"device_id": device_id, "submission_id": s["id"], "attempt": s["attempt"]})
                name = self.device_name(device_id) or device_id
                kind = "resubmission" if s["attempt"] > 1 else "submission"
                await self.notify_commander(kind, f"📥 {name}: {'resubmission' if s['attempt'] > 1 else 'new submission'} (attempt {s['attempt']})",
                                            f"{a['mission']['title']} · {len(s['attachments'])} attachment(s)", s["id"])

        elif t == "PB_UPLOAD_INIT":
            purpose = p.get("purpose", "chat")
            max_size = 200 * 1024 * 1024
            allowed = None
            if purpose == "submission":
                a = pb.assignment(p.get("assignment_id", ""), device_id)
                max_size = a["mission"]["max_attachment_mb"] * 1024 * 1024
                allowed = a["mission"]["allowed_formats"] or None
            f = pb.upload_init(uploader=device_id, owner_device=device_id, purpose=purpose, name=p.get("name", ""),
                               mime=p.get("mime", ""), size=int(p.get("size", 0)), sha256=p.get("sha256", ""),
                               upload_id=p.get("upload_id", ""), max_size=max_size, allowed_ext=allowed)
            await ws.send_json({"type": "PB_UPLOAD_STATUS", "payload": self._status(p.get("upload_id", ""), f)})

        elif t == "PB_FILE_PULL":
            fid = p.get("file_id", "")
            if not pb.can_device_read(device_id, fid):
                raise PBError("File not available", 404)
            f = pb.file(fid)
            off = max(0, int(p.get("offset", 0)))
            data = pb.read_chunk(fid, off, PULL_CHUNK)
            eof = off + len(data) >= f["size"]
            await ws.send_bytes(pack({"type": "PB_FILE_CHUNK", "file_id": fid, "offset": off, "size": f["size"], "eof": eof}, data))

        elif t == "PB_SET_USERNAME":
            pb.ensure_member(device_id, self.device_name(device_id))
            prof = pb.set_username(device_id, p.get("username", ""))
            await ws.send_json({"type": "PB_USERNAME_OK", "payload": {"username": prof["username"]}})
            await self.push_profile(device_id)
            await self.to_dashboards("presence", {"device_id": device_id, "online": True})

        elif t == "PB_NOTIF_READ":
            pb.mark_notifications_read(device_id)

        elif t == "PB_LOG":
            detail = {"level": str(p.get("level", "info"))[:10], "message": str(p.get("message", ""))[:2000]}
            pb.log_event(device_id, "app_log", detail)
            if detail["level"] in ("error", "warn"):
                self.dm.add_diagnostic_report({"device_id": device_id, "timestamp": time.time() * 1000, "module": "powerboosters",
                                               "operation": str(p.get("op", "ui"))[:60], "error_code": "PB_" + detail["level"].upper(),
                                               "description": detail["message"], "suggested_resolution": "See PowerBoosters device events",
                                               "device_info": {}, "stack_trace": None})

    async def handle_chunk(self, ws, device_id: str, frame: bytes) -> bool:
        """Binary PB_CHUNK frames from a phone. Returns False when the frame is not ours."""
        if len(frame) < 2:
            return False
        try:
            header, data = unpack(frame)
        except Exception:
            return False
        if header.get("type") != "PB_CHUNK":
            return False
        try:
            f = self.pb.upload_chunk(header.get("file_id", ""), device_id, int(header.get("offset", 0)), data)
            await ws.send_json({"type": "PB_UPLOAD_STATUS", "payload": self._status(header.get("upload_id", ""), f)})
        except PBError as e:
            await ws.send_json({"type": "PB_UPLOAD_STATUS", "payload": {"upload_id": header.get("upload_id", ""),
                                                                         "file_id": header.get("file_id", ""), "error": e.message,
                                                                         "received": 0, "status": "error"}})
            await self.notify_commander("upload_failed", f"Upload problem on {self.device_name(device_id) or device_id}", e.message)
        return True

    @staticmethod
    def _status(upload_id: str, f: Dict[str, Any]) -> Dict[str, Any]:
        return {"upload_id": upload_id, "file_id": f["id"], "received": f["received"], "size": f["size"],
                "status": f["status"], "file": {k: f[k] for k in ("id", "name", "mime", "kind", "size", "sha256", "status")}}

    # ---------- commander actions that reach phones ----------
    async def after_publish(self, mission_id: str):
        m = self.pb.mission(mission_id)
        for a in m["assignments"]:
            await self.notify_device(a["device_id"], "mission", f"🚀 New mission: {m['title']}",
                                     f"{m['priority'].upper()} · +{m['xp_reward']} XP", a["id"])
            if await self.to_device(a["device_id"], {"type": "PB_ASSIGNMENT", "payload": self.pb.assignment(a["id"])}):
                self.pb.mark_delivered(a["device_id"], [a["id"]])
        await self.to_dashboards("mission", {"mission_id": mission_id})

    async def after_review(self, result: Dict[str, Any]):
        did = result["device_id"]
        s = result["submission"]
        title = result["mission"]["title"]
        a = self.pb.assignment(s["assignment_id"])
        await self.to_device(did, {"type": "PB_ASSIGNMENT", "payload": a})
        if result["action"] == "approve":
            if result["transactions"]:
                await self.notify_device(did, "approved", f"✅ Mission completed: {title}", f"+{result['xp_total']} XP earned", a["id"])
                await self.to_device(did, {"type": "PB_REWARD", "payload": {"title": title, "xp": result["xp_total"],
                                                                            "unlocked": result["unlocked"]}})
            for b in result["unlocked"]:
                await self.notify_device(did, "achievement", f"{b['icon']} Achievement unlocked: {b['name']}", b["description"])
        elif result["action"] == "reject":
            await self.notify_device(did, "rejected", f"❌ Mission not approved: {title}", result["feedback"], a["id"])
        else:
            await self.notify_device(did, "revision", f"✏️ Revision requested: {title}", result["feedback"], a["id"])
        await self.push_profile(did)
        await self.to_dashboards("review", {"submission_id": s["id"], "device_id": did, "action": result["action"]})

    async def push_profile(self, device_id: str):
        await self.to_device(device_id, {"type": "PB_PROFILE", "payload": {
            "profile": self.pb.profile(device_id),
            "leaderboard": {p: self.pb.leaderboard(p, 25, device_id) for p in ("global", "weekly", "monthly")}}})

    async def commander_message(self, device_id: str, m: Dict[str, Any]):
        if await self.to_device(device_id, {"type": "PB_MSG", "payload": m}):
            pass
        n = self.pb.notify(device_id, "message", "💬 Commander", (m["text"] or "📎 attachment")[:140], m["id"])
        await self.to_device(device_id, {"type": "PB_NOTIFY", "payload": n})
        await self.to_dashboards("message", m)
