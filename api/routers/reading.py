"""読書機能（書籍一覧 / 読書メモ保存 / マネージャー問いかけ / ログ / 多読プラン）。"""

import asyncio
import logging
import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.database import get_all_links, get_reading_plan, update_reading_plan
from api.routes import verify_api_key

router = APIRouter(prefix="/reading", tags=["reading"])


class ReadingMemoRequest(BaseModel):
    book_title: str
    memo: str


class ReadingPromptRequest(BaseModel):
    book_title: str
    previous_prompts: List[str] = []
    current_pass: str = ""


class ReadingPlanRequest(BaseModel):
    book_title: str
    passes: List[dict] = []


@router.get("/books", dependencies=[Depends(verify_api_key)])
async def reading_books():
    """読書候補となる書籍一覧を返す。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)

    books_from_links = []
    try:
        links = await get_all_links()
        for link in links:
            if link.get("type") == "book":
                books_from_links.append({
                    "title": link.get("title", "Untitled"),
                    "source": "stock",
                    "link_id": link.get("id"),
                    "url": link.get("url", ""),
                })
    except Exception as e:
        logging.debug(f"links fetch error: {e}")

    books_from_notes = []
    if chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            if service:
                folder_id = await chat_service.drive_service.find_file(
                    service, chat_service.drive_folder_id, "BookNotes"
                )
                if folder_id:
                    query = f"'{folder_id}' in parents and mimeType='text/markdown' and trashed=false"
                    results = await asyncio.to_thread(
                        lambda: service.files().list(
                            q=query, fields="files(id, name, modifiedTime)",
                            orderBy="modifiedTime desc", pageSize=30
                        ).execute()
                    )
                    for f in results.get("files", []):
                        title = f["name"].replace(".md", "")
                        if not any(b["title"] == title for b in books_from_links):
                            books_from_notes.append({
                                "title": title,
                                "source": "notes",
                                "link_id": None,
                                "url": "",
                            })
        except Exception as e:
            logging.debug(f"book notes fetch: {e}")

    return {"books": books_from_links + books_from_notes}


@router.post("/save", dependencies=[Depends(verify_api_key)])
async def reading_save(req: ReadingMemoRequest):
    """読書メモを書籍ノートに保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    book_cog = bot.get_cog("BookCog") if bot else None
    if not book_cog:
        raise HTTPException(status_code=503, detail="BookCog不在")
    title = (req.book_title or "").strip() or "無題の書籍"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")
    ok = await book_cog.append_book_memo(title, memo)
    if not ok:
        raise HTTPException(status_code=500, detail="保存に失敗しました")
    return {"status": "success", "message": f"「{title}」のノートに保存したよ。"}


@router.post("/prompt", dependencies=[Depends(verify_api_key)])
async def reading_prompt(req: ReadingPromptRequest):
    """読書中のマネージャーからの問いかけを生成する。"""
    from api import app
    from prompts import PROMPT_BOOK_READING_PROMPT
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")
    prev = "\n".join(f"- {p}" for p in (req.previous_prompts or [])) or "（まだなし）"
    prompt = PROMPT_BOOK_READING_PROMPT.replace(
        "{book_title}", req.book_title or "無題"
    ).replace("{previous_prompts}", prev).replace(
        "{current_pass}", req.current_pass or "（指定なし）"
    )
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("book_prompt", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(model=_m, contents=prompt)
        return {"prompt": (response.text or "").strip()}
    except Exception as e:
        logging.error(f"reading_prompt error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/log", dependencies=[Depends(verify_api_key)])
async def reading_log(book_title: str):
    """書籍ノートに蓄積された過去の読書メモ（Reading Log セクション）を返す。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    title = (book_title or "").strip()
    log_text = ""
    if title and chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            if service:
                folder_id = await chat_service.drive_service.find_file(
                    service, chat_service.drive_folder_id, "BookNotes"
                )
                if folder_id:
                    f_id = await chat_service.drive_service.find_file(
                        service, folder_id, f"{title}.md"
                    )
                    if f_id:
                        content = await chat_service.drive_service.read_text_file(service, f_id)
                        m = re.search(r"## 📖 Reading Log\n(.*?)(?=\n## |\Z)", content or "", re.DOTALL)
                        if m:
                            log_text = m.group(1).strip()
        except Exception as e:
            logging.debug(f"reading_log fetch error: {e}")
    return {"book_title": title, "log": log_text}


@router.get("/plan", dependencies=[Depends(verify_api_key)])
async def reading_plan_get(book_title: str):
    """書籍の多読プラン（段階リスト）を返す。"""
    passes = await get_reading_plan(book_title)
    return {"book_title": (book_title or "").strip(), "passes": passes}


@router.put("/plan", dependencies=[Depends(verify_api_key)])
async def reading_plan_put(req: ReadingPlanRequest):
    """書籍の多読プランを保存する。"""
    title = (req.book_title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="book_titleが空です")
    await update_reading_plan(title, req.passes)
    return {"status": "success", "book_title": title, "passes": req.passes}
