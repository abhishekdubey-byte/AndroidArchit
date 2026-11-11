"""Shared waitlist counter: base 370 plus one per unique device that joins."""
import json
from typing import Set

from ..config import STORAGE_DIR

BASE = 370
_FILE = STORAGE_DIR / "waitlist.json"


def _load() -> Set[str]:
    try:
        return set(json.loads(_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _save(ids: Set[str]):
    _FILE.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def count() -> int:
    return BASE + len(_load())


def has(device_id: str) -> bool:
    return device_id in _load()


def join(device_id: str) -> int:
    ids = _load()
    if device_id and device_id not in ids:
        ids.add(device_id)
        _save(ids)
    return BASE + len(ids)
