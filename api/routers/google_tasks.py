"""Google Tasks (仕事/プライベート/習慣リスト) 関連エンドポイント。

action: add / delete / update / toggle と並び替え (move) と取得 (get) を提供。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="", tags=["tasks"])


class GTaskActionRequest(BaseModel):
    action: str
    task_id: Optional[str] = None
    title: Optional[str] = None
    completed: Optional[bool] = None
    list_name: Optional[str] = None
    due: Optional[str] = None  # RFC3339 (YYYY-MM-DDTHH:MM:SS.000Z) または YYYY-MM-DD


class GTaskMoveRequest(BaseModel):
    task_id: str
    previous_task_id: Optional[str] = None
    list_name: Optional[str] = None
    parent: Optional[str] = None  # 親タスクIDを指定するとサブタスク化


def _get_tasks_service():
    from api import app
    bot = getattr(app.state, "bot", None)
    svc = getattr(bot, "tasks_service", None) if bot else None
    if not svc:
        raise HTTPException(status_code=503, detail="タスクサービス未設定")
    return svc


@router.post("/google_tasks_action", dependencies=[Depends(verify_api_key)])
async def google_tasks_action(req: GTaskActionRequest):
    svc = _get_tasks_service()
    if req.action == "add":
        res = await svc.add_task(req.title, list_name=req.list_name, due=req.due)
    elif req.action == "delete":
        res = await svc.delete_task(req.task_id, list_name=req.list_name)
    elif req.action == "update":
        res = await svc.update_task(req.task_id, title=req.title, due=req.due, list_name=req.list_name)
    elif req.action == "toggle":
        res = await svc.update_task(req.task_id, completed=req.completed, list_name=req.list_name)
    else:
        res = "不明なアクションです"
    return {"status": "success", "message": res}


@router.post("/google_tasks_move", dependencies=[Depends(verify_api_key)])
async def google_tasks_move(req: GTaskMoveRequest):
    svc = _get_tasks_service()
    res = await svc.move_task(
        req.task_id, req.previous_task_id, req.list_name, parent=req.parent or None
    )
    return {"status": "success", "message": res}


@router.get("/google_tasks", dependencies=[Depends(verify_api_key)])
async def get_google_tasks(list_name: str = "仕事"):
    """指定リストのタスク（未完了 + 本日完了分）を軽量に返す。
    並び替え後の再描画など、ダッシュボード全体を再取得せずに該当リストだけを更新したいときに使う。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    tasks_service = getattr(bot, "tasks_service", None) if bot else None
    if not tasks_service:
        return {"tasks": []}
    try:
        uncompleted = await tasks_service.get_raw_tasks(list_name)
        done_today = await tasks_service.get_completed_tasks_today(list_name)
        tasks = uncompleted + [
            {"id": f"done_{list_name}_{i}", "title": t, "notes": "", "completed": True}
            for i, t in enumerate(done_today)
        ]
        return {"tasks": tasks}
    except Exception as e:
        logging.debug(f"get_google_tasks({list_name}) error: {e}")
        return {"tasks": []}
