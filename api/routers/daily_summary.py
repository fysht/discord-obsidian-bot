"""デイリーサマリー & マネージャー質問（daily_summary / daily_questions）関連エンドポイント。

Obsidian 保存ヘルパー (_save_*_to_obsidian / _generate_daily_summary) は
api.routes に残置（dailysummary_cog / partner_routine_cog からも import されているため）。
"""

import datetime
import logging
import random
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


# 記録完了の一言。定型文の繰り返しを避け、毎回ニュアンスを変えて「人が話している」感じにする。
_ACK_TEMPLATES = [
    "{label}、記録しといたよ！",
    "おっけー、{label}メモしといた〜",
    "{label}、ちゃんと残しておいたよ。",
    "了解！{label}控えたよ✍️",
    "うん、{label}書きとめたよ👍",
    "{label}、しっかり記録したよ。",
    "はーい、{label}残しといたね。",
]


def _casual_ack(label: str) -> str:
    """記録完了メッセージを毎回少しずつ違う言い回しで返す（定型感をなくす）。"""
    return random.choice(_ACK_TEMPLATES).format(label=label)


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
        _save_daily_data_to_obsidian,
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
            await _save_daily_data_to_obsidian(date_str)
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


@router.get("/daily_questions/by_marker", dependencies=[Depends(verify_api_key)])
async def daily_questions_by_marker(date: str, scope: str = "summary"):
    """指定スコープ・日付の質問一覧（resolved 含む）。チャットの [QUESTIONS:scope:date]
    マーカー描画用。回答済みの質問は『回答そのもの』を表示するために使う。"""
    import json
    from services.log_question_registry import resolve_chips

    qs = await get_questions_by_date(date, scope=scope)
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


@router.get("/daily_journal_entries", dependencies=[Depends(verify_api_key)])
async def daily_journal_entries():
    """今日の Daily Journal セクションから自発記録（出来事/学び/良かったこと）を
    scope ごとに時刻付きで返す。記録ボードを開いたとき、その日に既に記録した分を
    一覧表示するために使う（記録は Obsidian にのみ保存され、再読込で消えないように）。"""
    import re as _re
    from api import app

    board_scopes = ["event", "learning", "gratitude"]
    out: dict[str, list] = {s: [] for s in board_scopes}
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"entries": out}
    try:
        drive = chat_service.drive_service
        service = drive.get_service()
        folder_id = await drive.find_file(service, chat_service.drive_folder_id, "DailyNotes")
        if not folder_id:
            return {"entries": out}
        today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        f_id = await drive.find_file(service, folder_id, f"{today}.md")
        if not f_id:
            return {"entries": out}
        content = await drive.read_text_file(service, f_id)
    except Exception as e:
        logging.debug(f"daily_journal_entries read error: {e}")
        return {"entries": out}

    m = _re.search(r"## 📔 Daily Journal\n(.*?)(?=\n## |\Z)", content or "", _re.DOTALL)
    if not m:
        return {"entries": out}
    # 各行は "- HH:MM {icon} {label}: {text}"（_append_raw_message_to_obsidian の書式）。
    for raw in m.group(1).splitlines():
        lm = _re.match(r"^-\s*(\d{1,2}:\d{2})\s+(.*)$", raw.strip())
        if not lm:
            continue
        time_s, rest = lm.group(1), lm.group(2)
        for scope in board_scopes:
            icon, label = _JOURNAL_SCOPE_META.get(scope, ("", ""))
            prefix = f"{icon} {label}:"
            if rest.startswith(prefix):
                text = rest[len(prefix):].strip()
                if text:
                    out[scope].append({"time": time_s, "text": text})
                break
    return {"entries": out}


class QuickLogRequest(BaseModel):
    scope: str
    text: str
    date: str | None = None  # MIT で「明日の分」を設定する等、対象日を指定する場合に使う


