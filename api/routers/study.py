"""勉強（学習目標 + 教材/質問 + セッション時間計測）。"""

import datetime
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/study", tags=["study"])

STUDY_DATA_FILE = "study_data.json"


def _empty_study_data() -> dict:
    return {"goals": [], "items": []}


async def _load_study_data() -> dict:
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return _empty_study_data()
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return _empty_study_data()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return _empty_study_data()
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, STUDY_DATA_FILE)
    if not f_id:
        return _empty_study_data()
    try:
        raw = await drive.read_text_file(service, f_id)
        data = json.loads(raw) or {}
        data.setdefault("goals", [])
        data.setdefault("items", [])
        return data
    except Exception as e:
        logging.warning(f"study_data.json 読込失敗: {e}")
        return _empty_study_data()


async def _save_study_data(data: dict) -> None:
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, STUDY_DATA_FILE)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    if f_id:
        await drive.update_text(service, f_id, content)
    else:
        await drive.upload_text(service, b_folder, STUDY_DATA_FILE, content)


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{datetime.datetime.now(JST).strftime('%Y%m%d%H%M%S%f')}"


class StudyGoalSaveRequest(BaseModel):
    id: Optional[str] = None
    title: str
    due_date: Optional[str] = ""
    memo: Optional[str] = ""


class StudyIdRequest(BaseModel):
    id: str


class StudyItemSaveRequest(BaseModel):
    id: Optional[str] = None
    goal_id: Optional[str] = None
    type: str  # "material" | "question"
    title: str
    note_url: Optional[str] = ""
    memo: Optional[str] = ""


class StudySessionRequest(BaseModel):
    item_id: str
    status: str  # "start" | "end"


@router.get("", dependencies=[Depends(verify_api_key)])
async def study_get():
    data = await _load_study_data()
    return {"ok": True, **data}


@router.post("/goal/save", dependencies=[Depends(verify_api_key)])
async def study_goal_save(req: StudyGoalSaveRequest):
    title = (req.title or "").strip()
    if not title:
        return {"ok": False, "error": "タイトルを入力してください"}
    data = await _load_study_data()
    goals = data.setdefault("goals", [])
    if req.id:
        target = next((g for g in goals if g.get("id") == req.id), None)
        if not target:
            return {"ok": False, "error": "対象の目標が見つかりません"}
        target["title"] = title
        target["due_date"] = (req.due_date or "").strip()
        target["memo"] = (req.memo or "").strip()
        goal = target
    else:
        goal = {
            "id": _gen_id("g"),
            "title": title,
            "due_date": (req.due_date or "").strip(),
            "memo": (req.memo or "").strip(),
            "created_at": datetime.datetime.now(JST).isoformat(),
        }
        goals.append(goal)
    await _save_study_data(data)
    return {"ok": True, "goal": goal}


@router.post("/goal/delete", dependencies=[Depends(verify_api_key)])
async def study_goal_delete(req: StudyIdRequest):
    data = await _load_study_data()
    goals = data.setdefault("goals", [])
    after = [g for g in goals if g.get("id") != req.id]
    if len(after) == len(goals):
        return {"ok": False, "error": "対象の目標が見つかりません"}
    data["goals"] = after
    for it in data.setdefault("items", []):
        if it.get("goal_id") == req.id:
            it["goal_id"] = None
    await _save_study_data(data)
    return {"ok": True}


@router.post("/item/save", dependencies=[Depends(verify_api_key)])
async def study_item_save(req: StudyItemSaveRequest):
    title = (req.title or "").strip()
    if not title:
        return {"ok": False, "error": "タイトルを入力してください"}
    itype = req.type if req.type in ("material", "question") else "material"
    data = await _load_study_data()
    items = data.setdefault("items", [])
    if req.id:
        target = next((it for it in items if it.get("id") == req.id), None)
        if not target:
            return {"ok": False, "error": "対象の項目が見つかりません"}
        target["goal_id"] = req.goal_id or None
        target["type"] = itype
        target["title"] = title
        target["note_url"] = (req.note_url or "").strip()
        target["memo"] = (req.memo or "").strip()
        item = target
    else:
        item = {
            "id": _gen_id("i"),
            "goal_id": req.goal_id or None,
            "type": itype,
            "title": title,
            "note_url": (req.note_url or "").strip(),
            "memo": (req.memo or "").strip(),
            "created_at": datetime.datetime.now(JST).isoformat(),
        }
        items.append(item)
    await _save_study_data(data)
    return {"ok": True, "item": item}


@router.post("/item/delete", dependencies=[Depends(verify_api_key)])
async def study_item_delete(req: StudyIdRequest):
    data = await _load_study_data()
    items = data.setdefault("items", [])
    after = [it for it in items if it.get("id") != req.id]
    if len(after) == len(items):
        return {"ok": False, "error": "対象の項目が見つかりません"}
    data["items"] = after
    await _save_study_data(data)
    return {"ok": True}


@router.post("/session", dependencies=[Depends(verify_api_key)])
async def study_session(req: StudySessionRequest):
    """教材の学習セッションを開始/終了。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"ok": False, "error": "Bot 未初期化"}
    partner = bot.get_cog("PartnerCog")
    if not partner:
        return {"ok": False, "error": "PartnerCog 未ロード"}
    status = (req.status or "").lower().strip()
    if status not in ("start", "end"):
        return {"ok": False, "error": "status は start か end"}
    data = await _load_study_data()
    item = next((it for it in data.get("items", []) if it.get("id") == req.item_id), None)
    if not item:
        return {"ok": False, "error": "対象の項目が見つかりません"}
    activity_name = f"勉強：{item.get('title', '')}"
    try:
        msg = await partner._log_life_activity_to_obsidian(activity_name, status)
        return {"ok": True, "message": msg, "activity_name": activity_name}
    except Exception as e:
        logging.exception(f"study_session error: {e}")
        return {"ok": False, "error": "ライフログ記録に失敗"}
