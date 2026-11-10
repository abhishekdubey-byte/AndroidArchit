"""PowerBoosters Commander API (dashboard). Protected by the admin AccessGuard like every /api route."""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..services.pb_gateway import PBGateway
from ..services.powerboosters import PBError

INLINE_KINDS = {"image", "video", "audio"}


class MessageIn(BaseModel):
    text: str = ""
    client_id: str = ""
    reply_to: str = ""
    attachments: List[str] = []


class ForwardIn(BaseModel):
    device_ids: List[str]


class ReactIn(BaseModel):
    emoji: str = ""


class ReadIn(BaseModel):
    seq: int


class ReviewIn(BaseModel):
    action: str
    feedback: str = ""
    bonus_xp: int = 0


class BonusIn(BaseModel):
    device_id: str
    amount: int
    reason: str
    request_id: str = ""


class UploadInit(BaseModel):
    name: str
    mime: str = ""
    size: int
    sha256: str = ""
    purpose: str = "chat"
    upload_id: str = ""
    device_id: Optional[str] = None


def _reviewer(request: Request) -> str:
    host = request.client.host if request.client else "local"
    return f"Commander@{host}"


def create_pb_router(gw: PBGateway) -> APIRouter:
    r = APIRouter(prefix="/api/pb", tags=["powerboosters"])
    pb = gw.pb

    # ---------- squad ----------
    @r.get("/members")
    async def members():
        gw.registered_devices()
        out = []
        for m in pb.members():
            prof = pb.profile(m["device_id"])
            out.append({**m, "online": gw.online(m["device_id"]), "xp": prof["xp"], "level": prof["level"],
                        "completed_missions": prof["completed_missions"],
                        "badges": [a for a in prof["achievements"] if a["unlocked_at"]]})
        return {"members": out}

    @r.get("/members/{device_id}")
    async def member(device_id: str):
        return {**pb.profile(device_id), "online": gw.online(device_id)}

    @r.get("/leaderboard")
    async def leaderboard(period: str = "global"):
        return {"period": period, "rows": pb.leaderboard(period)}

    # ---------- chat ----------
    @r.get("/conversations")
    async def conversations():
        gw.registered_devices()
        return {"conversations": [{**c, "online": gw.online(c["device_id"])} for c in pb.conversations()]}

    @r.get("/conversations/{device_id}/messages")
    async def messages(device_id: str, after: int = 0, q: str = ""):
        return {"messages": pb.messages(device_id, after, 500, q), "conversation": pb.conversation_for(device_id)}

    @r.post("/conversations/{device_id}/messages")
    async def send(device_id: str, body: MessageIn):
        m = pb.send_message(device_id, "commander", body.text, body.client_id, body.reply_to, body.attachments)
        await gw.commander_message(device_id, m)
        return m

    @r.post("/conversations/{device_id}/read")
    async def read(device_id: str, body: ReadIn):
        conv = pb.mark_read(device_id, "commander", body.seq)
        await gw.to_device(device_id, {"type": "PB_RECEIPT", "payload": conv})
        return conv

    @r.post("/conversations/{device_id}/typing")
    async def typing(device_id: str, typing: bool = True):
        await gw.to_device(device_id, {"type": "PB_TYPING", "payload": {"typing": typing}})
        return {"ok": True}

    @r.post("/messages/{message_id}/react")
    async def react(message_id: str, body: ReactIn):
        m = pb.message(message_id)
        m = pb.react(m["device_id"], "commander", message_id, body.emoji)
        await gw.to_device(m["device_id"], {"type": "PB_MSG", "payload": m})
        return m

    @r.post("/messages/{message_id}/forward")
    async def forward(message_id: str, body: ForwardIn):
        src = pb.message(message_id)
        out = []
        for d in body.device_ids[:50]:
            m = pb.send_message(d, "commander", src["text"] or "", "", "", [a["id"] for a in src["attachments"]], forwarded=True)
            await gw.commander_message(d, m)
            out.append(m)
        return {"messages": out}

    # ---------- files ----------
    @r.post("/files")
    async def upload_init(body: UploadInit):
        f = pb.upload_init(uploader="commander", owner_device=body.device_id, purpose=body.purpose, name=body.name,
                           mime=body.mime, size=body.size, sha256=body.sha256, upload_id=body.upload_id,
                           max_size=2048 * 1024 * 1024)
        return gw._status(body.upload_id, f)

    @r.put("/files/{file_id}")
    async def upload_chunk(file_id: str, request: Request, offset: int = 0):
        data = await request.body()
        if len(data) > 16 * 1024 * 1024:
            raise PBError("Chunk too large", 413)
        return gw._status("", pb.upload_chunk(file_id, "commander", offset, data))

    @r.get("/files/{file_id}")
    async def download(file_id: str, download: bool = False):
        f = pb.file(file_id)
        if f["status"] != "ready":
            raise PBError("File is still uploading", 409)
        inline = not download and (f["kind"] in INLINE_KINDS or f["mime"] == "application/pdf")
        # anything that is not media/pdf is always a download with a neutral type (no HTML/script rendering)
        media = f["mime"] if inline else "application/octet-stream"
        return FileResponse(f["path"], media_type=media, filename=f["name"],
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

    # ---------- missions ----------
    @r.get("/missions")
    async def missions():
        return {"missions": pb.missions()}

    @r.get("/missions/{mission_id}")
    async def mission(mission_id: str):
        return pb.mission(mission_id)

    @r.post("/missions")
    async def create(body: Dict[str, Any]):
        m = pb.save_mission(body)
        if body.get("publish"):
            pb.publish(m["id"], gw.registered_devices())
            await gw.after_publish(m["id"])
        return pb.mission(m["id"])

    @r.put("/missions/{mission_id}")
    async def update(mission_id: str, body: Dict[str, Any]):
        return pb.save_mission(body, mission_id)

    @r.post("/missions/{mission_id}/publish")
    async def publish(mission_id: str):
        pb.publish(mission_id, gw.registered_devices())
        await gw.after_publish(mission_id)
        return pb.mission(mission_id)

    @r.delete("/missions/{mission_id}")
    async def delete(mission_id: str):
        pb.delete_draft(mission_id)
        return {"ok": True}

    # ---------- verification centre ----------
    @r.get("/reviews")
    async def reviews(status: str = "", device_id: str = "", mission_id: str = "", priority: str = "",
                      attempt: Optional[int] = None, since: Optional[float] = None, until: Optional[float] = None):
        return {"submissions": pb.review_queue(status, device_id, mission_id, priority, attempt, since, until)}

    @r.get("/reviews/{submission_id}")
    async def review_detail(submission_id: str):
        return pb.review_detail(submission_id)

    @r.post("/reviews/{submission_id}")
    async def review(submission_id: str, body: ReviewIn, request: Request):
        res = pb.review(submission_id, body.action, _reviewer(request), body.feedback, body.bonus_xp)
        if not res.get("duplicate"):
            await gw.after_review(res)
        return res

    # ---------- rewards ----------
    @r.get("/ledger")
    async def ledger(device_id: str = ""):
        return {"transactions": pb.ledger(device_id or None)}

    @r.post("/bonus")
    async def bonus(body: BonusIn, request: Request):
        res = pb.manual_bonus(body.device_id, body.amount, body.reason, _reviewer(request), body.request_id)
        if res["transaction"]:
            await gw.notify_device(body.device_id, "xp", f"⭐ +{body.amount} XP bonus", body.reason)
            await gw.to_device(body.device_id, {"type": "PB_REWARD", "payload": {"title": body.reason, "xp": body.amount,
                                                                                 "unlocked": res["unlocked"]}})
            await gw.push_profile(body.device_id)
        return res

    @r.get("/policy")
    async def policy():
        return pb.policy()

    @r.put("/policy")
    async def set_policy(body: Dict[str, Any]):
        return pb.set_policy(body)

    # ---------- notifications ----------
    @r.get("/notifications")
    async def notifications():
        return {"notifications": pb.notifications("commander")}

    @r.post("/notifications/read")
    async def notifications_read():
        pb.mark_notifications_read("commander")
        return {"ok": True}

    @r.get("/events")
    async def events(device_id: str = ""):
        if device_id:
            return {"events": pb._q("SELECT * FROM pb_device_events WHERE device_id=? ORDER BY seq DESC LIMIT 300", (device_id,))}
        return {"events": pb._q("SELECT * FROM pb_device_events ORDER BY seq DESC LIMIT 300")}

    return r