@router.post("/daily_questions/quick_log", dependencies=[Depends(verify_api_key)])
async def daily_questions_quick_log(req: QuickLogRequest):
    """出来事 / 学び / 良かったこと / MIT 等を、マネージャーの質問を待たず自分から記録する。
    「残しておこう」と思った瞬間に何度でも入力できる入口（夜にまとめてではなく都度・複数入力）。

    記録は質問への回答と同じ扱いで、対象 scope（registry の followup="ai"）には回答後に
    AI 掘り下げ質問が静かに積まれる＝自発記録が会話の種まきになる。"""
    scope = (req.scope or "").strip()
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="本文が空です")

    # MIT は append ではなく1日1セットの set（Obsidian の ## 🎯 MIT を置き換え）。
    # 朝の MorningMitCog が同じ MIT を再度聞かないよう morning_mit 質問も resolved 化する。
    if scope == "mit":
        import re as _re
        items = []
        for ln in text.splitlines():
            ln = _re.sub(r"^\s*[0-9]+[.)、]\s*", "", ln).strip()
            ln = _re.sub(r"^[-・*]\s*", "", ln).strip()
            if ln:
                items.append(ln[:60])
        items = items[:3]
        if not items:
            raise HTTPException(status_code=422, detail="MIT が空です")
        from api import app
        bot = getattr(app.state, "bot", None)
        partner_cog = bot.get_cog("PartnerCog") if bot else None
        if not partner_cog:
            raise HTTPException(status_code=503, detail="Obsidian に接続できませんでした")
        today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        target_date = (req.date or "").strip()
        if target_date and target_date != today:
            # 明日（など今日以外）の MIT は対象日のノートへ前もって設定する。
            await partner_cog._set_mit_for_date(target_date, items)
            return {"ok": True, "icon": "🎯", "label": "MIT", "scope": "mit"}
        await partner_cog._set_mit_to_obsidian(items)
        # 朝の MorningMitCog が同じ MIT を再度聞かないよう morning_mit 質問も resolved 化する。
        try:
            await resolve_questions(today, scope="morning_mit")
        except Exception as e:
            logging.debug(f"quick_log MIT: morning_mit resolve skipped: {e}")
        return {"ok": True, "icon": "🎯", "label": "MIT", "scope": "mit"}

    # 気分 / 体調は日次チェックインとして Daily Journal に時刻付き1行で追記する
    # （質問経由の _reflect_mood_answer と同じ記録先。自発記録では理由の追質問は出さない）。
    if scope in ("mood", "condition"):
        from api import app
        bot = getattr(app.state, "bot", None)
        partner_cog = bot.get_cog("PartnerCog") if bot else None
        label = "気分" if scope == "mood" else "体調"
        icon = "😀" if scope == "mood" else "🩺"
        if partner_cog:
            try:
                await partner_cog._append_raw_message_to_obsidian(
                    f"{icon} {label}: {text}", target_heading=_CHECKIN_HEADING
                )
            except Exception as e:
                logging.debug(f"quick_log {scope} obsidian append failed: {e}")
        return {"ok": True, "icon": icon, "label": label, "scope": scope}

    if scope not in _JOURNAL_SCOPE_META:
        raise HTTPException(status_code=422, detail="対応していない種類です")
    icon, label = await _append_journal_line(scope, text)
    # 自発記録にも質問起点と同じ AI 掘り下げを適用（深さ0から・push なし）。
    try:
        await _maybe_generate_followup(scope, text, depth=0)
    except Exception as e:
        logging.debug(f"quick_log followup skipped ({scope}): {e}")
    return {"ok": True, "icon": icon, "label": label, "scope": scope}


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
            await _reflect_meal_answer(rq, req.answer)
        elif rscope == "expense":
            await _reflect_expense_answer(qid, req.answer)
        elif rscope in ("mood", "condition"):
            await _reflect_mood_answer(rq, req.answer, rscope)
        elif rscope == "reading":
            await _reflect_reading_answer(qid, req.answer)
        elif rscope == "english_quiz":
            await _reflect_english_quiz_answer(rq, req.answer)
        elif rscope in ("afternoon", "learning", "gratitude", "event"):
            await _reflect_journal_answer(qid, req.answer, rscope)

        # 掘り下げ：回答後に AI が価値ありと判断したら追質問を静かに積む。
        # 親質問の context.depth を引き継いで深さ上限で連鎖を止める（mood は専用の理由
        # 追質問を持つので followup="ai" 対象外＝ここでは発火しない）。
        if rscope:
            import json as _fj
            depth = 0
            try:
                _ctx = _fj.loads(rq.get("context") or "{}") if isinstance(rq, dict) else {}
                depth = int(_ctx.get("depth") or 0)
            except Exception:
                depth = 0
            await _maybe_generate_followup(
                rscope, req.answer, date=(rq.get("date") if isinstance(rq, dict) else None), depth=depth,
            )
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


