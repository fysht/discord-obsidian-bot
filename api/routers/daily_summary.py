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
    """未回答の質問一覧。各質問に scope レジストリ由来の選択チップ(chips)を付与する。"""
    import json
    from services.log_question_registry import resolve_chips

    qs = await get_pending_questions()
    for q in qs:
        ctx = {}
        raw = q.get("context")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    ctx = parsed
            except Exception:
                ctx = {}
        q["chips"] = resolve_chips(q.get("scope") or "", ctx)
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

    # 夜の振り返り（summary スコープ）の中の「明日のMIT」質問への回答は、
    # 翌日の DailyNote の `## 🎯 MIT` セクションへ前もって書き込んでおく。
    # こうしておかないと、翌朝の MorningMitCog が「今日の MIT 未設定」と判定し、
    # 同じ MIT を朝もう一度聞いてきてしまう（前日入力が当日に反映されない不具合）。
    try:
        mq = await _find_question_by_id(qid)
        if mq and mq.get("scope") == "summary" and (mq.get("question") or "").startswith("明日のMIT"):
            import re as _re2
            mit_items = []
            for ln in (req.answer or "").splitlines():
                ln = _re2.sub(r"^\s*[0-9]+[.)、]\s*", "", ln).strip()
                ln = _re2.sub(r"^[-・*]\s*", "", ln).strip()
                if ln:
                    mit_items.append(ln[:60])
            mit_items = mit_items[:3]
            if mit_items:
                from api import app
                bot = getattr(app.state, "bot", None)
                partner_cog = bot.get_cog("PartnerCog") if bot else None
                if partner_cog:
                    tomorrow_str = (
                        datetime.datetime.now(JST) + datetime.timedelta(days=1)
                    ).strftime("%Y-%m-%d")
                    await partner_cog._set_mit_for_date(tomorrow_str, mit_items)
    except Exception as e:
        logging.error(f"明日のMIT 翌日反映エラー: {e}")

    # meal / expense スコープ: 回答テキストを記録系ログへ反映する（ログ質問フレームワーク）
    try:
        rq = await _find_question_by_id(qid)
        rscope = rq.get("scope") if rq else None
        if rscope == "meal":
            await _reflect_meal_answer(qid, req.answer)
        elif rscope == "expense":
            await _reflect_expense_answer(qid, req.answer)
        elif rscope in ("mood", "condition"):
            await _reflect_mood_answer(qid, req.answer, rscope)
        elif rscope == "reading":
            await _reflect_reading_answer(qid, req.answer)
        elif rscope == "english_quiz":
            await _reflect_english_quiz_answer(rq, req.answer)
        elif rscope in ("afternoon", "learning", "gratitude"):
            await _reflect_journal_answer(qid, req.answer, rscope)
    except Exception as e:
        logging.error(f"log-question reflect エラー: {e}")

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


def _meal_type_jp(mt: str) -> str:
    """analyze の英語 meal_type を日本語に正規化。不明なら現在時刻から推定。"""
    m = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食", "snack": "間食"}.get((mt or "").strip().lower())
    if m:
        return m
    h = datetime.datetime.now(JST).hour
    if 4 <= h < 11:
        return "朝食"
    if 11 <= h < 15:
        return "昼食"
    if 15 <= h < 18:
        return "間食"
    return "夕食"


async def _reflect_meal_answer(qid: int, answer_text: str):
    """meal スコープの回答を栄養推定して食事ログに保存し、リンク返信を送る（フローA）。"""
    from api.routers.meals import analyze_meal_text, meals_save, MealSaveRequest
    from api.database import resolve_question_by_id

    text = (answer_text or "").strip()
    if not text:
        return
    nutri = await analyze_meal_text(text)
    save_req = MealSaveRequest(
        name=(nutri.get("name") or text)[:60],
        meal_type=_meal_type_jp(nutri.get("meal_type")),
        calories=int(nutri.get("calories") or 0),
        protein_g=float(nutri.get("protein_g") or 0),
        fat_g=float(nutri.get("fat_g") or 0),
        carbs_g=float(nutri.get("carbs_g") or 0),
        memo=(nutri.get("memo") or ""),
    )
    await meals_save(save_req)
    await resolve_question_by_id(qid)
    cal = save_req.calories
    cal_txt = f"・約{cal}kcal" if cal else ""
    msg = f"🍽 食事ログに記録したよ（{save_req.name}{cal_txt}）\n[ACTION:open_meals]"
    await notification_service.save_message_and_notify("assistant", msg, title="🍽 食事ログ記録")


