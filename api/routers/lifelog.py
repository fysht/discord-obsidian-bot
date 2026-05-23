"""ライフログ + 思考の壁打ち + 朝のMIT提案。"""

import datetime
import json as _json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.database import get_questions_by_date, resolve_questions
from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["lifelog"])


class ThoughtReflectionRequest(BaseModel):
    theme: str
    summary: str = ""
    next_step: str = ""


class LifelogActivityRequest(BaseModel):
    activity_name: str
    status: str  # 'start' / 'end'


class MorningMitConfirmRequest(BaseModel):
    items: list[str]
    qid: Optional[int] = None


@router.post("/thought_reflection", dependencies=[Depends(verify_api_key)])
async def thought_reflection_save(req: ThoughtReflectionRequest):
    """壁打ちメモを Obsidian に保存する（ボタン経由のみで呼ばれる想定）。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジン未初期化")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 未ロード")
    try:
        msg = await partner_cog._save_thought_reflection_to_obsidian(
            req.theme or "無題",
            req.summary or "",
            req.next_step or "",
        )
        return {"ok": True, "message": msg}
    except Exception as e:
        logging.error(f"thought_reflection_save error: {e}")
        return {"ok": False, "error": "保存失敗"}


@router.post("/lifelog_activity", dependencies=[Depends(verify_api_key)])
async def lifelog_activity(req: LifelogActivityRequest):
    """`- HH:MM ▶ 活動名` 開始 → `- HH:MM - HH:MM 活動名` 終了 の標準形で記録。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジン未初期化")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 未ロード")
    status = req.status.strip().lower()
    if status not in ("start", "end"):
        return {"ok": False, "error": "status は 'start' か 'end'"}
    try:
        msg = await partner_cog._log_life_activity_to_obsidian(req.activity_name, status)
        return {"ok": True, "message": msg}
    except Exception as e:
        logging.error(f"lifelog_activity error: {e}")
        return {"ok": False, "error": "保存失敗"}


@router.get("/morning_mit/pending", dependencies=[Depends(verify_api_key)])
async def morning_mit_pending():
    """今日の朝のMIT提案で未確定（resolved以外）のものを返す。"""
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        qs = await get_questions_by_date(today_str, scope='morning_mit')
        unresolved = [q for q in qs if q["status"] != 'resolved']
        if not unresolved:
            return {"date": today_str, "qid": None, "candidates": []}
        q = unresolved[0]
        cands = []
        try:
            ctx = _json.loads(q.get("context") or "{}")
            cands = ctx.get("candidates") or []
        except Exception:
            cands = []
        return {"date": today_str, "qid": q["id"], "candidates": cands}
    except Exception as e:
        logging.error(f"morning_mit_pending error: {e}")
        return {"date": today_str, "qid": None, "candidates": []}


@router.post("/morning_mit/confirm", dependencies=[Depends(verify_api_key)])
async def morning_mit_confirm(req: MorningMitConfirmRequest):
    """ユーザーが朝のMIT候補を編集して確定したものをObsidianに書き込む。"""
    from api import app
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    items = [s.strip() for s in (req.items or []) if s and s.strip()][:3]
    if not items:
        return {"ok": False, "error": "MIT が空です"}
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジン未初期化")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 未ロード")
    try:
        result_msg = await partner_cog._set_mit_to_obsidian(items)
        try:
            await resolve_questions(today_str, scope='morning_mit')
        except Exception:
            pass
        return {"ok": True, "message": result_msg, "date": today_str}
    except Exception as e:
        logging.error(f"morning_mit_confirm error: {e}")
        return {"ok": False, "error": "保存に失敗"}
