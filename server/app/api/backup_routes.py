from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from ..services.backup import BackupService


class Selection(BaseModel):
    presets: List[str] = []
    custom: List[str] = []


class Paths(BaseModel):
    paths: List[str]


def create_backup_router(backups: BackupService) -> APIRouter:
    """Phone Backup (docs/superpowers/specs/2026-11-04-phone-backup-design.md). BackupError → JSON in main.py."""
    router = APIRouter(prefix="/api/backup", tags=["backup"])

    @router.get("")
    async def phones():
        return {"phones": backups.phones()}

    @router.get("/{device_id}/status")
    async def status(device_id: str):
        return backups.status(device_id)

    @router.put("/{device_id}/selection")
    async def set_selection(device_id: str, sel: Selection):
        return backups.set_selection(device_id, sel.model_dump())

    @router.post("/{device_id}/custom")
    async def add_custom(device_id: str, body: Paths):
        return backups.add_custom(device_id, body.paths)

    @router.post("/{device_id}/run")
    async def run(device_id: str):
        return backups.start(device_id)

    @router.post("/{device_id}/cancel")
    async def cancel(device_id: str):
        return backups.cancel(device_id)

    return router
