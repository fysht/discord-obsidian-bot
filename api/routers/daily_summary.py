"""デイリーサマリー & マネージャー質問（daily_summary / daily_questions）関連エンドポイント。

Obsidian 保存ヘルパー (_save_*_to_obsidian / _generate_daily_summary) は
api.routes に残置（dailysummary_cog / partner_routine_cog からも import されているため）。
"""

import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import notification_service
from api.database import (
    add_daily_question, answer_daily_question, delete_daily_question,
    get_pending_questions, get_questions_by_date, resolve_questions,
)
from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["daily"])


class DailySummaryUpdate(BaseModel):
    text: str
    date: Optional[str] = None


class DailySummaryGenerateRequest(BaseModel):
    date: Optional[str] = None
    answers: Optional[dict] = None
    finalize: bool = False  # True なら質問が無くてもそのまま Obsidian に保存


class DailyAnswerRequest(BaseModel):
    answer: str


async def _read_summary_for_date(chat_service, date: str) -> str:
    """指定日の Daily Note から `## 📅 Daily Summary` の本文を読み取って返す。"""
    import re as _re
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            return ""
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{date}.md")
        if not f_id:
            return ""
        content = await chat_service.drive_service.read_text_file(service, f_id)
        m = _re.search(r"## 📅 Daily Summary\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        if m:
            return m.group(1).strip()
    except Exception as e:
        logging.debug(f"_read_summary_for_date error ({date}): {e}")
    return ""


@router.post("/daily_summary", dependencies=[Depends(verify_api_key)])
async def daily_summary_set(req: DailySummaryUpdate):
    """ユーザーが手動で編集したデイリーサマリーを Obsidian へ保存する。"""
    from api.routes import _save_daily_summary_to_obsidian, _save_manager_qa_to_obsidian
    date_str = req.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        saved = await _save_daily_summary_to_obsidian(date_str, req.text or "")
        if saved:
            await _save_manager_qa_to_obsidian(date_str)
            await resolve_questions(date_str, scope='summary')
            return {"ok": True, "saved": True, "date": date_str}
        raise HTTPException(status_code=500, detail="保存に失敗しました")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"daily_summary_set error: {e}")
        raise HTTPException(status_code=500, detail=f"保存に失敗しました: {e}")


