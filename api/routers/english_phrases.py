"""英語フレーズ帳（English Phrases）関連エンドポイント。
保存・一括保存・一覧・削除・4択クイズ出題・回答記録・日本語→英訳保存。"""

import datetime
import logging
import random
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.database import (
    get_english_phrases, add_english_phrase, delete_english_phrase,
    get_quiz_phrase_pool, record_quiz_attempt,
)
from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/english_phrases", tags=["english"])


class PhraseSaveRequest(BaseModel):
    phrase: str
    translation: str = ""
    context: str = ""


class PhraseBulkItem(BaseModel):
    phrase: str
    translation: str = ""
    context: str = ""


class PhraseBulkRequest(BaseModel):
    phrases: List[PhraseBulkItem]


class QuizAnswerRequest(BaseModel):
    phrase_id: int
    correct: bool


class TranslateSaveRequest(BaseModel):
    text: str


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_english_phrases():
    phrases = await get_english_phrases()
    return {"phrases": phrases}


@router.post("", dependencies=[Depends(verify_api_key)])
async def save_english_phrase(req: PhraseSaveRequest):
    phrase_id = await add_english_phrase(
        req.phrase.strip(), req.translation.strip(), req.context.strip()
    )
    return {"id": phrase_id}


@router.post("/bulk", dependencies=[Depends(verify_api_key)])
async def save_english_phrases_bulk(req: PhraseBulkRequest):
    """複数の英語フレーズを一括保存する。長押しメッセージから複数文を選択保存する用途。"""
    saved_ids = []
    for item in req.phrases:
        phrase = (item.phrase or "").strip()
        if not phrase:
            continue
        pid = await add_english_phrase(
            phrase,
            (item.translation or "").strip(),
            (item.context or "").strip(),
        )
        if pid:
            saved_ids.append(pid)
    return {"saved": len(saved_ids), "ids": saved_ids}


@router.delete("/{phrase_id}", dependencies=[Depends(verify_api_key)])
async def remove_english_phrase(phrase_id: int):
    deleted = await delete_english_phrase(phrase_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="フレーズが見つかりません")
    return {"deleted": True}


@router.get("/quiz", dependencies=[Depends(verify_api_key)])
async def english_phrases_quiz():
    """正解率の低いフレーズを優先して 1 問返す。"""
    pool = await get_quiz_phrase_pool()
    if not pool:
        raise HTTPException(status_code=404, detail="フレーズが登録されていません")

    now = datetime.datetime.now(JST)

    def priority(p: dict) -> float:
        attempts = p.get("attempt_count") or 0
        correct = p.get("correct_count") or 0
        if attempts == 0:
            return 999.0
        rate = correct / attempts
        days_since = 0.0
        last = p.get("last_attempted_at")
        if last:
            try:
                last_dt = datetime.datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=JST)
                days_since = (now - last_dt).total_seconds() / 86400.0
            except Exception:
                pass
        return (1.0 - rate) * 0.7 + min(days_since, 30.0) / 30.0 * 0.3

    pool_sorted = sorted(pool, key=priority, reverse=True)
    top = pool_sorted[: min(8, len(pool_sorted))]
    chosen = random.choice(top)
    distractors = [p["phrase"] for p in pool if p["id"] != chosen["id"] and p.get("phrase")]
    random.shuffle(distractors)
    options = [chosen["phrase"]] + distractors[:3]
    random.shuffle(options)
    return {
        "id": chosen["id"],
        "phrase": chosen["phrase"],
        "translation": chosen.get("translation", ""),
        "context": chosen.get("context", ""),
        "options": options,
        "attempt_count": chosen.get("attempt_count", 0),
        "correct_count": chosen.get("correct_count", 0),
    }


@router.post("/answer", dependencies=[Depends(verify_api_key)])
async def english_phrases_answer(req: QuizAnswerRequest):
    """クイズの正解/不正解を記録する。"""
    ok = await record_quiz_attempt(req.phrase_id, req.correct)
    if not ok:
        raise HTTPException(status_code=404, detail="フレーズが見つかりません")
    return {"status": "success"}


@router.post("/translate_and_save", dependencies=[Depends(verify_api_key)])
async def translate_and_save_phrase(req: TranslateSaveRequest):
    """ユーザーのテキスト（日本語）を英訳してフレーズ帳に保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.gemini_client:
        raise HTTPException(status_code=503, detail="AIサービス未接続")
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("english_translate", default_pro=False)
        resp = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=(
                "Translate the following Japanese text to natural, everyday English. "
                "Output only the English translation.\n\n"
                + req.text
            ),
        )
        translation = resp.text.strip()
    except Exception as e:
        logging.error(f"translate_and_save_phrase error: {e}")
        raise HTTPException(status_code=500, detail=f"翻訳に失敗しました: {e}")

    phrase_id = await add_english_phrase(translation, req.text, req.text[:300])
    return {"id": phrase_id, "phrase": translation, "translation": req.text}