async def _reflect_meal_answer(rq: dict, answer_text: str):
    """meal スコープの回答を栄養推定して食事ログに保存し、リンク返信を送る（フローA）。

    食事区分は質問の context.meal_type を最優先する（朝食の質問への回答は、たとえ
    夜にまとめて回答しても「朝食」として記録される）。context に無い会話入力の場合のみ
    AI 推定／現在時刻から区分を決める。記録時刻は meals_save が区分の代表時刻で補完する。"""
    import json as _json
    from api.routers.meals import analyze_meal_text, meals_save, MealSaveRequest
    from api.database import resolve_question_by_id

    qid = rq.get("id") if isinstance(rq, dict) else rq
    text = (answer_text or "").strip()
    if not text:
        return
    # 質問生成時に埋めた食事区分（朝食/昼食/夕食）を正とする。
    ctx_meal_type = ""
    try:
        ctx = _json.loads(rq.get("context") or "{}") if isinstance(rq, dict) else {}
        if isinstance(ctx, dict):
            ctx_meal_type = (ctx.get("meal_type") or "").strip()
    except Exception:
        ctx_meal_type = ""
    nutri = await analyze_meal_text(text)
    meal_type = ctx_meal_type or _meal_type_jp(nutri.get("meal_type"))
    save_req = MealSaveRequest(
        name=(nutri.get("name") or text)[:60],
        meal_type=meal_type,
        calories=int(nutri.get("calories") or 0),
        protein_g=float(nutri.get("protein_g") or 0),
        fat_g=float(nutri.get("fat_g") or 0),
        carbs_g=float(nutri.get("carbs_g") or 0),
        memo=(nutri.get("memo") or ""),
    )
    await meals_save(save_req)
    if qid is not None:
        await resolve_question_by_id(qid)
    msg = f"🍽 {_casual_ack('食事')}\n[ACTION:open_meals]"
    await notification_service.save_message_and_notify("assistant", msg, title="🍽 食事ログ記録")


# afternoon/learning/gratitude/event スコープの表示アイコン・ラベル。
# 記録先はすべてデイリーノートの `## 📔 Daily Journal`（独立セクションにせず日記に統合）。
_JOURNAL_SCOPE_META = {
    "afternoon": ("🌤", "午後の調子"),
    "learning": ("💡", "学び"),
    "gratitude": ("🙏", "良かったこと"),
    "event": ("📌", "出来事"),
}

# 日次チェックインの記録先（Daily Journal に時刻付き1行で集約）
_CHECKIN_HEADING = "## 📔 Daily Journal"


async def _append_journal_line(scope: str, text: str) -> tuple[str, str]:
    """出来事 / 学び / 良かったこと / 午後の調子を Daily Journal に時刻付き1行で追記する。
    (icon, label) を返す。質問経由でもクイック入力経由でも共通で使う。"""
    from api import app

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
    return icon, label