# afternoon/learning/gratitude スコープの表示アイコン・ラベル。
# 記録先はすべてデイリーノートの `## 📔 Daily Journal`（独立セクションにせず日記に統合）。
_JOURNAL_SCOPE_META = {
    "afternoon": ("🌤", "午後の調子"),
    "learning": ("💡", "学び"),
    "gratitude": ("🙏", "良かったこと"),
}

# 日次チェックインの記録先（Daily Journal に時刻付き1行で集約）
_CHECKIN_HEADING = "## 📔 Daily Journal"


async def _reflect_journal_answer(qid: int, answer_text: str, scope: str):
    """昼の振り返り / 学び / 感謝の回答を、入力テキストそのまま Obsidian の
    Daily Journal に時刻付きで1行記録する（フローA・日次チェックイン）。"""
    from api import app
    from api.database import resolve_question_by_id

    text = (answer_text or "").strip()
    if not text:
        return
    icon, label = _JOURNAL_SCOPE_META.get(scope, ("📝", "メモ"))
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if partner_cog:
        try:
            await partner_cog._append_raw_message_to_obsidian(
                f"{icon} {label}: {text}", target_heading=_CHECKIN_HEADING
            )
        except Exception as e:
            logging.debug(f"journal({scope}) obsidian append failed: {e}")
    await resolve_question_by_id(qid)
    await notification_service.save_message_and_notify(
        "assistant", f"📝 {label}を記録したよ（{text}）", title=f"📝 {label}記録",
    )


async def _reflect_expense_answer(qid: int, answer_text: str):
    """expense スコープの回答をAI抽出して支出ログへ反映する（フローB）。
    confidence=high は即保存＋リンク返信、それ以外は確認ボタンを返す（保存前確認）。"""
    from api.routers.expenses import analyze_expense_text
    from api.database import add_expense, resolve_question_by_id

    text = (answer_text or "").strip()
    if not text:
        return
    ex = await analyze_expense_text(text)
    await resolve_question_by_id(qid)
    amount = int(ex.get("amount") or 0)
    vendor = (ex.get("vendor") or "").strip()
    category = (ex.get("category") or "その他").strip()
    conf = (ex.get("confidence") or "low").strip()
    date = (ex.get("date") or "").strip() or datetime.datetime.now(JST).strftime("%Y-%m-%d")

    if conf == "high" and amount > 0:
        await add_expense(
            date=date, amount=amount, category=category, vendor=vendor,
            payment_method=(ex.get("payment_method") or ""), memo=(ex.get("memo") or ""),
        )
        v = vendor or category
        msg = f"💰 支出を記録したよ（{v} ¥{amount:,}・{category}）\n[ACTION:open_expenses]"
        await notification_service.save_message_and_notify("assistant", msg, title="💰 支出記録")
    else:
        def _s(x):
            return str(x or "").replace("|", " ").replace("=", " ").replace("\n", " ")
        msg = (
            "💰 支出を記録する？内容を確認して保存してね\n"
            f"[ACTION:expense_confirm:amount={amount}|vendor={_s(vendor)}|category={_s(category)}"
            f"|payment_method={_s(ex.get('payment_method'))}|memo={_s(ex.get('memo') or text)}|date={date}]"
        )
        await notification_service.save_message_and_notify("assistant", msg, title="💰 支出の確認")


