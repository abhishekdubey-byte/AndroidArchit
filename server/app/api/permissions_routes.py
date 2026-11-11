from fastapi import APIRouter, HTTPException

from ..services.permissions import PermissionService
from ..services.storage_service import StorageError


def create_permissions_router(perms: PermissionService) -> APIRouter:
    """Read-only view of each phone's companion-app permissions (static/permissions.html)."""
    router = APIRouter(prefix="/api/permissions", tags=["permissions"])

    @router.get("")
    async def all_phones():
        return {"phones": await perms.all()}

    @router.get("/{device_id}")
    async def one_phone(device_id: str):
        rep = await perms.report(device_id)
        if rep is None:
            raise HTTPException(404, f"Phone '{device_id}' not found")
        return rep

    @router.post("/{device_id}/screen-share")
    async def preapprove_screen_share(device_id: str):
        try:
            return await perms.preapprove_screen_share(device_id)
        except StorageError as e:
            raise HTTPException(e.status, e.message)

    return router
