"""マネージャー通知ログ（長文の自動通知をチャットから分離して保存）。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="/manager/notices", tags=["notices"])


class ManagerNoticeReadRequest(BaseModel):
    is_read: bool = True


@router.get("", dependencies=[Depends(verify_api_key)])
async def manager_notices_get(limit: int = 30):
    from api.database import list_manager_notices
    try:
        items = await list_manager_notices(limit=max(1, min(int(limit), 100)))
    except Exception as e:
        logging.error(f"manager_notices fetch error: {e}")
        items = []
    unread = sum(1 for it in items if not it.get("is_read"))
    return {"ok": True, "items": items, "unread": unread}


@router.post("/{nid}/read", dependencies=[Depends(verify_api_key)])
async def manager_notice_set_read(nid: int, req: ManagerNoticeReadRequest):
    from api.database import set_manager_notice_read
    ok = await set_manager_notice_read(nid, req.is_read)
    if not ok:
        raise HTTPException(status_code=404, detail="通知が見つかりません")
    return {"ok": True}


@router.delete("/{nid}", dependencies=[Depends(verify_api_key)])
async def manager_notice_delete(nid: int):
    from api.database import delete_manager_notice
    ok = await delete_manager_notice(nid)
    if not ok:
        raise HTTPException(status_code=404, detail="通知が見つかりません")
    return {"ok": True}