async def _reflect_mood_answer(qid: int, answer_text: str, scope: str):
    """mood / condition スコープの回答を Obsidian の Daily Journal に時刻付き1行で記録する
    （フローA・選択式・日次チェックインとして日記に統合）。"""
    from api import app
    from api.database import resolve_question_by_id

    text = (answer_text or "").strip()
    if not text:
        return
    label = "気分" if scope == "mood" else "体調"
    icon = "😀" if scope == "mood" else "🩺"
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if partner_cog:
        try:
            await partner_cog._append_raw_message_to_obsidian(
                f"{icon} {label}: {text}", target_heading=_CHECKIN_HEADING
            )
        except Exception as e:
            logging.debug(f"mood/condition obsidian append failed: {e}")
    await resolve_question_by_id(qid)
    await notification_service.save_message_and_notify(
        "assistant", f"{icon} {label}を記録したよ（{text}）", title=f"{icon} {label}記録",
    )


async def _extract_reading(text: str) -> dict:
    """読書メモのテキストから書名と学び・メモを抽出する（フローB）。"""
    from api import app
    from google.genai import types as _gt

    fallback = {"book": "", "memo": text}
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        return fallback
    prompt = (
        "次の読書メモから書名と学び・要点を抽出し、必ず以下の JSON だけ返してください。前置き禁止。\n"
        f"メモ: {text}\n\n"
        '{"book": "書名（分からなければ空文字）", "memo": "学び・要点（元の文意を保って簡潔に）"}'
    )
    try:
        import json as _json
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("partner_chat", default_pro=False)
        resp = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[_gt.Part.from_text(text=prompt)]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = _json.loads(resp.text)
        if not (data.get("memo") or "").strip():
            data["memo"] = text
        return data
    except Exception as e:
        logging.debug(f"_extract_reading error: {e}")
        return fallback


async def _reflect_reading_answer(qid: int, answer_text: str):
    """reading スコープの回答を書名ごとの読書ノート（BookNotes）へ保存する（フローB）。"""
    from api import app
    from api.database import resolve_question_by_id

    text = (answer_text or "").strip()
    if not text:
        return
    data = await _extract_reading(text)
    book = (data.get("book") or "").strip()
    memo = (data.get("memo") or text).strip()
    bot = getattr(app.state, "bot", None)
    saved = False
    if bot and book:
        book_cog = bot.get_cog("BookCog")
        if book_cog:
            try:
                saved = await book_cog.append_book_memo(book, memo)
            except Exception as e:
                logging.debug(f"reading book note append failed: {e}")
    if not saved:
        # 書名が取れない/失敗時は DailyNote の Reading Log に追記
        partner_cog = bot.get_cog("PartnerCog") if bot else None
        if partner_cog:
            try:
                await partner_cog._append_raw_message_to_obsidian(
                    f"📖 {text}", target_heading="## 📖 Reading Log"
                )
            except Exception as e:
                logging.debug(f"reading daily note append failed: {e}")
    await resolve_question_by_id(qid)
    where = f"『{book}』のノート" if (saved and book) else "今日のノート"
    await notification_service.save_message_and_notify(
        "assistant", f"📖 読書メモを{where}に記録したよ", title="📖 読書メモ",
    )


async def _reflect_english_quiz_answer(rq: dict, answer_text: str):
    """english_quiz スコープの回答を採点し、正誤を学習データへ記録する（学習レール）。
    context に {correct, phrase_id} を持つ前提。"""
    import json as _json
    from api.database import record_quiz_attempt, resolve_question_by_id

    qid = rq.get("id")
    ctx = {}
    raw = rq.get("context")
    if raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                ctx = parsed
        except Exception:
            ctx = {}
    correct = (ctx.get("correct") or "").strip()
    phrase_id = ctx.get("phrase_id")
    ans = (answer_text or "").strip()
    is_correct = bool(correct) and ans == correct
    try:
        if phrase_id is not None:
            await record_quiz_attempt(int(phrase_id), is_correct)
    except Exception as e:
        logging.debug(f"record_quiz_attempt failed: {e}")
    if qid is not None:
        await resolve_question_by_id(int(qid))
    msg = "🎉 正解！その調子！" if is_correct else f"惜しい！正解は「{correct}」だよ。また出すね💪"
    await notification_service.save_message_and_notify(
        "assistant", msg, title="🗣 英語クイズ",
    )


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
                "今日のデイリーサマリーをまとめてObsidianに保存したよ📅 下のボタンから今日の振り返りを見てね🌙\n[ACTION:open_reflection]",
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
