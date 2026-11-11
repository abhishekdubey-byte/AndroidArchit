"""PowerBoosters: missions, verification, XP ledger and squad chat.

Independent of the AI Task Operator. Phones are identified only by the relay identity they
authenticated with on /ws/device (hub passes it in); the laptop admin is the Commander.
All state lives in SQLite (storage/powerboosters.db); files under storage/uploads/powerboosters/.
"""
import hashlib
import json
import mimetypes
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import STORAGE_DIR, UPLOADS_DIR

DB_FILE = STORAGE_DIR / "powerboosters.db"
FILES_DIR = UPLOADS_DIR / "powerboosters"

PRIORITIES = ("low", "normal", "high", "critical")
MAX_CHAT_FILE = 200 * 1024 * 1024
ALLOWED_KINDS = ("image", "video", "audio", "document")
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "zip", "rtf", "odt", "json", "md"}

ACHIEVEMENTS = [
    ("first_mission", "First Mission", "🎯", "Complete your first verified mission"),
    ("perfect_submission", "Perfect Submission", "💎", "Get a mission approved on the first attempt"),
    ("consistent_contributor", "Consistent Contributor", "🔥", "Complete 5 verified missions"),
    ("mission_master", "Mission Master", "🏆", "Complete 10 verified missions"),
    ("communication_expert", "Communication Expert", "💬", "Send 50 messages to the Commander"),
    ("squad_champion", "Squad Champion", "👑", "Reach #1 on the global leaderboard"),
    ("elite_operator", "Elite Operator", "⚡", "Reach level 10"),
]

DEFAULT_POLICY = {"mission_approved": 100, "bonus_quality": 25, "first_try_bonus": 0, "level_base": 100, "demo_squad": 1}
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{2,19}")
DEMO_COUNT = 370


