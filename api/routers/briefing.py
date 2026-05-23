"""朝/夕ブリーフィング生成エンドポイント。"""

import datetime
import logging
import re

from fastapi import APIRouter, Depends

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["briefing"])


@router.post("/briefing", dependencies=[Depends(verify_api_key)])
async def briefing():
    """朝（12時前）はモーニングブリーフィング、午後以降はイブニングレビューを生成する。"""
    from api import app

    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    now = datetime.datetime.now(JST)
    is_morning = now.hour < 12
    briefing_type = "morning" if is_morning else "evening"

    context_parts = []

    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        try:
            events = await chat_service.calendar_service.get_raw_events_for_date(now.strftime("%Y-%m-%d"))
            if events:
                ev_str = "\n".join(f"- {e.get('summary', '?')} ({e.get('start_time', '?')}〜{e.get('end_time', '?')})" for e in events[:10])
                context_parts.append(f"今日の予定:\n{ev_str}")
        except Exception:
            pass

    if hasattr(chat_service, "tasks_service") and chat_service.tasks_service:
        try:
            for ln in ["仕事", "プライベート"]:
                tasks = await chat_service.tasks_service.get_raw_tasks(ln)
                if tasks:
                    t_str = "\n".join(
                        f"- {t['title']}" + (f" (締切: {t['due'][:10]})" if t.get('due') else "")
                        for t in tasks[:8]
                    )
                    context_parts.append(f"{ln}タスク:\n{t_str}")
        except Exception:
            pass

    try:
        info_svc = getattr(bot, "info_service", None)
        if info_svc:
            w = await info_svc.get_weather()
            if w and w.get("summary") not in ("取得失敗", None):
                context_parts.append(f"天気: {w.get('summary', '不明')} (最高{w.get('max_temp','--')}℃ / 最低{w.get('min_temp','--')}℃)")
    except Exception:
        pass

    if not is_morning and chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
            if folder_id:
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{now.strftime('%Y-%m-%d')}.md")
                if f_id:
                    content = await chat_service.drive_service.read_text_file(service, f_id)
                    m = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
                    if m:
                        context_parts.append(f"今日のライフログ:\n{m.group(1).strip()[:500]}")
        except Exception:
            pass

    context = "\n\n".join(context_parts) if context_parts else "情報が取得できませんでした。"

    gemini_client = getattr(bot, "gemini_client", None) if bot else None
    if not gemini_client:
        return {"reply": f"現在の情報:\n{context}", "type": briefing_type}

    if is_morning:
        prompt = (
            f"あなたはユーザーの秘書AIです。今は{now.strftime('%Y年%m月%d日 %H:%M')}です。\n"
            f"以下の情報を元に、朝のブリーフィングを簡潔に作成してください。\n\n"
            f"{context}\n\n"
            f"ブリーフィング内容:\n"
            f"1. 今日の天気のひとこと\n"
            f"2. 今日の予定サマリー\n"
            f"3. 優先タスクの提案（上位3つ）\n"
            f"4. 今日のひとことアドバイス\n\n"
            f"親しみやすく、簡潔に日本語で回答してください。"
        )
    else:
        prompt = (
            f"あなたはユーザーの秘書AIです。今は{now.strftime('%Y年%m月%d日 %H:%M')}です。\n"
            f"以下の情報を元に、今日の振り返りレビューを作成してください。\n\n"
            f"{context}\n\n"
            f"レビュー内容:\n"
            f"1. 今日の活動サマリー\n"
            f"2. 良かった点\n"
            f"3. 明日に向けての提案\n\n"
            f"親しみやすく、簡潔に日本語で回答してください。"
        )

    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("daily_review", default_pro=False)
        response = await gemini_client.aio.models.generate_content(model=_m, contents=prompt)
        reply = response.text.strip() if response.text else "ブリーフィング生成に失敗しました。"
    except Exception as e:
        logging.error(f"Briefing AI error: {e}")
        reply = f"AI生成でエラーが発生しました。\n\n{context}"

    return {"reply": reply, "type": briefing_type}
