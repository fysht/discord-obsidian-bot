"""MIT (Most Important Tasks) + Daily Journal 関連エンドポイント。"""

import datetime
import logging
import re as _re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["mit"])


class MitSetRequest(BaseModel):
    items: List[str]


class MitToggleRequest(BaseModel):
    index: int


class DailyJournalUpdate(BaseModel):
    text: str


@router.post("/mit_set", dependencies=[Depends(verify_api_key)])
async def mit_set(req: MitSetRequest):
    """今日の MIT を DailyNote の `## 🎯 MIT` セクションに書き込む。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    msg = await partner_cog._set_mit_to_obsidian(req.items)
    return {"status": "success", "message": msg}


@router.get("/mit_get", dependencies=[Depends(verify_api_key)])
async def mit_get():
    """今日の MIT のみを軽量に取得する。設定モーダルの初期値表示に使う。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"items": []}
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            return {"items": []}
        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if not f_id:
            return {"items": []}
        content = await chat_service.drive_service.read_text_file(service, f_id)
        m = _re.search(r"## 🎯 MIT\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        if not m:
            return {"items": []}
        items = []
        for line in m.group(1).splitlines():
            line = line.strip()
            mm = _re.match(r"-\s*\[([ xX])\]\s*(.+)$", line)
            if mm:
                items.append({"text": mm.group(2).strip(), "done": mm.group(1).lower() == "x"})
        return {"items": items}
    except Exception as e:
        logging.debug(f"mit_get error: {e}")
        return {"items": []}


@router.post("/mit_rollover", dependencies=[Depends(verify_api_key)])
async def mit_rollover():
    """今日の未達 MIT を翌日に持ち越す。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    msg = await partner_cog._rollover_mit()
    return {"status": "success", "message": msg}


@router.post("/mit_toggle", dependencies=[Depends(verify_api_key)])
async def mit_toggle(req: MitToggleRequest):
    """今日のMITの `index` 番目（0始まり）の完了/未完了をトグルする。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    result = await partner_cog._toggle_mit_in_obsidian(req.index)
    if result.get("status") != "success":
        raise HTTPException(status_code=400, detail=result.get("message", "MIT toggle 失敗"))
    return result


@router.get("/daily_journal", dependencies=[Depends(verify_api_key)])
async def daily_journal_get():
    """今日の日記（## 📔 Daily Journal セクション）の本文を返す。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"text": ""}
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            return {"text": ""}
        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if not f_id:
            return {"text": ""}
        content = await chat_service.drive_service.read_text_file(service, f_id)
        m = _re.search(r"## 📔 Daily Journal\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        return {"text": m.group(1).strip() if m else ""}
    except Exception as e:
        logging.debug(f"daily_journal_get error: {e}")
        return {"text": ""}


@router.post("/daily_journal", dependencies=[Depends(verify_api_key)])
async def daily_journal_set(req: DailyJournalUpdate):
    """今日の日記（## 📔 Daily Journal セクション）を上書き保存する（Obsidian反映）。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Drive サービス未設定")
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            folder_id = await chat_service.drive_service.create_folder(
                service, chat_service.drive_folder_id, "DailyNotes"
            )
        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if f_id:
            content = await chat_service.drive_service.read_text_file(service, f_id)
        else:
            content = f"---\ndate: {today_str}\n---\n\n# Daily Note {today_str}\n"

        new_text = (req.text or "").strip()
        section_header = "## 📔 Daily Journal"
        pattern = _re.compile(rf"{_re.escape(section_header)}\n.*?(?=\n## |\Z)", _re.DOTALL)
        replacement = f"{section_header}\n{new_text}" if new_text else section_header
        if pattern.search(content):
            new_content = pattern.sub(replacement, content, count=1)
        else:
            from utils.obsidian_utils import update_section
            new_content = update_section(content, new_text, section_header)

        if f_id:
            await chat_service.drive_service.update_text(service, f_id, new_content)
        else:
            await chat_service.drive_service.upload_text(
                service, folder_id, f"{today_str}.md", new_content
            )
        return {"status": "success"}
    except Exception as e:
        logging.error(f"daily_journal_set error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
