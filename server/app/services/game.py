"""Real-time shared mining game. Phones are controllers; every strike hits one shared crystal.
Pure in-memory round state; broadcast to all connected phones and dashboards over the existing WS.
"""
import time
from typing import Any, Dict, List


class GameState:
    def __init__(self):
        self.level = 1
        self.max_hp = 50
        self.hp = 50
        self.players: Dict[str, Dict[str, Any]] = {}   # device_id -> {name, score, streak, last}
        self.last_strike: Dict[str, Any] = {}

    def join(self, device_id: str, name: str):
        p = self.players.setdefault(device_id, {"name": name or "Miner", "score": 0, "streak": 0, "last": 0.0})
        p["name"] = name or p["name"]

    def strike(self, device_id: str, name: str, power: int = 1) -> Dict[str, Any]:
        power = max(1, min(int(power), 20))
        p = self.players.setdefault(device_id, {"name": name or "Miner", "score": 0, "streak": 0, "last": 0.0})
        if name:
            p["name"] = name
        now = time.time()
        p["streak"] = p["streak"] + 1 if now - p["last"] < 1.2 else 1   # combo within 1.2s
        p["last"] = now
        mult = 1 + min(p["streak"], 10) // 3                            # x1..x4 by combo
        dealt = power * mult
        self.hp -= dealt
        p["score"] += dealt
        cracked = False
        if self.hp <= 0:
            cracked = True
            p["score"] += 10 * self.level                              # bonus for the finishing blow
            self.level += 1
            self.max_hp = int(self.max_hp * 1.4)
            self.hp = self.max_hp
        self.last_strike = {"device_id": device_id, "name": p["name"], "dealt": dealt, "streak": p["streak"], "cracked": cracked}
        return self.last_strike

    def leave(self, device_id: str):
        self.players.pop(device_id, None)

    def snapshot(self) -> Dict[str, Any]:
        board = sorted(({"name": v["name"], "score": v["score"], "id": k} for k, v in self.players.items()),
                       key=lambda x: -x["score"])
        return {"level": self.level, "hp": max(0, self.hp), "max_hp": self.max_hp,
                "players": board, "last_strike": self.last_strike}
