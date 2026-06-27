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
    backup_db_to_drive, set_link_thumbnail,
)
from api.routes import verify_api_key, sync_link_to_obsidian, fetch_og_image
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


@router.post("/{link_id}/thumbnail", dependencies=[Depends(verify_api_key)])
async def link_thumbnail(link_id: int):
    """リンクのサムネイル（OGP画像）をオンデマンドで取得してキャッシュする。
    一度取得すれば次回からは即返す。画像が無いページは再取得しないよう印を付ける。"""
    lk = await get_link_by_id(link_id)
    if not lk:
        raise HTTPException(status_code=404, detail="リンクが見つかりません")
    cached = (lk.get("thumbnail") or "").strip()
    if cached:
        return {"ok": True, "thumbnail": "" if cached == "__none__" else cached}
    img = await fetch_og_image(lk.get("url") or "")
    await set_link_thumbnail(link_id, img or "__none__")
    return {"ok": True, "thumbnail": img}


class RecipeSaveRequest(BaseModel):
    video_id: str = ""
    link_id: int | None = None


@router.post("/save_as_recipe", dependencies=[Depends(verify_api_key)])
async def save_as_recipe(req: RecipeSaveRequest):
    """共有した YouTube / ウェブのリンクを「レシピ」として保存する
    （レシピのストックリンク＋Recipes ノートを作成）。"""
    from api import app
    import re as _re

    url = ""
    title = ""
    if req.video_id:
        from api.database import youtube_get_video
        v = await youtube_get_video(req.video_id)
        if not v:
            raise HTTPException(status_code=404, detail="動画が見つかりません")
        url = v.get("url") or f"https://www.youtube.com/watch?v={req.video_id}"
        title = v.get("title") or "レシピ動画"
    elif req.link_id:
        lk = await get_link_by_id(req.link_id)
        if not lk:
            raise HTTPException(status_code=404, detail="リンクが見つかりません")
        url = lk.get("url") or ""
        title = lk.get("title") or "レシピ"
    else:
        raise HTTPException(status_code=400, detail="video_id か link_id が必要です")

    new_id = await add_stocked_link(url, "recipe", title)
    chat_service = getattr(app.state, "chat_service", None)
    if chat_service:
        await sync_link_to_obsidian(chat_service, title, "recipe", url)
    _schedule_db_backup("db-backup-recipe-save")
    meal_name = _re.sub(r"[\[\]|\n]", "", title).strip()[:40]
    return {"ok": True, "link_id": new_id, "title": title, "meal_name": meal_name}


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
