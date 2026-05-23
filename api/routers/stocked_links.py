"""ストックリンク（Web/YouTube/レシピ/マップ/書籍）関連エンドポイント。

CRUD + 一括既読化。ヘルパー sync_link_to_obsidian / DB 関数は routes.py や api.database から共有利用。
"""

import datetime
import logging
import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.database import (
    add_stocked_link, get_all_links, get_link_by_id,
    update_link_details, mark_link_as_saved, delete_stocked_link,
    backup_db_to_drive,
)
from api.routes import verify_api_key, sync_link_to_obsidian
from utils.async_utils import safe_create_task

router = APIRouter(prefix="/links", tags=["links"])


class LinkCreateRequest(BaseModel):
    title: str = "Untitled"
    url: str = ""
    type: str = "web"


class LinkUpdateRequest(BaseModel):
    title: str = ""
    purpose: str = ""
    summary: str = ""
    memo: str = ""
    target_date: str = ""
    linked_note_url: str = ""
    type: str = ""
    add_to_calendar: bool = False
    tags: str = ""


class LinkBulkStatusRequest(BaseModel):
    link_ids: List[int]
    status: str = "saved"


def _schedule_db_backup(name: str):
    """DB 変更後にバックアップタスクを起動する（fire-and-forget）。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if bot and getattr(bot, "drive_service", None):
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name=name,
        )


@router.get("", dependencies=[Depends(verify_api_key)])
async def get_links():
    return {"links": await get_all_links()}


@router.post("", dependencies=[Depends(verify_api_key)])
async def create_link(req: LinkCreateRequest):
    """手動でのリンク（レシピ等）追加。"""
    from api import app
    await add_stocked_link(req.url, req.type, req.title)

    links = await get_all_links()
    if not links:
        raise HTTPException(status_code=500)
    new_link = links[0]

    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, req.title, req.type, req.url)

    _schedule_db_backup("db-backup-link-create")
    return {"status": "success", "link_id": new_link["id"]}


@router.put("/{link_id}", dependencies=[Depends(verify_api_key)])
async def update_link(link_id: int, req: LinkUpdateRequest):
    from api import app

    link = await get_link_by_id(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="リンク未検出")

    old_title = link["title"] or ""
    new_title = req.title or old_title
    new_type = req.type or link["type"]
    existing_cal_event_id = link.get("calendar_event_id", "")

    # カレンダー処理（重複防止）
    new_cal_event_id = existing_cal_event_id
    if req.add_to_calendar and req.target_date:
        bot = getattr(app.state, "bot", None)
        if bot and bot.calendar_service:
            prefix = {"map": "🗺️[行]", "recipe": "🍳[食]", "book": "📚[本]"}.get(new_type, "📎[記]")
            cal_body = {
                "summary": f"{prefix} {new_title}",
                "description": f"目的: {req.purpose}\nメモ: {req.memo}\nURL: {link['url']}",
                "start": {"date": req.target_date},
                "end": {
                    "date": (
                        datetime.datetime.strptime(req.target_date, "%Y-%m-%d")
                        + datetime.timedelta(days=1)
                    ).strftime("%Y-%m-%d")
                },
            }
            try:
                cal_svc = bot.calendar_service.get_service()
                if existing_cal_event_id:
                    cal_svc.events().update(
                        calendarId="primary", eventId=existing_cal_event_id, body=cal_body
                    ).execute()
                else:
                    result = cal_svc.events().insert(
                        calendarId="primary", body=cal_body
                    ).execute()
                    new_cal_event_id = result.get("id", "")
            except Exception as e:
                logging.warning(f"link calendar add/update failed: {e}")

    await update_link_details(
        link_id, new_title, req.purpose, req.summary, req.memo, req.target_date,
        req.linked_note_url, new_type, req.tags, new_cal_event_id,
    )

    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(
        chat_service, new_title, new_type, link["url"],
        req.purpose, req.target_date, req.memo, req.summary,
        is_update=True, old_title=old_title,
    )

    _schedule_db_backup("db-backup-link-update")
    return {"status": "success"}


@router.delete("/{link_id}", dependencies=[Depends(verify_api_key)])
async def delete_link(link_id: int):
    await delete_stocked_link(link_id)
    _schedule_db_backup("db-backup-link-delete")
    return {"status": "success"}


@router.post("/bulk_status", dependencies=[Depends(verify_api_key)])
async def bulk_update_link_status(req: LinkBulkStatusRequest):
    """複数リンクのステータスを一括更新する。"""
    if not req.link_ids:
        return {"status": "success", "updated": 0}
    for lid in req.link_ids:
        try:
            await mark_link_as_saved(lid)
        except Exception as e:
            logging.warning(f"bulk_status update {lid} failed: {e}")
    _schedule_db_backup("db-backup-bulk-status")
    return {"status": "success", "updated": len(req.link_ids)}
