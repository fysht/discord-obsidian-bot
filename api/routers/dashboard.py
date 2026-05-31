"""ダッシュボード（PWA ホーム画面用の統合データ）。"""

import datetime
import logging
import re

from fastapi import APIRouter, Depends

from api.routes import verify_api_key, _get_fitbit_semaphore
from config import JST
from services.info_service import InfoService

router = APIRouter(prefix="", tags=["dashboard"])

_dashboard_sleep_cache: dict = {"data": None, "expires_at": None}


@router.get("/dashboard", dependencies=[Depends(verify_api_key)])
async def dashboard():
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service:
        return {"tasks": [], "alter_log": "", "error": "サービス未接続"}

    sleep_stats = {"score": "N/A", "duration": "N/A"}
    service = chat_service.drive_service.get_service()
    if not service:
        return {"tasks": [], "alter_log": "", "sleep": sleep_stats}

    now = datetime.datetime.now(JST)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    display_date = f"{now.year}年{now.month}月{now.day}日 ({weekdays[now.weekday()]})"
    today_str = now.strftime("%Y-%m-%d")

    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    content = ""
    if folder_id:
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if f_id:
            try:
                content = await chat_service.drive_service.read_text_file(service, f_id)
            except Exception as e:
                logging.debug(f"daily note read failed: {e}")

    tasks = []
    task_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if task_match:
        for line in task_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- "):
                cb_match = re.search(r"- \[(.)\] (.*)", line)
                if cb_match:
                    tasks.append({"text": cb_match.group(2), "done": (cb_match.group(1) == 'x')})
                else:
                    tasks.append({"text": line[2:].strip(), "is_log": True})

    alter_log = ""
    daily_journal = ""
    next_actions = ""
    mit_items: list[str] = []

    def extract_section(text, header):
        m = re.search(rf"{re.escape(header)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def extract_alter_log(text):
        # 新名 🔎 Insights を優先しつつ、未移行ノート用に旧名もフォールバックで読む
        m = re.search(r"## 🔎 Insights\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m:
            m = re.search(r"## 💡 Insights & Thoughts\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m:
            m = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m:
            m = re.search(r"## 🕵️ AI Assessment\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    alter_log = extract_alter_log(content)
    alter_log_date = today_str if alter_log else ""
    daily_journal = extract_section(content, "## 📔 Daily Journal") or ""
    daily_journal_date = today_str if daily_journal else ""
    next_actions_raw = extract_section(content, "## 🚀 Next Actions") or ""
    next_actions = next_actions_raw
    mit_raw = extract_section(content, "## 🎯 MIT") or ""
    mit_items = [
        re.sub(r"^-\s*", "", l).strip()
        for l in mit_raw.splitlines()
        if l.strip().startswith("- [")
    ]

    yesterday_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if folder_id:
        if not alter_log or not daily_journal:
            y_fid = await chat_service.drive_service.find_file(service, folder_id, f"{yesterday_str}.md")
            if y_fid:
                try:
                    y_content = await chat_service.drive_service.read_text_file(service, y_fid)
                    if not alter_log:
                        alter_log = extract_alter_log(y_content)
                        if alter_log:
                            alter_log_date = yesterday_str
                    if not daily_journal:
                        dj = extract_section(y_content, "## 📔 Daily Journal")
                        if dj:
                            daily_journal = dj
                            daily_journal_date = yesterday_str
                    if not next_actions:
                        na = extract_section(y_content, "## 🚀 Next Actions")
                        if na:
                            next_actions = na
                except Exception as e:
                    logging.debug(f"yesterday content fetch failed: {e}")

    if not alter_log:
        alter_log = "本日の観察ログはまだ生成されていません"

    g_calendar = []
    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        g_calendar = await chat_service.calendar_service.get_raw_events_for_date(today_str)

    google_tasks_work = []
    google_tasks_private = []
    habits = []
    if hasattr(chat_service, "tasks_service") and chat_service.tasks_service:
        try:
            work_uncompleted = await chat_service.tasks_service.get_raw_tasks("仕事")
            work_done_today = await chat_service.tasks_service.get_completed_tasks_today("仕事")
            google_tasks_work = work_uncompleted + [
                {"id": f"done_w_{i}", "title": t, "notes": "", "completed": True}
                for i, t in enumerate(work_done_today)
            ]

            private_uncompleted = await chat_service.tasks_service.get_raw_tasks("プライベート")
            private_done_today = await chat_service.tasks_service.get_completed_tasks_today("プライベート")
            google_tasks_private = private_uncompleted + [
                {"id": f"done_p_{i}", "title": t, "notes": "", "completed": True}
                for i, t in enumerate(private_done_today)
            ]

            habits = await chat_service.tasks_service.get_raw_tasks("習慣")
        except Exception as e:
            logging.debug(f"google tasks fetch failed: {e}")

    try:
        weather_data = await bot.info_service.get_weather() if hasattr(bot, "info_service") else await InfoService().get_weather()
        raw_news = await (bot.info_service.get_news(limit=5) if hasattr(bot, "info_service") else InfoService().get_news(limit=5))
        news = []
        for n in raw_news:
            if isinstance(n, dict):
                news.append({"title": n.get("title", ""), "link": n.get("link", "#")})
            else:
                parts = str(n).split('\n')
                if len(parts) >= 2:
                    news.append({"title": parts[0], "link": parts[1]})
                else:
                    news.append({"title": str(n), "link": "#"})
    except Exception:
        weather_data = {"summary": "取得失敗"}
        news = []

    fitbit_cog = bot.get_cog("FitbitCog")
    if fitbit_cog and fitbit_cog.is_ready:
        now_dt = datetime.datetime.now(JST)
        cached = _dashboard_sleep_cache
        if cached["data"] and cached["expires_at"] and now_dt < cached["expires_at"]:
            sleep_stats = cached["data"]
        else:
            try:
                async with _get_fitbit_semaphore():
                    stats = await fitbit_cog.fitbit_service.get_stats(now_dt.date())
                if stats:
                    score = stats.get("sleep_score")
                    raw_duration = stats.get("total_sleep_minutes")
                    sleep_stats = {
                        "score": score or "N/A",
                        "duration": fitbit_cog._format_minutes(raw_duration) if raw_duration else "N/A",
                    }
                    cached["data"] = sleep_stats
                    cached["expires_at"] = now_dt + datetime.timedelta(minutes=10)
            except Exception as e:
                logging.debug(f"fitbit dashboard stats failed: {e}")

    return {
        "tasks": tasks, "alter_log": alter_log, "date": display_date, "g_calendar": g_calendar,
        "google_tasks_work": google_tasks_work, "google_tasks_private": google_tasks_private,
        "habits": habits, "weather": weather_data, "news": news, "sleep": sleep_stats,
        "daily_journal": daily_journal,
        "daily_journal_date": daily_journal_date,
        "alter_log_date": alter_log_date,
        "next_actions": next_actions,
        "mit": mit_items,
    }
