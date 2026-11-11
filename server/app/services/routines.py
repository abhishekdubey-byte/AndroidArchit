"""Scheduled routines: run an AI-operator instruction on a phone every day (or on chosen weekdays) at a set time.

Saved in storage/routines.json so they survive restarts. If the phone is offline (or busy) at the set time, the
routine keeps trying for up to CATCH_UP_HOURS that day and runs as soon as the phone is back.
"""
import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import STORAGE_DIR

PATH = STORAGE_DIR / "routines.json"
CATCH_UP_HOURS = 6
TICK = 20  # seconds


class RoutineService:
    def __init__(self, ai_agent, device_manager):
        self.ai = ai_agent
        self.dm = device_manager
        self.items: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._load()

    # ---- storage ----
    def _load(self):
        try:
            self.items = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else []
        except Exception:
            self.items = []

    def _save(self):
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=2), encoding="utf-8")
        tmp.replace(PATH)

    # ---- CRUD ----
    def list(self) -> List[Dict[str, Any]]:
        out = []
        for r in self.items:
            r = dict(r)
            t = self.ai.get_task(r.get("last_task_id") or "") if r.get("last_task_id") else None
            if t:
                r["last_status"] = t.status
                r["last_result"] = t.result_summary or r.get("last_result")
            dev = self.dm.get_device(r.get("device_id") or "")
            r["device_name"] = dev.name if dev else r.get("device_id")
            r["next_run"] = self._next_run_text(r)
            out.append(r)
        return out

    def upsert(self, data: Dict[str, Any]) -> Dict[str, Any]:
        hhmm = str(data.get("time") or "09:00")
        h, m = (int(x) for x in hhmm.split(":")[:2])
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("time must be HH:MM")
        days = sorted({int(d) for d in (data.get("days") or []) if 0 <= int(d) <= 6})
        r = next((x for x in self.items if x["id"] == data.get("id")), None)
        if r is None:
            r = {"id": "rt_" + uuid.uuid4().hex[:8], "created_at": time.time(), "last_run_date": None,
                 "last_task_id": None, "last_result": None}
            self.items.append(r)
        r.update({
            "name": str(data.get("name") or "Routine")[:60],
            "instruction": str(data.get("instruction") or "").strip()[:300],
            "device_id": str(data.get("device_id") or ""),
            "time": f"{h:02d}:{m:02d}",
            "days": days,                       # [] = every day; 0 = Monday … 6 = Sunday
            "enabled": bool(data.get("enabled", True)),
        })
        if not r["instruction"] or not r["device_id"]:
            raise ValueError("instruction and device_id are required")
        # a routine edited to a later time today should still run today
        if r.get("last_run_date") == self._today() and r["time"] > datetime.now().strftime("%H:%M"):
            r["last_run_date"] = None
        self._save()
        return r

    def delete(self, rid: str) -> bool:
        n = len(self.items)
        self.items = [x for x in self.items if x["id"] != rid]
        self._save()
        return len(self.items) != n

    async def run_now(self, rid: str):
        r = next((x for x in self.items if x["id"] == rid), None)
        if r is None:
            raise KeyError(rid)
        return await self._fire(r, manual=True)

    # ---- scheduling ----
    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _next_run_text(self, r) -> str:
        if not r.get("enabled"):
            return "paused"
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        days = "every day" if not r.get("days") else ", ".join(names[d] for d in r["days"])
        return f"{days} at {r['time']}"

    def _due(self, r, now: datetime) -> bool:
        if not r.get("enabled") or r.get("last_run_date") == now.strftime("%Y-%m-%d"):
            return False
        if r.get("days") and now.weekday() not in r["days"]:
            return False
        h, m = (int(x) for x in r["time"].split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return start <= now and (now - start).total_seconds() <= CATCH_UP_HOURS * 3600

    def _busy(self, device_id: str) -> bool:
        return any(t.target_device_id == device_id and t.status in ("pending", "executing", "waiting_approval")
                   for t in self.ai.get_all_tasks())

    async def _fire(self, r, manual: bool = False):
        dev = self.dm.get_device(r["device_id"])
        if not dev or dev.status == "offline":
            r["last_result"] = "Waiting for the phone to come online"
            self._save()
            if manual:
                raise RuntimeError("The phone is offline")
            return None
        if self._busy(dev.id):
            r["last_result"] = "Waiting: the phone is busy with another task"
            self._save()
            if manual:
                raise RuntimeError("The phone is busy with another task")
            return None
        task = await self.ai.submit_task(r["instruction"], dev.id)
        if not manual:
            r["last_run_date"] = self._today()
        r["last_task_id"] = task.id
        r["last_started_at"] = time.time()
        r["last_result"] = "Running…"
        self._save()
        print(f"[Routines] '{r['name']}' started on {dev.name} ({'manual' if manual else 'scheduled'})")
        return task

    async def _loop(self):
        while True:
            try:
                now = datetime.now()
                for r in list(self.items):
                    if self._due(r, now):
                        await self._fire(r)
            except Exception as e:
                print(f"[Routines] scheduler error: {e}")
            await asyncio.sleep(TICK)

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
