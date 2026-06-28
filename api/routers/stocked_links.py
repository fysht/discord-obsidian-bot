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
    backup_db_to_drive, set_link_thumbnail, set_link_tags, set_link_cook_dates,
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


async def _generate_tags(title: str, link_type: str, summary: str = "") -> str:
    """タイトル（＋概要）から分類用タグを 1〜3 個、AI（Flash）で生成する。失敗時は空。"""
    from api import app
    import re as _re

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None) or not (title or "").strip():
        return ""
    type_hint = {"recipe": "料理レシピ", "web": "ウェブ記事", "book": "書籍",
                 "map": "場所・店", "youtube": "動画"}.get(link_type, "リンク")
    prompt = (
        f"次の{type_hint}を後から探しやすく分類するための短いタグを1〜3個付けてください。\n"
        "・出力はタグのみ。カンマ区切り。前置き・記号・絵文字・番号は付けない\n"
        "・各タグは2〜6文字程度の名詞\n"
        "・レシピなら『和食/洋食/中華/麺類/作り置き/お菓子/鶏肉』等のジャンルや主材料\n\n"
        f"タイトル: {title}\n" + (f"概要: {summary[:300]}\n" if summary else "")
    )
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        model = await _rgm("link_auto_tag", default_pro=False)
        resp = await bot.gemini_client.aio.models.generate_content(model=model, contents=prompt)
        raw = (resp.text or "").strip()
        parts = [p.strip(" 　#・-・") for p in _re.split(r"[,、\n]", raw) if p.strip()]
        parts = [p for p in parts if p and len(p) <= 12][:3]
        return ",".join(parts)
    except Exception as e:
        logging.debug(f"auto tag generate error: {e}")
        return ""


async def auto_tag_link(link_id: int, overwrite: bool = False) -> str:
    """リンク1件にAIでタグを自動付与する（既存タグがあれば overwrite=False で温存）。
    付与したタグ文字列を返す。背景タスクからもエンドポイントからも使う。"""
    lk = await get_link_by_id(link_id)
    if not lk:
        return ""
    if (lk.get("tags") or "").strip() and not overwrite:
        return lk.get("tags") or ""
    tags = await _generate_tags(lk.get("title") or "", lk.get("type") or "", lk.get("summary") or "")
    if tags:
        await set_link_tags(link_id, tags)
    return tags


class AutoTagRequest(BaseModel):
    overwrite: bool = False


@router.post("/{link_id}/auto_tag", dependencies=[Depends(verify_api_key)])
async def link_auto_tag(link_id: int, req: AutoTagRequest = AutoTagRequest()):
    """リンクにAIでタグを自動付与する（『後から自動でタグを付ける』ボタン用）。"""
    lk = await get_link_by_id(link_id)
    if not lk:
        raise HTTPException(status_code=404, detail="リンクが見つかりません")
    tags = await auto_tag_link(link_id, overwrite=req.overwrite)
    return {"ok": True, "tags": tags}


@router.post("/auto_tag_all", dependencies=[Depends(verify_api_key)])
async def link_auto_tag_all():
    """タグが付いていないリンク全件にAIでタグを自動付与する（一括）。"""
    links = await get_all_links()
    targets = [lk for lk in links if not (lk.get("tags") or "").strip()]
    count = 0
    for lk in targets:
        try:
            tags = await auto_tag_link(lk["id"], overwrite=False)
            if tags:
                count += 1
        except Exception as e:
            logging.debug(f"auto_tag_all error ({lk.get('id')}): {e}")
    return {"ok": True, "tagged": count, "total": len(targets)}


@router.get("", dependencies=[Depends(verify_api_key)])
async def get_links():
    return {"links": await get_all_links()}


async def _fetch_thumbnail_for_link(lk: dict) -> str:
    """リンク種別に応じた最適な方法でサムネイル画像URLを取得する。
    書籍は Google Books、マップは Google Places、それ以外は OGP画像。
    種別固有の取得に失敗したら OGP画像にフォールバックする。"""
    from api.routes import fetch_book_cover, fetch_place_photo, _extract_youtube_id

    url = lk.get("url") or ""
    ltype = lk.get("type") or "web"
    title = (lk.get("title") or "").strip()

    # YouTube は動画サムネをフロントで使うのでサーバー取得は不要
    if _extract_youtube_id(url):
        return ""

    if ltype == "book":
        img = await fetch_book_cover(title)
        if img:
            return img
        return await fetch_og_image(url)
    if ltype == "map":
        # タイトルが場所名（fetch_maps_info 由来）。汎用名なら URL から場所名を引き直す。
        query = title
        if not query or query in ("Google Maps", "Google Maps Location", "Untitled"):
            try:
                from web_parser import fetch_maps_info
                place_name, _ = await fetch_maps_info(url)
                if place_name and place_name != "Google Maps Location":
                    query = place_name
            except Exception:
                pass
        img = await fetch_place_photo(query)
        if img:
            return img
        return await fetch_og_image(url)
    return await fetch_og_image(url)