async def _reflect_journal_answer(qid: int, answer_text: str, scope: str):
    """昼の振り返り / 学び / 感謝 / 出来事の回答を、入力テキストそのまま Obsidian の
    Daily Journal に時刻付きで1行記録する（フローA・日次チェックイン）。"""
    from api.database import resolve_question_by_id

    text = (answer_text or "").strip()
    if not text:
        return
    icon, label = await _append_journal_line(scope, text)
    await resolve_question_by_id(qid)
    await notification_service.save_message_and_notify(
        "assistant", f"{icon} {_casual_ack(label)}", title=f"{icon} {label}記録",
    )


# 掘り下げ（フォローアップ質問）の最大深さ。回答→追質問→回答 を最大この回数だけ繰り返す。
# 自発記録モードで「答えるたびに質問が増え続ける」のを防ぐためのループ上限。
_FOLLOWUP_MAX_DEPTH = 1


async def _maybe_generate_followup(scope: str, answer_text: str, *, date: str | None = None, depth: int = 0):
    """回答（質問起点・自発記録のどちらでも）に対し、AI が「掘り下げる価値あり」と判断した
    ときだけ追質問を1つ生成し、未回答インボックスに静かに積む。

    設計上の約束（docs/log_question_framework.md / 自発記録の掘り下げ）:
      - 文面は固定テンプレで縛らず AI 自由生成（registry の followup="ai" が対象）。
      - 価値判定も AI に任せ、一言の事実メモなど掘る余地が乏しいものは掘らない。
      - 深さ上限（_FOLLOWUP_MAX_DEPTH）で連鎖を打ち切りループを防ぐ。
      - push 通知は出さず save_message のみ＝平日昼に鳴らさず、夜インボックスを開いた時に気づく。
    """
    import json as _json
    from services.log_question_registry import should_followup, get_scope_config

    if not should_followup(scope):
        return
    if depth >= _FOLLOWUP_MAX_DEPTH:
        return
    text = (answer_text or "").strip()
    if not text:
        return

    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        return

    cfg = get_scope_config(scope)
    label = cfg.get("label") or scope

    # 追質問は「いつものマネージャー」と同じ人格・口調で発話させる。
    # チャットや定期メッセージと同じ get_system_prompt を system_instruction に渡し、
    # 別人格に感じられないようにする（質問文だけ JSON で受け取る）。
    from prompts import get_system_prompt
    partner_cog = bot.get_cog("PartnerCog")
    user_name = getattr(partner_cog, "user_name", "ゆうすけ") if partner_cog else "ゆうすけ"
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    user_manual = ""
    if partner_cog:
        try:
            user_manual = await partner_cog._get_user_manual()
        except Exception:
            user_manual = ""
    system_prompt = get_system_prompt(user_name, now_str, user_manual)

    task = (
        f"ゆうすけが今ちょうど「{label}」をこう記録したよ:\n「{text}」\n\n"
        "これを読んで、もう一歩だけ掘り下げて聞く価値があるか判断して。"
        "感情・判断・学び・次の行動につながりそうなら掘り下げる。"
        "事実の羅列や一言メモなら掘らない（deepen=false）。\n"
        "掘り下げるなら、いつもの君（マネージャー）のキャラと口調そのままで、"
        "タメ口・親しみやすく・短く1つだけ、ゆうすけが答えたくなる質問を作って。詰問にはしないこと。\n"
        "出力は必ず次の JSON だけ。前置き禁止。\n"
        '{"deepen": true/false, "question": "追質問（君の口調そのまま。なければ空文字）"}'
    )
    try:
        from google.genai import types as _gt
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("partner_chat", default_pro=False)
        resp = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[_gt.Part.from_text(text=task)]),
            config=_gt.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
            ),
        )
        data = _json.loads(resp.text)
    except Exception as e:
        logging.debug(f"followup gen failed ({scope}): {e}")
        return
    if not (isinstance(data, dict) and data.get("deepen")):
        return
    question = (data.get("question") or "").strip()
    if not question:
        return

    from api.database import add_daily_question, save_message
    day = date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    ctx = _json.dumps({"followup": True, "depth": depth + 1}, ensure_ascii=False)
    await add_daily_question(day, question, scope=scope, context=ctx)
    # push なしでチャットに積むだけ（インライン回答欄が後で描画される）。
    await save_message(
        "assistant",
        f"{cfg.get('icon', '💬')} {question}\n[QUESTIONS:{scope}:{day}]",
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
        msg = f"💰 {_casual_ack('支出')}\n[ACTION:open_expenses]"
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


async def _reflect_mood_answer(rq: dict, answer_text: str, scope: str):
    """mood / condition スコープの回答を Obsidian の Daily Journal に時刻付き1行で記録する
    （フローA・選択式・日次チェックインとして日記に統合）。

    気分は「まあまあ」に寄りがちなので、最初の回答（context.followup なし）には
    理由を一言うながす追質問を出す。理由の回答（context.followup=True）はその場の
    気分行に続けて記録し、それ以上は追質問しない（ループ防止）。"""
    import json as _json
    from api import app
    from api.database import resolve_question_by_id

    qid = rq.get("id") if isinstance(rq, dict) else rq
    text = (answer_text or "").strip()
    if not text:
        return
    # 追質問（理由）かどうかを context から判定する。
    is_followup = False
    try:
        ctx = _json.loads(rq.get("context") or "{}") if isinstance(rq, dict) else {}
        is_followup = bool(isinstance(ctx, dict) and ctx.get("followup"))
    except Exception:
        is_followup = False

    label = "気分" if scope == "mood" else "体調"
    icon = "😀" if scope == "mood" else "🩺"
    log_label = f"{label}の理由" if is_followup else label
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if partner_cog:
        try:
            await partner_cog._append_raw_message_to_obsidian(
                f"{icon} {log_label}: {text}", target_heading=_CHECKIN_HEADING
            )
        except Exception as e:
            logging.debug(f"mood/condition obsidian append failed: {e}")
    if qid is not None:
        await resolve_question_by_id(qid)

    # 最初の気分回答には理由を一言うながす（push なしでチャットに回答欄だけ追加）。
    if scope == "mood" and not is_followup:
        try:
            from api.database import add_daily_question, save_message
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            fctx = _json.dumps({"followup": True}, ensure_ascii=False)
            await add_daily_question(
                today, f"「{text}」だったのは何があったから？（一言・なければスキップでOK）",
                scope="mood", context=fctx,
            )
            await save_message(
                "assistant",
                f"{icon} {_casual_ack(label)} "
                f"よかったら理由も一言どうぞ👇\n"
                f"「{text}」だったのは何があったから？（一言・なければスキップでOK）\n"
                f"[QUESTIONS:mood:{today}]",
            )
            return
        except Exception as e:
            logging.debug(f"mood reason follow-up failed: {e}")
    await notification_service.save_message_and_notify(
        "assistant", f"{icon} {_casual_ack(log_label)}", title=f"{icon} {label}記録",
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
    await notification_service.save_message_and_notify(
        "assistant", f"📖 {_casual_ack('読書メモ')}", title="📖 読書メモ",
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
        _save_daily_data_to_obsidian,
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
        await _save_daily_data_to_obsidian(date_str)
        await resolve_questions(date_str, scope="summary")
        try:
            await notification_service.save_message_and_notify(
                "assistant",
                "今日のデイリーサマリーをまとめてObsidianに保存したよ📅 下のボタンからデイリーノートを見てね🌙\n[ACTION:open_reflection]",
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


@router.post("/daily_questions/{qid}/resolve", dependencies=[Depends(verify_api_key)])
async def daily_questions_resolve(qid: int):
    """質問を reflect なしで resolved にして閉じる。
    食事ログなど別の入力UIで既に記録済みの質問を、未回答に残さないために使う。"""
    from api.database import resolve_question_by_id
    ok = await resolve_question_by_id(qid)
    if not ok:
        raise HTTPException(status_code=404, detail="質問が見つかりません")
    return {"status": "success"}
