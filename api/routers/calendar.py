"""Google カレンダー操作エンドポイント（add / delete / update）。"""

import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["calendar"])


class CalendarActionRequest(BaseModel):
    action: str
    event_id: Optional[str] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


@router.post("/calendar_action", dependencies=[Depends(verify_api_key)])
async def calendar_action(req: CalendarActionRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.calendar_service:
        raise HTTPException(status_code=503, detail="カレンダーサービス未設定")

    if req.action == "add":
        start = req.start_time or datetime.datetime.now(JST).strftime("%Y-%m-%d 10:00:00")
        end = req.end_time or (
            datetime.datetime.strptime(start[:19], "%Y-%m-%d %H:%M:%S")
            + datetime.timedelta(hours=1)
        ).strftime("%Y-%m-%d %H:%M:%S") if " " in start else start
        res = await bot.calendar_service.create_event(req.summary, start, end, req.description or "")
    elif req.action == "delete":
        res = await bot.calendar_service.delete_event(req.event_id)
    elif req.action == "update":
        res = await bot.calendar_service.update_event(
            req.event_id, summary=req.summary, description=req.description
        )
    else:
        res = "不明なアクションです"
    return {"status": "success", "message": res}
