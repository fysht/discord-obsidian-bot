"""ゼロ秒思考（Zero Second Thinking）関連エンドポイント。
テーマ候補生成 / 深掘りテーマ / セッション保存 / 開始ログ。"""

import asyncio
import datetime
import json
import logging
import re as _re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/zerosec", tags=["zerosec"])


class ZTThemeRequest(BaseModel):
    context: str = ""


class ZTDeepDiveRequest(BaseModel):
    original_theme: str
    user_memo: str


class ZTSaveRequest(BaseModel):
    theme: str
    memo: str
    session_id: Optional[str] = None


@router.post("/themes", dependencies=[Depends(verify_api_key)])
async def zerosec_themes(req: ZTThemeRequest):
    """ゼロ秒思考のテーマ候補を5つ返す。"""
    from api import app
    from prompts import PROMPT_ZT_THEMES_DETAILED
    from google.genai import types as gtypes
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")
    prompt = PROMPT_ZT_THEMES_DETAILED.replace("{context}", req.context or "（特になし）")
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("zt_themes", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m, contents=prompt,
            config=gtypes.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text or "{}")
        themes = data.get("themes", [])
        if not isinstance(themes, list):
            themes = []
        return {"themes": themes[:5]}
    except Exception as e:
        logging.error(f"zerosec_themes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deep_dive", dependencies=[Depends(verify_api_key)])
async def zerosec_deep_dive(req: ZTDeepDiveRequest):
    """ユーザーが書いたメモから、深掘り用の追加テーマを5つ生成する。"""
    from api import app
    from prompts import PROMPT_ZT_DEEP_DIVE
    from google.genai import types as gtypes
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")
    prompt = (PROMPT_ZT_DEEP_DIVE
              .replace("{original_theme}", req.original_theme or "")
              .replace("{user_memo}", req.user_memo or ""))
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("zt_deep_dive", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m, contents=prompt,
            config=gtypes.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text or "{}")
        themes = data.get("themes", [])
        if not isinstance(themes, list):
            themes = []
        return {"themes": themes[:5]}
    except Exception as e:
        logging.error(f"zerosec_deep_dive error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save", dependencies=[Depends(verify_api_key)])
async def zerosec_save(req: ZTSaveRequest):
    """ゼロ秒思考のメモをノートに保存し、ライフログにも記録する。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Drive未接続")
    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")
    theme = (req.theme or "").strip() or "無題のテーマ"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")
    folder_id = await chat_service.drive_service.find_file(
        service, chat_service.drive_folder_id, "ZeroSecondThinking"
    )
    if not folder_id:
        folder_id = await chat_service.drive_service.create_folder(
            service, chat_service.drive_folder_id, "ZeroSecondThinking"
        )
    now = datetime.datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    session_id = req.session_id or now.strftime("%Y%m%d%H%M%S")
    safe_theme = _re.sub(r'[\\/*?:"<>|]', "", theme)[:60]
    file_name = f"{today_str}_{session_id}_{safe_theme}.md"
    existing_id = None
    if req.session_id:
        try:
            query = (f"'{folder_id}' in parents and trashed=false "
                     f"and name contains '{session_id}'")
            results = await asyncio.to_thread(
                lambda: service.files().list(q=query, fields="files(id, name)").execute()
            )
            files = results.get("files", [])
            if files:
                existing_id = files[0]["id"]
                file_name = files[0]["name"]
        except Exception as e:
            logging.debug(f"zt existing file lookup: {e}")
    formatted_memo = memo.replace("\n", "\n> ")
    section_block = f"\n## 🧠 {time_str} {theme}\n\n> {formatted_memo}\n"
    if existing_id:
        existing = await chat_service.drive_service.read_text_file(service, existing_id)
        new_content = (existing or "").rstrip() + "\n" + section_block
        await chat_service.drive_service.update_text(service, existing_id, new_content)
    else:
        header = (
            f"---\ntitle: ゼロ秒思考 {today_str} {time_str}\n"
            f"date: {today_str}\ntags: [zero_second_thinking]\n---\n\n"
            f"# ゼロ秒思考セッション ({today_str} {time_str})\n"
            + section_block
        )
        await chat_service.drive_service.upload_text(service, folder_id, file_name, header)
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if partner_cog:
        try:
            await partner_cog._log_life_activity_to_obsidian(
                f"ゼロ秒思考: {theme[:30]}", "end"
            )
        except Exception as e:
            logging.debug(f"zt lifelog error: {e}")
    return {"status": "success", "session_id": session_id, "message": f"「{theme}」のメモを保存したよ。"}


@router.post("/log_start", dependencies=[Depends(verify_api_key)])
async def zerosec_log_start(req: ZTThemeRequest):
    """ゼロ秒思考の開始をライフログに記録する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if not partner_cog:
        return {"status": "skipped"}
    try:
        theme = (req.context or "テーマ未指定")[:30]
        await partner_cog._log_life_activity_to_obsidian(f"ゼロ秒思考: {theme}", "start")
    except Exception as e:
        logging.debug(f"zt start log: {e}")
    return {"status": "success"}
