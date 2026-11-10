from typing import Any, Dict
from fastapi import APIRouter, HTTPException


def create_routine_router(routines) -> APIRouter:
    router = APIRouter(prefix="/api/routines", tags=["routines"])

    @router.get("")
    async def list_routines():
        return routines.list()

    @router.post("")
    async def save_routine(body: Dict[str, Any]):
        try:
            return routines.upsert(body)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.delete("/{rid}")
    async def delete_routine(rid: str):
        if not routines.delete(rid):
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    @router.post("/{rid}/run")
    async def run_routine(rid: str):
        try:
            task = await routines.run_now(rid)
        except KeyError:
            raise HTTPException(status_code=404, detail="not found")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True, "task_id": task.id}

    return router