def _demo_players():
    """Deterministic demo squad (flagged is_demo everywhere; never in the XP ledger)."""
    import random
    rnd = random.Random(370)
    first = ["arjun", "rohan", "vikram", "aditya", "karan", "rahul", "ishaan", "kabir", "dev", "aarav", "vivaan", "reyansh",
             "ananya", "diya", "priya", "sneha", "kavya", "isha", "meera", "riya", "tanvi", "nisha", "pooja", "aditi",
             "sam", "alex", "max", "leo", "zara", "maya", "noah", "liam", "ethan", "lucas", "mia", "emma", "sara", "nina",
             "omar", "ali", "yusuf", "kenji", "hiro", "sora", "luca", "marco", "ivan", "nikolai", "pablo", "diego"]
    tags = ["ninja", "blaze", "storm", "shadow", "raider", "hawk", "viper", "titan", "nova", "pixel", "ghost", "falcon",
            "rocket", "phoenix", "wolf", "striker", "ace", "sniper", "rogue", "bolt", "frost", "cyber", "drift", "reaper"]
    seen, out = set(), []
    while len(out) < DEMO_COUNT:
        style = rnd.random()
        if style < 0.35:
            n = f"{rnd.choice(first)}_{rnd.choice(tags)}"
        elif style < 0.65:
            n = f"{rnd.choice(first)}{rnd.randint(7, 2009)}"
        elif style < 0.85:
            n = f"{rnd.choice(tags)}.{rnd.choice(first)}"
        else:
            n = f"{rnd.choice(first).capitalize()}{rnd.choice(tags).capitalize()}{rnd.randint(1, 99)}"
        n = n[:20]
        if n.lower() in seen:
            continue
        seen.add(n.lower())
        xp = int(rnd.paretovariate(1.6) * 60) // 5 * 5
        xp = min(xp, 9800)
        month = min(xp, int(xp * rnd.uniform(0.1, 0.6)) // 5 * 5)
        week = min(month, int(month * rnd.uniform(0.1, 0.5)) // 5 * 5)
        out.append((n, xp, week, month, max(0, xp // 110 + rnd.randint(-1, 1))))
    return out

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS pb_members (
  device_id TEXT PRIMARY KEY, member_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  created_at REAL NOT NULL, last_seen REAL);
CREATE TABLE IF NOT EXISTS pb_files (
  id TEXT PRIMARY KEY, owner_device TEXT, uploaded_by TEXT NOT NULL, purpose TEXT NOT NULL,
  name TEXT NOT NULL, mime TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT, received INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'uploading', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pb_files_owner ON pb_files(owner_device, sha256);
CREATE TABLE IF NOT EXISTS pb_missions (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, instructions TEXT,
  distribution TEXT NOT NULL CHECK(distribution IN ('broadcast','targeted')),
  targets TEXT NOT NULL DEFAULT '[]', priority TEXT NOT NULL DEFAULT 'normal',
  xp_reward INTEGER NOT NULL DEFAULT 100, bonus_xp INTEGER NOT NULL DEFAULT 0, deadline REAL,
  deliverables TEXT, allowed_formats TEXT, max_attachment_mb INTEGER NOT NULL DEFAULT 50,
  submission_instructions TEXT, max_attempts INTEGER NOT NULL DEFAULT 3,
  reference_files TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft', created_at REAL NOT NULL, published_at REAL);
CREATE TABLE IF NOT EXISTS pb_assignments (
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES pb_missions(id),
  device_id TEXT NOT NULL REFERENCES pb_members(device_id),
  status TEXT NOT NULL DEFAULT 'assigned', attempts_used INTEGER NOT NULL DEFAULT 0,
  delivered_at REAL, accepted_at REAL, completed_at REAL, updated_at REAL NOT NULL,
  UNIQUE(mission_id, device_id));
CREATE INDEX IF NOT EXISTS ix_pb_assign_dev ON pb_assignments(device_id);
CREATE TABLE IF NOT EXISTS pb_submissions (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, assignment_id TEXT NOT NULL REFERENCES pb_assignments(id),
  device_id TEXT NOT NULL, attempt INTEGER NOT NULL, text TEXT, comment TEXT,
  status TEXT NOT NULL DEFAULT 'pending', submitted_at REAL NOT NULL,
  UNIQUE(device_id, client_id), UNIQUE(assignment_id, attempt));
CREATE INDEX IF NOT EXISTS ix_pb_sub_status ON pb_submissions(status);
CREATE TABLE IF NOT EXISTS pb_submission_attachments (
  submission_id TEXT NOT NULL REFERENCES pb_submissions(id), file_id TEXT NOT NULL REFERENCES pb_files(id),
  PRIMARY KEY(submission_id, file_id));
CREATE TABLE IF NOT EXISTS pb_reviews (
  id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES pb_submissions(id),
  action TEXT NOT NULL CHECK(action IN ('approve','reject','revision')), feedback TEXT,
  reviewer TEXT NOT NULL, bonus_xp INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pb_reward_transactions (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, member_id TEXT NOT NULL, mission_id TEXT,
  assignment_id TEXT, submission_id TEXT, amount INTEGER NOT NULL, reason TEXT NOT NULL,
  approval_ref TEXT, tx_type TEXT NOT NULL, idem_key TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pb_tx_dev ON pb_reward_transactions(device_id, created_at);
CREATE TABLE IF NOT EXISTS pb_achievements (key TEXT PRIMARY KEY, name TEXT, icon TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS pb_member_achievements (
  device_id TEXT NOT NULL, achievement TEXT NOT NULL REFERENCES pb_achievements(key),
  unlocked_at REAL NOT NULL, PRIMARY KEY(device_id, achievement));
CREATE TABLE IF NOT EXISTS pb_conversations (
  id TEXT PRIMARY KEY, device_id TEXT UNIQUE NOT NULL, admin_read_seq INTEGER NOT NULL DEFAULT 0,
  member_read_seq INTEGER NOT NULL DEFAULT 0, member_delivered_seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pb_messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  conversation_id TEXT NOT NULL REFERENCES pb_conversations(id), sender TEXT NOT NULL,
  client_id TEXT, text TEXT, reply_to TEXT, forwarded INTEGER NOT NULL DEFAULT 0,
  reactions TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, UNIQUE(conversation_id, sender, client_id));
CREATE TABLE IF NOT EXISTS pb_message_attachments (
  message_id TEXT NOT NULL REFERENCES pb_messages(id), file_id TEXT NOT NULL REFERENCES pb_files(id),
  PRIMARY KEY(message_id, file_id));
CREATE TABLE IF NOT EXISTS pb_notifications (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, audience TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT, ref TEXT, read INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pb_notif ON pb_notifications(audience, seq);
CREATE TABLE IF NOT EXISTS pb_device_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, kind TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pb_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pb_demo_players (
  username TEXT PRIMARY KEY, xp INTEGER NOT NULL, week_xp INTEGER NOT NULL, month_xp INTEGER NOT NULL, completed INTEGER NOT NULL);
"""


class PBError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _safe(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (name or "file").strip())[:120].strip(". ") or "file"
    return name


def kind_of(mime: str, name: str) -> str:
    m = (mime or "").lower()
    for k in ("image", "video", "audio"):
        if m.startswith(k + "/"):
            return k
    return "document"


def level_for(xp: int, base: int = 100) -> Dict[str, int]:
    """Level L needs base*L*(L-1)/2 total XP (0, 100, 300, 600, …)."""
    lvl = 1
    while xp >= base * (lvl + 1) * lvl // 2:
        lvl += 1
    cur = base * lvl * (lvl - 1) // 2
    nxt = base * (lvl + 1) * lvl // 2
    return {"level": lvl, "level_floor": cur, "next_level_xp": nxt, "xp_to_next": nxt - xp}


class PowerBoosters:
    def close(self) -> None:
        """Release the SQLite handle (Windows cannot delete or move an open database file). Idempotent."""
        with self._lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    def __init__(self, db_file: Path = DB_FILE, files_dir: Path = FILES_DIR):
        self.files_dir = Path(files_dir)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(db_file), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(pb_members)")]
        if "username" not in cols:
            self.db.execute("ALTER TABLE pb_members ADD COLUMN username TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_pb_username ON pb_members(lower(username))")
        if not self.db.execute("SELECT 1 FROM pb_demo_players LIMIT 1").fetchone():
            self.db.executemany("INSERT OR IGNORE INTO pb_demo_players VALUES (?,?,?,?,?)", _demo_players())
        for a in ACHIEVEMENTS:
            self.db.execute("INSERT OR IGNORE INTO pb_achievements VALUES (?,?,?,?)", a)
        # an upload interrupted by a server restart resumes from what is really on disk
        for r in self.db.execute("SELECT id, path FROM pb_files WHERE status='uploading'").fetchall():
            p = Path(r["path"])
            self.db.execute("UPDATE pb_files SET received=? WHERE id=?", (p.stat().st_size if p.exists() else 0, r["id"]))

    # ---------------- helpers ----------------
    def _tx(self):
        db = self.db
        lock = self._lock

        class Tx:
            def __enter__(self_):
                lock.acquire()
                db.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_, et, ev, tb):
                try:
                    db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    lock.release()
                return False
        return Tx()

    def _q(self, sql: str, args=()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> Optional[Dict[str, Any]]:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def policy(self) -> Dict[str, int]:
        row = self._one("SELECT value FROM pb_settings WHERE key='reward_policy'")
        p = dict(DEFAULT_POLICY)
        if row:
            p.update(json.loads(row["value"]))
        return p

    def set_policy(self, values: Dict[str, Any]) -> Dict[str, int]:
        p = self.policy()
        for k in DEFAULT_POLICY:
            if k in values:
                p[k] = max(0, min(int(values[k]), 100000))
        p["level_base"] = max(10, p["level_base"])
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO pb_settings VALUES ('reward_policy', ?)", (json.dumps(p),))
        return p

    def log_event(self, device_id: Optional[str], kind: str, detail: Any = None):
        with self._tx() as db:
            db.execute("INSERT INTO pb_device_events(device_id, kind, detail, created_at) VALUES (?,?,?,?)",
                       (device_id, kind, json.dumps(detail) if detail is not None else None, time.time()))

    def notify(self, audience: str, kind: str, title: str, body: str = "", ref: str = "") -> Dict[str, Any]:
        """audience: a device_id, or 'commander'."""
        with self._tx() as db:
            cur = db.execute("INSERT INTO pb_notifications(audience, kind, title, body, ref, created_at) VALUES (?,?,?,?,?,?)",
                             (audience, kind, title, body, ref, time.time()))
            seq = cur.lastrowid
        return self._one("SELECT * FROM pb_notifications WHERE seq=?", (seq,))

    # ---------------- members ----------------
    def ensure_member(self, device_id: str, name: str = "") -> Dict[str, Any]:
        if not device_id:
            raise PBError("Unknown device", 403)
        now = time.time()
        with self._tx() as db:
            row = db.execute("SELECT * FROM pb_members WHERE device_id=?", (device_id,)).fetchone()
            if not row:
                db.execute("INSERT INTO pb_members(device_id, member_id, name, created_at, last_seen) VALUES (?,?,?,?,?)",
                           (device_id, _id("mbr"), (name or device_id)[:60], now, now))
                db.execute("INSERT OR IGNORE INTO pb_conversations(id, device_id) VALUES (?,?)", (_id("conv"), device_id))
            else:
                db.execute("UPDATE pb_members SET last_seen=?, name=? WHERE device_id=?",
                           (now, (name or row["name"])[:60], device_id))
        return self._one("SELECT * FROM pb_members WHERE device_id=?", (device_id,))

    def set_username(self, device_id: str, username: str) -> Dict[str, Any]:
        username = (username or "").strip()
        if not USERNAME_RE.fullmatch(username):
            raise PBError("Username: 3–20 letters, numbers, _ or . and it must start with a letter")
        low = username.lower()
        with self._tx() as db:
            taken = db.execute("SELECT device_id FROM pb_members WHERE lower(username)=? AND device_id<>?", (low, device_id)).fetchone()
            if taken or db.execute("SELECT 1 FROM pb_demo_players WHERE lower(username)=?", (low,)).fetchone():
                raise PBError(f"'{username}' is already taken — try another", 409)
            db.execute("UPDATE pb_members SET username=? WHERE device_id=?", (username, device_id))
        return self.profile(device_id)

    def members(self) -> List[Dict[str, Any]]:
        return self._q("SELECT * FROM pb_members ORDER BY name")

    def xp_of(self, device_id: str) -> int:
        r = self._one("SELECT COALESCE(SUM(amount),0) AS xp FROM pb_reward_transactions WHERE device_id=?", (device_id,))
        return int(r["xp"])

    def profile(self, device_id: str) -> Dict[str, Any]:
        m = self._one("SELECT * FROM pb_members WHERE device_id=?", (device_id,))
        if not m:
            raise PBError("Unknown member", 404)
        xp = self.xp_of(device_id)
        counts = self._one(
            "SELECT SUM(status='completed') AS completed, COUNT(*) AS total FROM pb_assignments WHERE device_id=?", (device_id,))
        rejected = self._one("SELECT COUNT(*) AS n FROM pb_submissions WHERE device_id=? AND status IN ('rejected','revision_requested')",
                             (device_id,))["n"]
        badges = self._q("""SELECT a.key, a.name, a.icon, a.description, ma.unlocked_at FROM pb_achievements a
                            LEFT JOIN pb_member_achievements ma ON ma.achievement=a.key AND ma.device_id=?""", (device_id,))
        history = self._q("SELECT * FROM pb_reward_transactions WHERE device_id=? ORDER BY created_at DESC LIMIT 50", (device_id,))
        return {**m, "xp": xp, **level_for(xp, self.policy()["level_base"]),
                "completed_missions": counts["completed"] or 0, "assigned_missions": counts["total"] or 0,
                "rejected_attempts": rejected, "achievements": badges, "activity": history}

    def leaderboard(self, period: str = "global", limit: int = 50, me: Optional[str] = None) -> List[Dict[str, Any]]:
        """Ranked by username only: real members who picked one, plus the demo squad when enabled."""
        since = {"weekly": time.time() - 7 * 86400, "monthly": time.time() - 30 * 86400}.get(period, 0)
        base = self.policy()["level_base"]
        rows = self._q("""SELECT m.device_id, m.username AS name, COALESCE(SUM(t.amount),0) AS xp,
                          (SELECT COUNT(*) FROM pb_assignments a WHERE a.device_id=m.device_id AND a.status='completed') AS completed
                          FROM pb_members m LEFT JOIN pb_reward_transactions t ON t.device_id=m.device_id AND t.created_at>=?
                          WHERE m.username IS NOT NULL GROUP BY m.device_id""", (since,))
        for r in rows:
            r["level"] = level_for(self.xp_of(r["device_id"]), base)["level"]
            r["is_demo"] = False
        if self.policy().get("demo_squad", 1):
            col = {"weekly": "week_xp", "monthly": "month_xp"}.get(period, "xp")
            for d in self._q(f"SELECT username, {col} AS pxp, xp, completed FROM pb_demo_players"):
                rows.append({"device_id": "demo:" + d["username"], "name": d["username"], "xp": d["pxp"],
                             "completed": d["completed"], "level": level_for(d["xp"], base)["level"], "is_demo": True})
        rows.sort(key=lambda r: (-r["xp"], r["is_demo"], r["name"].lower()))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        out = rows[:limit]
        if me and not any(r["device_id"] == me for r in out):
            out += [r for r in rows if r["device_id"] == me]
        return out

    # ---------------- XP ledger ----------------
    def _award(self, db, device_id: str, amount: int, reason: str, tx_type: str, idem_key: str,
               mission_id=None, assignment_id=None, submission_id=None, approval_ref=None) -> Optional[Dict[str, Any]]:
        """Inside a transaction. Idempotent on idem_key: a repeated request never pays twice."""
        if amount <= 0:
            return None
        if db.execute("SELECT 1 FROM pb_reward_transactions WHERE idem_key=?", (idem_key,)).fetchone():
            return None
        member = db.execute("SELECT member_id FROM pb_members WHERE device_id=?", (device_id,)).fetchone()
        tx = {"id": _id("xp"), "device_id": device_id, "member_id": member["member_id"], "mission_id": mission_id,
              "assignment_id": assignment_id, "submission_id": submission_id, "amount": int(amount), "reason": reason,
              "approval_ref": approval_ref, "tx_type": tx_type, "idem_key": idem_key, "created_at": time.time()}
        db.execute(f"INSERT INTO pb_reward_transactions({','.join(tx)}) VALUES ({','.join('?' * len(tx))})", tuple(tx.values()))
        return tx

    def manual_bonus(self, device_id: str, amount: int, reason: str, reviewer: str, request_id: str = "") -> Dict[str, Any]:
        amount = int(amount)
        if not (0 < amount <= 100000):
            raise PBError("Bonus must be between 1 and 100000 XP")
        if not (reason or "").strip():
            raise PBError("A reason is required for a manual bonus")
        if not self._one("SELECT 1 FROM pb_members WHERE device_id=?", (device_id,)):
            raise PBError("Unknown member", 404)
        with self._tx() as db:
            tx = self._award(db, device_id, amount, reason.strip()[:500], "manual_bonus",
                             f"manual:{request_id or uuid.uuid4().hex}", approval_ref=reviewer)
        unlocked = self._check_achievements(device_id)
        return {"transaction": tx, "unlocked": unlocked}

    def ledger(self, device_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        if device_id:
            return self._q("SELECT * FROM pb_reward_transactions WHERE device_id=? ORDER BY created_at DESC LIMIT ?", (device_id, limit))
        return self._q("SELECT t.*, m.name FROM pb_reward_transactions t JOIN pb_members m USING(device_id) ORDER BY created_at DESC LIMIT ?", (limit,))

    def _check_achievements(self, device_id: str) -> List[Dict[str, Any]]:
        p = self.profile(device_id)
        have = {a["key"] for a in p["achievements"] if a["unlocked_at"]}
        sent = self._one("""SELECT COUNT(*) AS n FROM pb_messages m JOIN pb_conversations c ON c.id=m.conversation_id
                            WHERE c.device_id=? AND m.sender='member'""", (device_id,))["n"]
        first_try = self._one("SELECT 1 FROM pb_submissions WHERE device_id=? AND attempt=1 AND status='approved'", (device_id,))
        board = self.leaderboard("global", 1)
        earned = {
            "first_mission": p["completed_missions"] >= 1,
            "perfect_submission": bool(first_try),
            "consistent_contributor": p["completed_missions"] >= 5,
            "mission_master": p["completed_missions"] >= 10,
            "communication_expert": sent >= 50,
            "squad_champion": bool(board) and board[0]["device_id"] == device_id and board[0]["xp"] > 0,
            "elite_operator": p["level"] >= 10,
        }
        new = []
        with self._tx() as db:
            for key, ok in earned.items():
                if ok and key not in have:
                    db.execute("INSERT OR IGNORE INTO pb_member_achievements VALUES (?,?,?)", (device_id, key, time.time()))
                    new.append(next({"key": a[0], "name": a[1], "icon": a[2], "description": a[3]} for a in ACHIEVEMENTS if a[0] == key))
        return new

    # ---------------- files ----------------
    def upload_init(self, *, uploader: str, owner_device: Optional[str], purpose: str, name: str, mime: str,
                    size: int, sha256: str, upload_id: str = "", max_size: int = MAX_CHAT_FILE,
                    allowed_ext: Optional[List[str]] = None) -> Dict[str, Any]:
        """uploader: device_id or 'commander'. purpose: chat | submission | reference. Resumable by upload_id."""
        size = int(size)
        if purpose not in ("chat", "submission", "reference"):
            raise PBError("Bad upload purpose")
        if size <= 0 or size > max_size:
            raise PBError(f"File too large (limit {max_size // (1024 * 1024)} MB)", 413)
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise PBError("Bad checksum")
        name = _safe(name)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        mime = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream").lower()
        if not re.fullmatch(r"[a-z]+/[a-z0-9.+-]+", mime):
            raise PBError("Bad MIME type")
        kind = kind_of(mime, name)
        if kind == "document" and ext and ext not in DOC_EXT and mime != "application/octet-stream":
            pass  # unknown documents are allowed but always served as downloads
        if allowed_ext:
            if ext not in [e.lower().lstrip(".") for e in allowed_ext]:
                raise PBError(f"'.{ext or '?'}' is not an allowed format for this mission ({', '.join(allowed_ext)})", 415)
        upload_id = upload_id if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", upload_id or "") else _id("up")
        fid = f"f_{hashlib.sha256((uploader + ':' + upload_id).encode()).hexdigest()[:24]}"
        existing = self._one("SELECT * FROM pb_files WHERE id=?", (fid,))
        if existing:
            if existing["uploaded_by"] != uploader:
                raise PBError("Not your upload", 403)
            return existing
        # duplicate detection: the same bytes already uploaded by this sender are reused
        if sha256:
            dup = self._one("SELECT * FROM pb_files WHERE uploaded_by=? AND sha256=? AND status='ready' AND size=? AND purpose=?",
                            (uploader, sha256, size, purpose))
            if dup:
                return dup
        folder = self.files_dir / _safe(owner_device or "commander") / purpose / time.strftime("%Y%m")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{fid}_{name}"
        with self._tx() as db:
            db.execute("""INSERT INTO pb_files(id, owner_device, uploaded_by, purpose, name, mime, kind, size, sha256, received, path, status, created_at)
                          VALUES (?,?,?,?,?,?,?,?,?,0,?,'uploading',?)""",
                       (fid, owner_device, uploader, purpose, name, mime, kind, size, sha256 or None, str(path), time.time()))
        return self._one("SELECT * FROM pb_files WHERE id=?", (fid,))

    def upload_chunk(self, file_id: str, uploader: str, offset: int, data: bytes) -> Dict[str, Any]:
        f = self._one("SELECT * FROM pb_files WHERE id=?", (file_id,))
        if not f or f["uploaded_by"] != uploader:
            raise PBError("Unknown upload", 404)
        if f["status"] == "ready":
            return f
        if offset != f["received"]:
            return f  # out of order / duplicate chunk: caller resends from f["received"]
        if offset + len(data) > f["size"]:
            raise PBError("Upload exceeds declared size", 413)
        with self._lock:
            with open(f["path"], "ab") as fh:
                fh.truncate(offset)
                fh.write(data)
            received = offset + len(data)
            status = "uploading"
            if received == f["size"]:
                h = hashlib.sha256()
                with open(f["path"], "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        h.update(block)
                digest = h.hexdigest()
                if f["sha256"] and digest != f["sha256"]:
                    Path(f["path"]).unlink(missing_ok=True)
                    self.db.execute("UPDATE pb_files SET received=0, status='uploading' WHERE id=?", (file_id,))
                    raise PBError("Checksum mismatch: the file was corrupted in transit, retrying", 422)
                self.db.execute("UPDATE pb_files SET sha256=? WHERE id=?", (digest, file_id))
                status = "ready"
            self.db.execute("UPDATE pb_files SET received=?, status=? WHERE id=?", (received, status, file_id))
        return self._one("SELECT * FROM pb_files WHERE id=?", (file_id,))

    def file(self, file_id: str) -> Dict[str, Any]:
        f = self._one("SELECT * FROM pb_files WHERE id=?", (file_id,))
        if not f:
            raise PBError("File not found", 404)
        return f

    def can_device_read(self, device_id: str, file_id: str) -> bool:
        """Secure download authorization for phones: only files of their own chat, own submissions,
        or reference files of missions assigned to them."""
        f = self._one("SELECT * FROM pb_files WHERE id=? AND status='ready'", (file_id,))
        if not f:
            return False
        if f["uploaded_by"] == device_id:
            return True
        if self._one("""SELECT 1 FROM pb_message_attachments ma JOIN pb_messages m ON m.id=ma.message_id
                        JOIN pb_conversations c ON c.id=m.conversation_id WHERE ma.file_id=? AND c.device_id=?""", (file_id, device_id)):
            return True
        for r in self._q("""SELECT m.reference_files FROM pb_assignments a JOIN pb_missions m ON m.id=a.mission_id
                            WHERE a.device_id=?""", (device_id,)):
            if file_id in json.loads(r["reference_files"] or "[]"):
                return True
        return False

    def read_chunk(self, file_id: str, offset: int, length: int) -> bytes:
        f = self.file(file_id)
        with open(f["path"], "rb") as fh:
            fh.seek(max(0, offset))
            return fh.read(length)

    def _public_file(self, f: Dict[str, Any]) -> Dict[str, Any]:
        return {k: f[k] for k in ("id", "name", "mime", "kind", "size", "sha256", "status")}

    def _files(self, ids: List[str]) -> List[Dict[str, Any]]:
        out = []
        for i in ids:
            f = self._one("SELECT * FROM pb_files WHERE id=?", (i,))
            if f:
                out.append(self._public_file(f))
        return out

    # ---------------- chat ----------------
    def conversation_for(self, device_id: str) -> Dict[str, Any]:
        c = self._one("SELECT * FROM pb_conversations WHERE device_id=?", (device_id,))
        if not c:
            self.ensure_member(device_id)
            c = self._one("SELECT * FROM pb_conversations WHERE device_id=?", (device_id,))
        return c

    def _msg_out(self, m: Dict[str, Any]) -> Dict[str, Any]:
        files = [r["file_id"] for r in self._q("SELECT file_id FROM pb_message_attachments WHERE message_id=?", (m["id"],))]
        conv = self._one("SELECT * FROM pb_conversations WHERE id=?", (m["conversation_id"],))
        reply = None
        if m.get("reply_to"):
            r = self._one("SELECT id, sender, text FROM pb_messages WHERE id=? AND conversation_id=?", (m["reply_to"], m["conversation_id"]))
            reply = r
        read = (conv["member_read_seq"] if m["sender"] == "commander" else conv["admin_read_seq"]) >= m["seq"]
        delivered = m["sender"] == "member" or conv["member_delivered_seq"] >= m["seq"]
        return {**m, "device_id": conv["device_id"], "reactions": json.loads(m["reactions"] or "{}"),
                "attachments": self._files(files), "reply": reply, "read": read, "delivered": delivered}

    def send_message(self, device_id: str, sender: str, text: str = "", client_id: str = "", reply_to: str = "",
                     attachment_ids: Optional[List[str]] = None, forwarded: bool = False) -> Dict[str, Any]:
        """sender: 'member' (the phone itself) or 'commander'. Idempotent on (sender, client_id)."""
        conv = self.conversation_for(device_id)
        text = (text or "")[:8000]
        attachment_ids = list(dict.fromkeys(attachment_ids or []))[:10]
        uploader = device_id if sender == "member" else "commander"
        for fid in attachment_ids:
            f = self._one("SELECT * FROM pb_files WHERE id=?", (fid,))
            # attachments must be ready, and either uploaded by the sender or (commander forward) any file
            if not f or f["status"] != "ready" or (sender == "member" and f["uploaded_by"] != uploader):
                raise PBError("Attachment is not available", 400)
        if not text.strip() and not attachment_ids:
            raise PBError("Empty message")
        client_id = client_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client_id or "") else uuid.uuid4().hex
        with self._tx() as db:
            old = db.execute("SELECT * FROM pb_messages WHERE conversation_id=? AND sender=? AND client_id=?",
                             (conv["id"], sender, client_id)).fetchone()
            if old:
                return self._msg_out_after(dict(old))
            mid = _id("msg")
            cur = db.execute("""INSERT INTO pb_messages(id, conversation_id, sender, client_id, text, reply_to, forwarded, created_at)
                                VALUES (?,?,?,?,?,?,?,?)""",
                             (mid, conv["id"], sender, client_id, text, reply_to or None, int(forwarded), time.time()))
            for fid in attachment_ids:
                db.execute("INSERT OR IGNORE INTO pb_message_attachments VALUES (?,?)", (mid, fid))
            seq = cur.lastrowid
            if sender == "member":
                db.execute("UPDATE pb_conversations SET member_read_seq=MAX(member_read_seq,?), member_delivered_seq=MAX(member_delivered_seq,?) WHERE id=?",
                           (seq, seq, conv["id"]))
            else:
                db.execute("UPDATE pb_conversations SET admin_read_seq=MAX(admin_read_seq,?) WHERE id=?", (seq, conv["id"]))
        return self._msg_out_after(self._one("SELECT * FROM pb_messages WHERE id=?", (mid,)))

    def _msg_out_after(self, m):  # outside the transaction lock re-entry is fine (RLock)
        return self._msg_out(m)

    def messages(self, device_id: str, after_seq: int = 0, limit: int = 300, query: str = "") -> List[Dict[str, Any]]:
        conv = self.conversation_for(device_id)
        if query:
            rows = self._q("SELECT * FROM pb_messages WHERE conversation_id=? AND text LIKE ? ORDER BY seq DESC LIMIT ?",
                           (conv["id"], f"%{query}%", limit))
            rows.reverse()
        else:
            rows = self._q("SELECT * FROM (SELECT * FROM pb_messages WHERE conversation_id=? AND seq>? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                           (conv["id"], after_seq, limit))
        return [self._msg_out(m) for m in rows]

    def message(self, message_id: str) -> Dict[str, Any]:
        m = self._one("SELECT * FROM pb_messages WHERE id=?", (message_id,))
        if not m:
            raise PBError("Message not found", 404)
        return self._msg_out(m)

    def mark_read(self, device_id: str, by: str, up_to_seq: int, delivered_only: bool = False) -> Dict[str, Any]:
        conv = self.conversation_for(device_id)
        top = self._one("SELECT COALESCE(MAX(seq),0) AS s FROM pb_messages WHERE conversation_id=?", (conv["id"],))["s"]
        up = max(0, min(int(up_to_seq), top))
        with self._tx() as db:
            if by == "member":
                db.execute("UPDATE pb_conversations SET member_delivered_seq=MAX(member_delivered_seq,?) WHERE id=?", (up, conv["id"]))
                if not delivered_only:
                    db.execute("UPDATE pb_conversations SET member_read_seq=MAX(member_read_seq,?) WHERE id=?", (up, conv["id"]))
            else:
                db.execute("UPDATE pb_conversations SET admin_read_seq=MAX(admin_read_seq,?) WHERE id=?", (up, conv["id"]))
        return self._one("SELECT * FROM pb_conversations WHERE id=?", (conv["id"],))

    def react(self, device_id: str, by: str, message_id: str, emoji: str) -> Dict[str, Any]:
        conv = self.conversation_for(device_id)
        emoji = (emoji or "")[:8]
        with self._tx() as db:
            row = db.execute("SELECT reactions FROM pb_messages WHERE id=? AND conversation_id=?", (message_id, conv["id"])).fetchone()
            if not row:
                raise PBError("Message not found", 404)
            r = json.loads(row["reactions"] or "{}")
            if not emoji or r.get(by) == emoji:
                r.pop(by, None)
            else:
                r[by] = emoji
            db.execute("UPDATE pb_messages SET reactions=? WHERE id=?", (json.dumps(r), message_id))
        return self.message(message_id)

    def conversations(self) -> List[Dict[str, Any]]:
        rows = self._q("""SELECT c.*, CASE WHEN m.username IS NULL THEN m.name ELSE m.username || ' · ' || m.name END AS name, m.username, m.last_seen,
                          (SELECT COUNT(*) FROM pb_messages x WHERE x.conversation_id=c.id AND x.sender='member' AND x.seq>c.admin_read_seq) AS unread,
                          (SELECT text FROM pb_messages x WHERE x.conversation_id=c.id ORDER BY seq DESC LIMIT 1) AS last_text,
                          (SELECT created_at FROM pb_messages x WHERE x.conversation_id=c.id ORDER BY seq DESC LIMIT 1) AS last_at
                          FROM pb_conversations c JOIN pb_members m USING(device_id)
                          ORDER BY COALESCE(last_at, 0) DESC, m.name""")
        return rows

    # ---------------- missions ----------------
    def _mission_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        title = (data.get("title") or "").strip()
        if not title:
            raise PBError("Mission title is required")
        dist = data.get("distribution", "broadcast")
        if dist not in ("broadcast", "targeted"):
            raise PBError("Distribution must be broadcast or targeted")
        targets = [t for t in (data.get("targets") or []) if isinstance(t, str)]
        if dist == "targeted" and len(targets) != 1:
            raise PBError("A targeted mission needs exactly one device")
        prio = data.get("priority", "normal")
        if prio not in PRIORITIES:
            raise PBError("Bad priority")
        fmts = data.get("allowed_formats") or []
        if isinstance(fmts, str):
            fmts = [x.strip().lstrip(".").lower() for x in fmts.split(",") if x.strip()]
        refs = [r for r in (data.get("reference_files") or []) if isinstance(r, str)]
        for r in refs:
            f = self._one("SELECT uploaded_by, status FROM pb_files WHERE id=?", (r,))
            if not f or f["uploaded_by"] != "commander" or f["status"] != "ready":
                raise PBError("Reference attachment is not available")
        deadline = data.get("deadline")
        return {
            "title": title[:200], "description": (data.get("description") or "")[:5000],
            "instructions": (data.get("instructions") or "")[:20000], "distribution": dist,
            "targets": json.dumps(targets), "priority": prio,
            "xp_reward": max(0, min(int(data.get("xp_reward", self.policy()["mission_approved"])), 100000)),
            "bonus_xp": max(0, min(int(data.get("bonus_xp", 0) or 0), 100000)),
            "deadline": float(deadline) if deadline not in (None, "") else None,
            "deliverables": (data.get("deliverables") or "")[:5000], "allowed_formats": json.dumps(fmts),
            "max_attachment_mb": max(1, min(int(data.get("max_attachment_mb", 50) or 50), 2048)),
            "submission_instructions": (data.get("submission_instructions") or "")[:5000],
            "max_attempts": max(1, min(int(data.get("max_attempts", 3) or 3), 50)),
            "reference_files": json.dumps(refs),
        }

    def save_mission(self, data: Dict[str, Any], mission_id: str = "") -> Dict[str, Any]:
        fields = self._mission_fields(data)
        with self._tx() as db:
            if mission_id:
                row = db.execute("SELECT status FROM pb_missions WHERE id=?", (mission_id,)).fetchone()
                if not row:
                    raise PBError("Mission not found", 404)
                if row["status"] != "draft":
                    raise PBError("Only drafts can be edited", 409)
                db.execute(f"UPDATE pb_missions SET {','.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), mission_id))
            else:
                mission_id = _id("msn")
                fields.update(id=mission_id, status="draft", created_at=time.time())
                db.execute(f"INSERT INTO pb_missions({','.join(fields)}) VALUES ({','.join('?' * len(fields))})", tuple(fields.values()))
        return self.mission(mission_id)

    def publish(self, mission_id: str, registered: List[str]) -> List[Dict[str, Any]]:
        """Creates one independent assignment per target. Broadcast = every member registered right now."""
        m = self._one("SELECT * FROM pb_missions WHERE id=?", (mission_id,))
        if not m:
            raise PBError("Mission not found", 404)
        if m["status"] != "draft":
            raise PBError("Mission already published", 409)
        for d in registered:
            self.ensure_member(d)
        targets = json.loads(m["targets"]) if m["distribution"] == "targeted" else \
            (json.loads(m["targets"]) or [x["device_id"] for x in self.members()])
        if not targets:
            raise PBError("No registered squad members to receive this mission")
        now = time.time()
        with self._tx() as db:
            for d in targets:
                if not db.execute("SELECT 1 FROM pb_members WHERE device_id=?", (d,)).fetchone():
                    raise PBError(f"Unknown device {d}", 404)
                db.execute("INSERT OR IGNORE INTO pb_assignments(id, mission_id, device_id, status, updated_at) VALUES (?,?,?, 'assigned', ?)",
                           (_id("asg"), mission_id, d, now))
            db.execute("UPDATE pb_missions SET status='published', published_at=?, targets=? WHERE id=?",
                       (now, json.dumps(targets), mission_id))
        return self._q("SELECT * FROM pb_assignments WHERE mission_id=?", (mission_id,))

    def delete_draft(self, mission_id: str):
        with self._tx() as db:
            n = db.execute("DELETE FROM pb_missions WHERE id=? AND status='draft'", (mission_id,)).rowcount
        if not n:
            raise PBError("Only drafts can be deleted", 409)

    def _mission_out(self, m: Dict[str, Any]) -> Dict[str, Any]:
        return {**m, "targets": json.loads(m["targets"]), "allowed_formats": json.loads(m["allowed_formats"] or "[]"),
                "reference_files": self._files(json.loads(m["reference_files"] or "[]"))}

    def mission(self, mission_id: str) -> Dict[str, Any]:
        m = self._one("SELECT * FROM pb_missions WHERE id=?", (mission_id,))
        if not m:
            raise PBError("Mission not found", 404)
        out = self._mission_out(m)
        out["assignments"] = self._q("""SELECT a.*, mb.name FROM pb_assignments a JOIN pb_members mb USING(device_id)
                                        WHERE a.mission_id=? ORDER BY mb.name""", (mission_id,))
        return out

    def missions(self) -> List[Dict[str, Any]]:
        out = []
        for m in self._q("SELECT * FROM pb_missions ORDER BY created_at DESC"):
            mo = self._mission_out(m)
            stats = self._q("SELECT status, COUNT(*) AS n FROM pb_assignments WHERE mission_id=? GROUP BY status", (m["id"],))
            mo["progress"] = {s["status"]: s["n"] for s in stats}
            mo["progress"]["total"] = sum(s["n"] for s in stats)
            out.append(mo)
        return out

    def _assignment_out(self, a: Dict[str, Any]) -> Dict[str, Any]:
        m = self._mission_out(self._one("SELECT * FROM pb_missions WHERE id=?", (a["mission_id"],)))
        subs = self.submissions_for(a["id"])
        return {**a, "mission": {k: m[k] for k in m if k not in ("targets",)}, "submissions": subs,
                "attempts_left": max(0, m["max_attempts"] - a["attempts_used"])}

    def assignments_for(self, device_id: str) -> List[Dict[str, Any]]:
        rows = self._q("""SELECT a.* FROM pb_assignments a JOIN pb_missions m ON m.id=a.mission_id
                          WHERE a.device_id=? ORDER BY m.published_at DESC""", (device_id,))
        return [self._assignment_out(a) for a in rows]

    def assignment(self, assignment_id: str, device_id: Optional[str] = None) -> Dict[str, Any]:
        a = self._one("SELECT * FROM pb_assignments WHERE id=?", (assignment_id,))
        if not a or (device_id is not None and a["device_id"] != device_id):
            raise PBError("Mission not found", 404)  # same answer as missing: no existence leak
        return self._assignment_out(a)

    def mark_delivered(self, device_id: str, assignment_ids: List[str]):
        with self._tx() as db:
            for aid in assignment_ids:
                db.execute("UPDATE pb_assignments SET delivered_at=COALESCE(delivered_at, ?) WHERE id=? AND device_id=?",
                           (time.time(), aid, device_id))

    def accept(self, device_id: str, assignment_id: str) -> Dict[str, Any]:
        with self._tx() as db:
            a = db.execute("SELECT * FROM pb_assignments WHERE id=? AND device_id=?", (assignment_id, device_id)).fetchone()
            if not a:
                raise PBError("Mission not found", 404)
            if a["status"] == "assigned":
                db.execute("UPDATE pb_assignments SET status='in_progress', accepted_at=?, updated_at=? WHERE id=?",
                           (time.time(), time.time(), assignment_id))
        return self.assignment(assignment_id)

    # ---------------- submissions & review ----------------
    def submit(self, device_id: str, assignment_id: str, client_id: str, text: str, comment: str,
               file_ids: List[str]) -> Dict[str, Any]:
        client_id = client_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client_id or "") else uuid.uuid4().hex
        file_ids = list(dict.fromkeys(file_ids or []))[:30]
        with self._tx() as db:
            dup = db.execute("SELECT id FROM pb_submissions WHERE device_id=? AND client_id=?", (device_id, client_id)).fetchone()
            if dup:
                sid = dup["id"]
            else:
                a = db.execute("SELECT * FROM pb_assignments WHERE id=? AND device_id=?", (assignment_id, device_id)).fetchone()
                if not a:
                    raise PBError("Mission not found", 404)
                m = db.execute("SELECT * FROM pb_missions WHERE id=?", (a["mission_id"],)).fetchone()
                if a["status"] in ("submitted",):
                    raise PBError("Your previous submission is still awaiting review", 409)
                if a["status"] in ("completed", "failed"):
                    raise PBError("This mission is closed", 409)
                if a["attempts_used"] >= m["max_attempts"]:
                    raise PBError("No attempts left for this mission", 409)
                if m["deadline"] and time.time() > m["deadline"] + 60:
                    raise PBError("The deadline for this mission has passed", 409)
                if not (text or "").strip() and not file_ids:
                    raise PBError("Add a description or at least one attachment")
                fmts = json.loads(m["allowed_formats"] or "[]")
                for fid in file_ids:
                    f = db.execute("SELECT * FROM pb_files WHERE id=?", (fid,)).fetchone()
                    if not f or f["uploaded_by"] != device_id or f["status"] != "ready":
                        raise PBError("An attachment has not finished uploading", 409)
                    if f["size"] > m["max_attachment_mb"] * 1024 * 1024:
                        raise PBError(f"{f['name']} is larger than {m['max_attachment_mb']} MB", 413)
                    ext = f["name"].rsplit(".", 1)[-1].lower() if "." in f["name"] else ""
                    if fmts and ext not in fmts:
                        raise PBError(f"{f['name']}: format not allowed ({', '.join(fmts)})", 415)
                used = a["attempts_used"] + 1
                attempt = db.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM pb_submissions WHERE assignment_id=?",
                                     (assignment_id,)).fetchone()[0]
                sid = _id("sub")
                db.execute("""INSERT INTO pb_submissions(id, client_id, assignment_id, device_id, attempt, text, comment, status, submitted_at)
                              VALUES (?,?,?,?,?,?,?, 'pending', ?)""",
                           (sid, client_id, assignment_id, device_id, attempt, (text or "")[:20000], (comment or "")[:5000], time.time()))
                for fid in file_ids:
                    db.execute("INSERT INTO pb_submission_attachments VALUES (?,?)", (sid, fid))
                db.execute("UPDATE pb_assignments SET status='submitted', attempts_used=?, updated_at=?, accepted_at=COALESCE(accepted_at, ?) WHERE id=?",
                           (used, time.time(), time.time(), assignment_id))
        return self.submission(sid)

    def submission(self, sid: str) -> Dict[str, Any]:
        s = self._one("SELECT * FROM pb_submissions WHERE id=?", (sid,))
        if not s:
            raise PBError("Submission not found", 404)
        files = [r["file_id"] for r in self._q("SELECT file_id FROM pb_submission_attachments WHERE submission_id=?", (sid,))]
        reviews = self._q("SELECT * FROM pb_reviews WHERE submission_id=? ORDER BY created_at", (sid,))
        return {**s, "attachments": self._files(files), "reviews": reviews}

    def submissions_for(self, assignment_id: str) -> List[Dict[str, Any]]:
        return [self.submission(r["id"]) for r in
                self._q("SELECT id FROM pb_submissions WHERE assignment_id=? ORDER BY attempt", (assignment_id,))]

    def review_queue(self, status: str = "", device_id: str = "", mission_id: str = "", priority: str = "",
                     attempt: Optional[int] = None, since: Optional[float] = None, until: Optional[float] = None) -> List[Dict[str, Any]]:
        sql = """SELECT s.id, s.attempt, s.status, s.submitted_at, s.device_id, COALESCE(mb.username, mb.name) AS member_name,
                 a.id AS assignment_id, m.id AS mission_id, m.title AS mission_title, m.priority, m.xp_reward,
                 (SELECT COUNT(*) FROM pb_submission_attachments x WHERE x.submission_id=s.id) AS attachment_count
                 FROM pb_submissions s JOIN pb_assignments a ON a.id=s.assignment_id
                 JOIN pb_missions m ON m.id=a.mission_id JOIN pb_members mb ON mb.device_id=s.device_id WHERE 1=1"""
        args: list = []
        if status == "resubmission":
            sql += " AND s.attempt>1 AND s.status='pending'"
        elif status:
            sql += " AND s.status=?"
            args.append(status)
        for col, val in (("s.device_id", device_id), ("m.id", mission_id), ("m.priority", priority)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        if attempt:
            sql += " AND s.attempt=?"
            args.append(int(attempt))
        if since:
            sql += " AND s.submitted_at>=?"
            args.append(float(since))
        if until:
            sql += " AND s.submitted_at<=?"
            args.append(float(until))
        sql += " ORDER BY CASE s.status WHEN 'pending' THEN 0 ELSE 1 END, s.submitted_at DESC LIMIT 500"
        return self._q(sql, args)

    def review_detail(self, sid: str) -> Dict[str, Any]:
        s = self.submission(sid)
        a = self._one("SELECT * FROM pb_assignments WHERE id=?", (s["assignment_id"],))
        m = self._mission_out(self._one("SELECT * FROM pb_missions WHERE id=?", (a["mission_id"],)))
        member = self._one("SELECT * FROM pb_members WHERE device_id=?", (s["device_id"],))
        return {"submission": s, "assignment": a, "mission": m, "member": member,
                "history": self.submissions_for(a["id"])}

    def review(self, sid: str, action: str, reviewer: str, feedback: str = "", bonus_xp: int = 0) -> Dict[str, Any]:
        """approve | reject | revision. Approving twice (double click, retried request) never pays twice."""
        if action not in ("approve", "reject", "revision"):
            raise PBError("Bad review action")
        feedback = (feedback or "").strip()[:5000]
        if action in ("reject", "revision") and not feedback:
            raise PBError("Tell the member what needs to change")
        bonus_xp = max(0, min(int(bonus_xp or 0), 100000))
        tx_out: List[Dict[str, Any]] = []
        with self._tx() as db:
            s = db.execute("SELECT * FROM pb_submissions WHERE id=?", (sid,)).fetchone()
            if not s:
                raise PBError("Submission not found", 404)
            if s["status"] != "pending":
                if s["status"] == "approved" and action == "approve":
                    return {"submission": self.submission(sid), "transactions": [], "duplicate": True}
                raise PBError(f"This submission was already reviewed ({s['status']})", 409)
            a = db.execute("SELECT * FROM pb_assignments WHERE id=?", (s["assignment_id"],)).fetchone()
            m = db.execute("SELECT * FROM pb_missions WHERE id=?", (a["mission_id"],)).fetchone()
            rid = _id("rev")
            db.execute("INSERT INTO pb_reviews VALUES (?,?,?,?,?,?,?)",
                       (rid, sid, action, feedback, reviewer, bonus_xp if action == "approve" else 0, time.time()))
            now = time.time()
            if action == "approve":
                db.execute("UPDATE pb_submissions SET status='approved' WHERE id=?", (sid,))
                db.execute("UPDATE pb_assignments SET status='completed', completed_at=?, updated_at=? WHERE id=?", (now, now, a["id"]))
                pol = self.policy()
                common = dict(mission_id=m["id"], assignment_id=a["id"], submission_id=sid, approval_ref=rid)
                for amount, reason, kind, key in (
                        (m["xp_reward"], f"Mission approved: {m['title']}", "mission_reward", f"reward:{a['id']}"),
                        (m["bonus_xp"], f"Mission bonus: {m['title']}", "mission_bonus", f"mbonus:{a['id']}"),
                        (bonus_xp, f"Bonus-quality submission: {m['title']}", "quality_bonus", f"qbonus:{a['id']}"),
                        (pol["first_try_bonus"] if s["attempt"] == 1 else 0, "First-try approval", "first_try_bonus", f"ftry:{a['id']}")):
                    tx = self._award(db, s["device_id"], amount, reason, kind, key, **common)
                    if tx:
                        tx_out.append(tx)
            else:
                db.execute("UPDATE pb_submissions SET status=? WHERE id=?",
                           ("rejected" if action == "reject" else "revision_requested", sid))
                out_of_attempts = a["attempts_used"] >= m["max_attempts"] and action == "reject"
                db.execute("UPDATE pb_assignments SET status=?, updated_at=? WHERE id=?",
                           ("failed" if out_of_attempts else "revision_required", now, a["id"]))
                if action == "revision" and a["attempts_used"] >= m["max_attempts"]:
                    # a revision request is not a final rejection: it never uses up the last attempt
                    db.execute("UPDATE pb_assignments SET attempts_used=attempts_used-1 WHERE id=?", (a["id"],))
        unlocked = self._check_achievements(s["device_id"]) if action == "approve" else []
        return {"submission": self.submission(sid), "transactions": tx_out, "unlocked": unlocked,
                "mission": dict(m), "device_id": s["device_id"], "action": action, "feedback": feedback,
                "xp_total": sum(t["amount"] for t in tx_out)}

    # ---------------- device snapshot (offline sync) ----------------
    def device_state(self, device_id: str, msg_after: int = 0, notif_after: int = 0) -> Dict[str, Any]:
        return {
            "profile": self.profile(device_id),
            "assignments": self.assignments_for(device_id),
            "messages": self.messages(device_id, after_seq=msg_after, limit=500),
            "conversation": self.conversation_for(device_id),
            "notifications": self._q("SELECT * FROM pb_notifications WHERE audience=? AND seq>? ORDER BY seq DESC LIMIT 100",
                                     (device_id, notif_after)),
            "leaderboard": {p: self.leaderboard(p, 25, device_id) for p in ("global", "weekly", "monthly")},
            "policy": self.policy(),
            "server_time": time.time(),
        }

    def notifications(self, audience: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self._q("SELECT * FROM pb_notifications WHERE audience=? ORDER BY seq DESC LIMIT ?", (audience, limit))

    def mark_notifications_read(self, audience: str):
        with self._tx() as db:
            db.execute("UPDATE pb_notifications SET read=1 WHERE audience=?", (audience,))