@router.get("/daily_summary", dependencies=[Depends(verify_api_key)])
async def daily_summary_get(date: str = ""):
    """指定日のデイリーサマリーを返す。
    date 未指定時は今日のものが無ければ昨日のものへ自動フォールバック。"""
    from api import app
    explicit_date = bool(date)
    if not date:
        date = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"text": "", "questions": [], "fallback": False, "date": date}
    try:
        text = await _read_summary_for_date(chat_service, date)
        fallback_date = None
        if not text and not explicit_date:
            # 最新の保存分を「デイリーノート」「マネージャーの気づき」と同様に
            # できるだけ広い範囲で探す（過去 30 日まで遡る）
            base = datetime.datetime.strptime(date, "%Y-%m-%d").date()
            for offset in range(1, 31):
                prev = (base - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
                t = await _read_summary_for_date(chat_service, prev)
                if t:
                    text = t
                    fallback_date = prev
                    break
        actual_date = fallback_date or date
        questions = await get_questions_by_date(actual_date, scope='summary')
        return {
            "date": actual_date,
            "requested_date": date,
            "text": text,
            "questions": questions,
            "fallback": fallback_date is not None,
        }
    except Exception as e:
        logging.debug(f"daily_summary_get error: {e}")
        return {"text": "", "questions": [], "fallback": False, "date": date}


@router.post("/daily_summary/generate", dependencies=[Depends(verify_api_key)])
async def daily_summary_generate(req: DailySummaryGenerateRequest):
    """サマリーを生成。質問がある場合は DB に登録して返す。
    finalize=True または質問が空の場合は Obsidian に保存して質問を resolved にする。"""
    from api.routes import (
        _generate_daily_summary, _save_daily_summary_to_obsidian, _save_manager_qa_to_obsidian,
    )
    date_str = req.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")

    if req.answers:
        for qid_str, ans in req.answers.items():
            if not ans:
                continue
            try:
                qid = int(qid_str)
            except (TypeError, ValueError):
                continue
            await answer_daily_question(qid, str(ans))

    result = await _generate_daily_summary(date_str, answers=req.answers)
    summary = result.get("summary", "")
    new_questions = result.get("questions", [])

    existing = await get_questions_by_date(date_str, scope='summary')
    existing_texts = {q["question"].strip() for q in existing}
    for q in new_questions:
        if q.strip() in existing_texts:
            continue
        await add_daily_question(date_str, q.strip(), scope='summary')

    pending = await get_questions_by_date(date_str, scope='summary')
    unanswered = [q for q in pending if q["status"] in ("pending",)]
    will_finalize = req.finalize or (not new_questions and not unanswered)

    saved = False
    if summary and will_finalize:
        saved = await _save_daily_summary_to_obsidian(date_str, summary)
        if saved:
            await _save_manager_qa_to_obsidian(date_str)
            await resolve_questions(date_str, scope='summary')

    return {
        "date": date_str,
        "summary": summary,
        "questions": await get_questions_by_date(date_str, scope='summary'),
        "saved": saved,
        "error": result.get("error"),
    }


@router.get("/daily_questions/pending", dependencies=[Depends(verify_api_key)])
async def daily_questions_pending():
    """未回答の質問一覧。"""
    qs = await get_pending_questions()
    return {"questions": qs}


@router.post("/daily_questions/{qid}/answer", dependencies=[Depends(verify_api_key)])
async def daily_questions_answer(qid: int, req: DailyAnswerRequest):
    ok = await answer_daily_question(qid, req.answer)
    if not ok:
        raise HTTPException(status_code=404, detail="質問が見つかりません")

    # morning_mit スコープの質問への回答は、そのまま今日の MIT として
    # Obsidian の DailyNote に確定書き込みする
    try:
        pending = await get_pending_questions()
        q = next((x for x in pending if x.get("id") == qid), None)
        if q and q.get("scope") == "morning_mit":
            import re as _re
            lines = []
            for ln in (req.answer or "").splitlines():
                ln = _re.sub(r"^\s*[0-9]+[.)、]\s*", "", ln).strip()
                ln = _re.sub(r"^[-・*]\s*", "", ln).strip()
                if ln:
                    lines.append(ln[:60])
            items = lines[:3]
            if items:
                from api import app
                bot = getattr(app.state, "bot", None)
                partner_cog = bot.get_cog("PartnerCog") if bot else None
                if partner_cog:
                    result_msg = await partner_cog._set_mit_to_obsidian(items)
                    await resolve_questions(q["date"], scope="morning_mit")
                    confirm = (
                        "📌 今日のMITを確定したよ！\n"
                        + "\n".join(f"{i}. {it}" for i, it in enumerate(items, 1))
                        + "\n\n" + (result_msg or "")
                    ).strip()
                    await notification_service.save_message_and_notify(
                        "assistant", confirm, title="📌 今日のMIT確定",
                    )
    except Exception as e:
        logging.error(f"morning_mit answer 反映エラー: {e}")

    # summary スコープの質問に答え終わったら自動で Obsidian へ確定保存する。
    # （旧仕様では「✓ 確定保存」を押すまで保存されなかったため、
    #   UI 上「保存済み（編集可）」と見えても振り返りカードは更新されない問題があった）
    auto_finalized = False
    try:
        target_q = await _find_question_by_id(qid)
        if target_q and target_q.get("scope") == "summary":
            date_for_q = target_q["date"]
            all_qs = await get_questions_by_date(date_for_q, scope="summary")
            still_pending = [q for q in all_qs if q.get("status") == "pending"]
            if not still_pending:
                # 回答を {qid: answer} の dict にして再生成 → 保存
                answers_map = {str(q["id"]): q.get("answer") or "" for q in all_qs}
                auto_finalized = await _auto_finalize_summary(date_for_q, answers_map)
    except Exception as e:
        logging.error(f"summary auto-finalize エラー: {e}")

    return {"status": "success", "auto_finalized": auto_finalized}


async def _find_question_by_id(qid: int):
    """指定 ID の質問（resolved 含む）を取得する小ヘルパー。
    現状の DB API には `get question by id` がないので、pending と直近の
    summary scope から探す。"""
    for q in await get_pending_questions():
        if q.get("id") == qid:
            return q
    today = datetime.datetime.now(JST).date()
    for offset in range(0, 3):
        d = (today - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
        for q in await get_questions_by_date(d, scope="summary"):
            if q.get("id") == qid:
                return q
    return None


async def _auto_finalize_summary(date_str: str, answers_map: dict) -> bool:
    """summary 質問が全て答え終わったら呼ぶ：サマリー再生成 → Obsidian 保存 → resolved 化。"""
    from api.routes import (
        _generate_daily_summary, _save_daily_summary_to_obsidian, _save_manager_qa_to_obsidian,
    )
    try:
        result = await _generate_daily_summary(date_str, answers=answers_map)
    except Exception as e:
        logging.error(f"auto finalize generate エラー: {e}")
        return False
    summary = (result.get("summary") or "").strip()
    if not summary:
        return False
    saved = await _save_daily_summary_to_obsidian(date_str, summary)
    if saved:
        await _save_manager_qa_to_obsidian(date_str)
        await resolve_questions(date_str, scope="summary")
        try:
            await notification_service.save_message_and_notify(
                "assistant",
                "今日のデイリーサマリーをまとめてObsidianに保存したよ📅 アプリの『ログ → 今日の振り返り』から見れるよ🌙",
                title="📅 デイリーサマリー保存",
            )
        except Exception as e:
            logging.debug(f"auto finalize notify エラー: {e}")
    return saved


@router.delete("/daily_questions/{qid}", dependencies=[Depends(verify_api_key)])
async def daily_questions_delete(qid: int):
    ok = await delete_daily_question(qid)
    if not ok:
        raise HTTPException(status_code=404, detail="質問が見つかりません")
    return {"status": "success"}