class ThumbnailRequest(BaseModel):
    refresh: bool = False


@router.post("/{link_id}/thumbnail", dependencies=[Depends(verify_api_key)])
async def link_thumbnail(link_id: int, req: ThumbnailRequest = ThumbnailRequest()):
    """リンクのサムネイル画像をオンデマンドで取得してキャッシュする。
    refresh=True なら既存キャッシュ（取得失敗の __none__ 含む）を無視して取り直す。"""
    lk = await get_link_by_id(link_id)
    if not lk:
        raise HTTPException(status_code=404, detail="リンクが見つかりません")
    cached = (lk.get("thumbnail") or "").strip()
    if cached and not req.refresh:
        return {"ok": True, "thumbnail": "" if cached == "__none__" else cached}
    img = await _fetch_thumbnail_for_link(lk)
    await set_link_thumbnail(link_id, img or "__none__")
    return {"ok": True, "thumbnail": img}


class ThumbnailManualRequest(BaseModel):
    url: str = ""


@router.post("/{link_id}/thumbnail_manual", dependencies=[Depends(verify_api_key)])
async def link_thumbnail_manual(link_id: int, req: ThumbnailManualRequest):
    """サムネイル画像を手動で設定する（画像URLを貼り付け）。空文字でクリア（No Image表示）。"""
    lk = await get_link_by_id(link_id)
    if not lk:
        raise HTTPException(status_code=404, detail="リンクが見つかりません")
    url = (req.url or "").strip()
    if url and not url.lower().startswith(("http://", "https://", "data:image/")):
        raise HTTPException(status_code=400, detail="http(s) の画像URLを指定してください")
    await set_link_thumbnail(link_id, url or "__none__")
    return {"ok": True, "thumbnail": url}


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
    safe_create_task(auto_tag_link(new_id), name="auto-tag-recipe")
    chat_service = getattr(app.state, "chat_service", None)
    if chat_service:
        await sync_link_to_obsidian(chat_service, title, "recipe", url)
    _schedule_db_backup("db-backup-recipe-save")
    meal_name = _re.sub(r"[\[\]|\n]", "", title).strip()[:40]
    return {"ok": True, "link_id": new_id, "title": title, "meal_name": meal_name}


def _normalize_cook_dates(raw: str) -> list[str]:
    """カンマ区切りの予定日文字列を、重複なし・昇順の YYYY-MM-DD リストにする。"""
    import re as _re
    seen, out = set(), []
    for p in (raw or "").replace("、", ",").split(","):
        d = p.strip()
        if _re.match(r"^\d{4}-\d{2}-\d{2}$", d) and d not in seen:
            seen.add(d)
            out.append(d)
    return sorted(out)


class CookDateRequest(BaseModel):
    date: str
    add: bool = True  # True=予定日に追加 / False=削除


@router.post("/{link_id}/cook_date", dependencies=[Depends(verify_api_key)])
async def link_cook_date(link_id: int, req: CookDateRequest):
    """レシピの「作る予定の日」を1件追加/削除する（複数日対応）。食事ログの日と相互連携。"""
    import re as _re
    lk = await get_link_by_id(link_id)
    if not lk:
        raise HTTPException(status_code=404, detail="リンクが見つかりません")
    date = (req.date or "").strip()
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=400, detail="日付は YYYY-MM-DD で指定してください")
    dates = _normalize_cook_dates(lk.get("cook_dates") or "")
    if req.add:
        if date not in dates:
            dates.append(date)
    else:
        dates = [d for d in dates if d != date]
    dates = sorted(set(dates))
    await set_link_cook_dates(link_id, ",".join(dates))
    return {"ok": True, "cook_dates": dates}


class CookDatesSetRequest(BaseModel):
    cook_dates: str = ""  # カンマ区切りでまとめて設定


@router.post("/{link_id}/cook_dates", dependencies=[Depends(verify_api_key)])
async def link_cook_dates_set(link_id: int, req: CookDatesSetRequest):
    """レシピの予定日リストをまとめて設定する（編集モーダルからの一括保存用）。"""
    lk = await get_link_by_id(link_id)
    if not lk:
        raise HTTPException(status_code=404, detail="リンクが見つかりません")
    dates = _normalize_cook_dates(req.cook_dates or "")
    await set_link_cook_dates(link_id, ",".join(dates))
    return {"ok": True, "cook_dates": dates}


@router.post("", dependencies=[Depends(verify_api_key)])
async def create_link(req: LinkCreateRequest):
    """手動でのリンク（レシピ等）追加。"""
    from api import app
    new_id = await add_stocked_link(req.url, req.type, req.title)
    safe_create_task(auto_tag_link(new_id), name="auto-tag-link")

    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, req.title, req.type, req.url)

    _schedule_db_backup("db-backup-link-create")
    return {"status": "success", "link_id": new_id}


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
