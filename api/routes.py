import os
import logging
import asyncio
import datetime
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from api.database import (
    save_message, get_history, add_stocked_link, get_all_links, 
    get_link_by_id, update_link_details, mark_link_as_saved, 
    delete_stocked_link, get_todays_log, clear_history
)
from services.info_service import InfoService
from web_parser import fetch_maps_info
from config import JST

router = APIRouter(prefix="/api")

API_KEY = os.getenv("PWA_API_KEY", "secretary-ai-default-key")

async def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="認証に失敗しました。")

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    reply: str

class AuthRequest(BaseModel):
    password: str

@router.post("/auth")
async def authenticate(req: AuthRequest):
    app_password = os.getenv("PWA_PASSWORD", "secretary")
    if req.password != app_password:
        raise HTTPException(status_code=401, detail="パスワードが正しくありません。")
    return {"api_key": API_KEY}

async def _fetch_link_meta(url: str) -> dict:
    import aiohttp
    import re as _re
    import urllib.parse

    title = "Untitled"
    link_type = "web"

    # YouTube判定とタイトル取得強化
    if "youtube.com" in url or "youtu.be" in url:
        link_type = "youtube"
        try:
            safe_url = urllib.parse.quote(url, safe='')
            oembed = f"https://www.youtube.com/oembed?url={safe_url}&format=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(oembed, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title", "YouTube Video")
                        recipe_kw = ["レシピ", "作り方", "材料", "献立", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw):
                            link_type = "recipe"
        except Exception: pass
        return {"title": title, "type": link_type}
        
    if "maps.google.com" in url or "maps.app.goo.gl" in url or "goo.gl/maps" in url or "/maps/" in url:
        link_type = "map"
        try:
            place_name, _ = await fetch_maps_info(url)
            if place_name and place_name != "Google Maps Location":
                title = place_name
        except Exception: pass
        return {"title": title, "type": link_type}
        
    if "amazon.co.jp" in url or "amzn.to" in url:
        link_type = "book"

    recipe_domains = ["cookpad.com", "kurashiru.com", "delishkitchen.tv", "macaro-ni.jp",
                      "orangepage.net", "lettuceclub.net", "kyounoryouri.jp", "ajinomoto.co.jp"]
    if any(d in url for d in recipe_domains): link_type = "recipe"

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as response:
                if response.status == 200:
                    html = await response.text(errors="replace")
                    match = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
                    if match:
                        title = match.group(1).strip()[:200]
                    if link_type == "book":
                        title = title.replace("Amazon.co.jp:", "").replace("Amazon |", "").strip()
                    if link_type == "web":
                        recipe_kw = ["レシピ", "作り方", "献立", "材料", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw): link_type = "recipe"
                        elif "材料" in html[:5000] and "作り方" in html[:5000]: link_type = "recipe"
    except Exception as e: logging.error(f"Link meta fetch failed for {url}: {e}")

    return {"title": title if title else "Untitled", "type": link_type}


# --- Obsidian同期用共通関数 (英語表記統一) ---
async def sync_link_to_obsidian(chat_service, title: str, link_type: str, url: str, 
                                purpose: str="", target_date: str="", memo: str="", summary: str="",
                                is_update: bool = False):
    """リンク情報をObsidianに作成・更新する"""
    if not chat_service or not chat_service.drive_service: return
    service = chat_service.drive_service.get_service()
    if not service: return
    
    import re
    now = datetime.datetime.now(JST)
    folder_map = {"youtube": "YouTube", "recipe": "Recipes", "web": "WebClips", "map": "Places", "book": "BookNotes"}
    section_map = {"youtube": "## 📺 YouTube", "recipe": "## 🍳 Recipes", "web": "## 🔗 WebClips", "map": "## 🔗 WebClips", "book": "## 📖 Reading Log"}
    
    folder_name = folder_map.get(link_type, "WebClips")
    section_header = section_map.get(link_type, "## 🔗 WebClips")
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:80] or "Untitled"
    
    # 既存ファイルの検索ロジック (タイムスタンプの有無に関わらずタイトルで判定)
    existing_id = None
    target_filename = f"{safe_title}.md"
    
    try:
        drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        f_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
        if not f_id: f_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

        # ドライブ内をタイトルで検索
        q_title = safe_title.replace("'", "\\'")
        query = f"'{f_id}' in parents and name contains '{q_title}' and trashed = false"
        results = await asyncio.to_thread(lambda: service.files().list(q=query, fields="files(id, name)").execute())
        for f in results.get("files", []):
            fname = f["name"]
            if fname == f"{safe_title}.md" or fname.endswith(f"-{safe_title}.md"):
                existing_id = f["id"]
                target_filename = fname # 既存のファイル名を維持
                break
    except Exception as e:
        logging.error(f"Obsidian Search Error: {e}")
        return

    if not existing_id:
        if is_update:
            return # 詳細編集時は新規作成しない
        if link_type != "book":
            timestamp = now.strftime("%Y%m%d%H%M%S")
            target_filename = f"{timestamp}-{safe_title}.md"

    daily_note_date = now.strftime("%Y-%m-%d")
    
    # Markdownコンテンツ作成 (空行を追加してセテックス見出し化を防止)
    note_content = f"# {title}\n\n"
    if purpose: note_content += f"**🎯 Purpose:** {purpose}\n"
    if target_date: note_content += f"**📅 Target Date:** {target_date}\n"
    if memo: note_content += f"**📝 Memo:** {memo}\n"
    if summary: note_content += f"\n**💡 Summary:**\n{summary}\n"
    
    if url: note_content += f"\n---\n## Link\n{url}\n\n"
    note_content += f"\n---\nSaved: {now.strftime('%Y-%m-%d %H:%M')}\n[[{daily_note_date}]]"

    try:
        if existing_id:
            await chat_service.drive_service.update_text(service, existing_id, note_content)
            return # 更新時はデイリーノートへの追記をスキップ
        else:
            await chat_service.drive_service.upload_text(service, f_id, target_filename, note_content)

        # デイリーノートへの追記 (新規作成時のみ)
        link_str = f"- [[{folder_name}/{target_filename.replace('.md', '')}|{title}]]"
        daily_fid = await chat_service.drive_service.find_file(service, drive_root, "DailyNotes")
        if daily_fid:
            df_id = await chat_service.drive_service.find_file(service, daily_fid, f"{daily_note_date}.md")
            from utils.obsidian_utils import update_section
            if df_id:
                cur = await chat_service.drive_service.read_text_file(service, df_id)
                if link_str not in cur: await chat_service.drive_service.update_text(service, df_id, update_section(cur, link_str, section_header))
            else:
                initial_content = f"---\ndate: {daily_note_date}\n---\n\n# Daily Note {daily_note_date}\n\n{section_header}\n{link_str}\n"
                await chat_service.drive_service.upload_text(service, daily_fid, f"{daily_note_date}.md", initial_content)
    except Exception as e: logging.error(f"Obsidian Sync Error: {e}")


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(req: ChatRequest):
    from api import app
    import re

    url_match = re.search(r"https?://[^\s]+", req.message)
    if url_match:
        url = url_match.group(0)
        try:
            meta = await _fetch_link_meta(url)
            await add_stocked_link(url, meta["type"], meta["title"])

            # Obsidianへの即時作成
            chat_service = getattr(app.state, "chat_service", None)
            if chat_service:
                await sync_link_to_obsidian(chat_service, meta["title"], meta["type"], url)

            type_label = {"web": "🌐 ウェブ", "youtube": "📺 YouTube", "recipe": "🍳 レシピ", "map": "🗺️ マップ", "book": "📚 書籍"}.get(meta["type"], "🔗 リンク")
            reply = f"「{meta['title']}」を{type_label}としてストックし、ノートを作成しました。"
            await save_message("user", req.message)
            await save_message("assistant", reply)
            return ChatResponse(reply=reply)
        except Exception as e:
            logging.error(f"Link stock failed, falling back to AI: {e}")

    bot = getattr(app.state, "bot", None)
    if not bot: raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog: raise HTTPException(status_code=503, detail="AIコアがロードされていません。")

    await save_message("user", req.message)

    from google.genai import types
    db_history = await get_history(limit=15)
    history_messages = []
    for m in reversed(db_history[1:]): 
        role = "model" if m["role"] == "assistant" else "user"
        history_messages.append(types.Content(role=role, parts=[types.Part.from_text(text=m["content"])]))

    reply = await partner_cog.generate_response_for_app(req.message, history_messages)
    await save_message("assistant", reply)

    import asyncio
    from api.database import backup_db_to_drive
    if bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        asyncio.create_task(backup_db_to_drive(bot.drive_service, folder_id))

    return ChatResponse(reply=reply)

@router.get("/history", dependencies=[Depends(verify_api_key)])
async def history(limit: int = 100):
    return {"messages": await get_history(limit=limit)}

@router.get("/dashboard", dependencies=[Depends(verify_api_key)])
async def dashboard():
    from api import app
    import datetime
    import re

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service: return {"tasks": [], "alter_log": "", "error": "サービス未接続"}

    sleep_stats = {"score": "N/A", "duration": "N/A"}
    service = chat_service.drive_service.get_service()
    if not service: return {"tasks": [], "alter_log": "", "sleep": sleep_stats}

    now = datetime.datetime.now(JST)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    display_date = f"{now.year}年{now.month}月{now.day}日 ({weekdays[now.weekday()]})"
    today_str = now.strftime("%Y-%m-%d")

    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    content = ""
    if folder_id:
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if f_id:
            try: content = await chat_service.drive_service.read_text_file(service, f_id)
            except Exception: pass

    tasks = []
    task_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if task_match:
        for line in task_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- "):
                # タスク形式 ([x], [/]) と タイムライン形式の両方に対応
                cb_match = re.search(r"- \[(.)\] (.*)", line)
                if cb_match:
                    tasks.append({"text": cb_match.group(2), "done": (cb_match.group(1) == 'x')})
                else:
                    # タイムライン形式 (HH:mm - HH:mm活動名)
                    tasks.append({"text": line[2:].strip(), "is_log": True})

    alter_log = ""
    def extract_alter_log(text):
        m = re.search(r"## 💡 Insights & Thoughts\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m: m = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m: m = re.search(r"## 🕵️ AI Assessment\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    alter_log = extract_alter_log(content)
    
    # 当日のログがない場合、昨日のノートから取得を試みる
    if not alter_log and folder_id:
        yesterday_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        y_fid = await chat_service.drive_service.find_file(service, folder_id, f"{yesterday_str}.md")
        if y_fid:
            try:
                y_content = await chat_service.drive_service.read_text_file(service, y_fid)
                alter_log = extract_alter_log(y_content)
                if alter_log: alter_log = f"【昨日の分析】\n{alter_log}"
            except Exception: pass

    if not alter_log:
        alter_log = "本日の観察ログはまだ生成されていません。"

    g_calendar = []
    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        g_calendar = await chat_service.calendar_service.get_raw_events_for_date(today_str)

    google_tasks_work = []
    google_tasks_private = []
    habits = []
    if hasattr(chat_service, "tasks_service") and chat_service.tasks_service:
        try:
            google_tasks_work = await chat_service.tasks_service.get_raw_tasks("仕事")
            google_tasks_private = await chat_service.tasks_service.get_raw_tasks("プライベート")
            habits = await chat_service.tasks_service.get_raw_tasks("習慣")
        except Exception: pass

    try:
        weather_data = await bot.info_service.get_weather() if hasattr(bot, "info_service") else await InfoService().get_weather()
        raw_news = await (bot.info_service.get_news(limit=5) if hasattr(bot, "info_service") else InfoService().get_news(limit=5))
        news = []
        for n in raw_news:
            parts = n.split('\n')
            if len(parts) >= 2: news.append({"title": parts[0], "link": parts[1]})
            else: news.append({"title": n, "link": "#"})
    except Exception:
        weather_data = {"summary": "取得失敗"}
        news = []

    fitbit_cog = bot.get_cog("FitbitCog")
    if fitbit_cog and fitbit_cog.is_ready:
        try:
            target_date = datetime.datetime.now(JST).date()
            stats = await fitbit_cog.fitbit_service.get_stats(target_date)
            if stats:
                score = stats.get("sleep_score")
                raw_duration = stats.get("total_sleep_minutes")
                sleep_stats = {"score": score or "N/A", "duration": fitbit_cog._format_minutes(raw_duration) if raw_duration else "N/A"}
        except: pass

    return {
        "tasks": tasks, "alter_log": alter_log, "date": display_date, "g_calendar": g_calendar,
        "google_tasks_work": google_tasks_work, "google_tasks_private": google_tasks_private,
        "habits": habits, "weather": weather_data, "news": news, "sleep": sleep_stats
    }

class TaskActionRequest(BaseModel):
    action: str
    old_text: str = ""
    new_text: str = ""

@router.post("/task_action", dependencies=[Depends(verify_api_key)])
async def task_action(req: TaskActionRequest):
    from api import app
    import datetime
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service: raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    file_name = f"{today_str}.md"
    f_id = await chat_service.drive_service.find_file(service, folder_id, file_name)

    content = f"# Daily Note {today_str}\n"
    if f_id:
        try: content = await chat_service.drive_service.read_text_file(service, f_id)
        except Exception: pass

    if req.action == "create":
        content = update_section(content, f"- [/] {req.new_text}", "## 🪟 Lifelog")
    else:
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith("- [") and req.old_text in line:
                if req.action == "delete": lines.pop(i)
                elif req.action == "update":
                    prefix = line[:6]
                    lines[i] = f"{prefix}{req.new_text}" if "[" in prefix and "]" in prefix else line.replace(req.old_text, req.new_text, 1)
                elif req.action == "toggle":
                    lines[i] = line.replace("- [x]", "- [/]", 1) if "- [x]" in line else line.replace("- [/]", "- [x]", 1)
                break
        content = '\n'.join(lines)

    if f_id: await chat_service.drive_service.update_text(service, f_id, content)
    else: await chat_service.drive_service.upload_text(service, folder_id, file_name, content)
    return {"status": "success"}

@router.post("/reset_history", dependencies=[Depends(verify_api_key)])
async def reset_history():
    from api import app
    from api.database import backup_db_to_drive
    import asyncio
    
    await clear_history()
    
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        asyncio.create_task(backup_db_to_drive(bot.drive_service, folder_id))
        
    return {"status": "success"}

class CalendarActionRequest(BaseModel):
    action: str
    event_id: Optional[str] = None
    summary: str = None
    description: str = None
    start_time: str = None
    end_time: str = None

@router.post("/calendar_action", dependencies=[Depends(verify_api_key)])
async def calendar_action(req: CalendarActionRequest):
    from api import app
    import datetime
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.calendar_service: raise HTTPException(status_code=503, detail="カレンダーサービス未設定")
    
    if req.action == "add":
        start = req.start_time or datetime.datetime.now(JST).strftime("%Y-%m-%d 10:00:00")
        end = req.end_time or (datetime.datetime.strptime(start[:19], "%Y-%m-%d %H:%M:%S") + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S") if " " in start else start
        res = await bot.calendar_service.create_event(req.summary, start, end, req.description or "")
    elif req.action == "delete":
        res = await bot.calendar_service.delete_event(req.event_id)
    elif req.action == "update":
        res = await bot.calendar_service.update_event(req.event_id, summary=req.summary, description=req.description)
    else: res = "不明なアクションです"
    return {"status": "success", "message": res}

class GTaskActionRequest(BaseModel):
    action: str
    task_id: Optional[str] = None
    title: str = None
    completed: bool = None
    list_name: str = None

@router.post("/google_tasks_action", dependencies=[Depends(verify_api_key)])
async def google_tasks_action(req: GTaskActionRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service: raise HTTPException(status_code=503, detail="タスクサービス未設定")

    if req.action == "add": res = await bot.tasks_service.add_task(req.title, list_name=req.list_name)
    elif req.action == "delete": res = await bot.tasks_service.delete_task(req.task_id, list_name=req.list_name)
    elif req.action == "update": res = await bot.tasks_service.update_task(req.task_id, title=req.title, list_name=req.list_name)
    elif req.action == "toggle": res = await bot.tasks_service.update_task(req.task_id, completed=req.completed, list_name=req.list_name)
    else: res = "不明なアクションです"
    return {"status": "success", "message": res}

class GTaskMoveRequest(BaseModel):
    task_id: str
    previous_task_id: Optional[str] = None
    list_name: str = None

@router.post("/google_tasks_move", dependencies=[Depends(verify_api_key)])
async def google_tasks_move(req: GTaskMoveRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service: raise HTTPException(status_code=503, detail="タスクサービス未設定")
    res = await bot.tasks_service.move_task(req.task_id, req.previous_task_id, req.list_name)
    return {"status": "success", "message": res}

@router.get("/sleep_trend", dependencies=[Depends(verify_api_key)])
async def sleep_trend():
    from api import app
    import asyncio as _asyncio
    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"trend": []}
    fitbit_cog = bot.get_cog("FitbitCog")
    if not fitbit_cog or not fitbit_cog.is_ready:
        return {"trend": []}

    async def fetch_day(i):
        date = datetime.datetime.now(JST).date() - datetime.timedelta(days=i)
        try:
            stats = await fitbit_cog.fitbit_service.get_stats(date)
            if stats:
                return {
                    "date": date.strftime("%m/%d"),
                    "score": stats.get("sleep_score"),
                    "duration": stats.get("total_sleep_minutes"),
                }
        except Exception:
            pass
        return {"date": date.strftime("%m/%d"), "score": None, "duration": None}

    results = await _asyncio.gather(*[fetch_day(i) for i in range(6, -1, -1)])
    return {"trend": list(results)}

@router.post("/execute_tool", dependencies=[Depends(verify_api_key)])
async def execute_tool(req: BaseModel):
    pass

@router.post("/daily_report", dependencies=[Depends(verify_api_key)])
async def daily_report():
    return {"message": "日次整理が完了しました。"}

@router.get("/habits", dependencies=[Depends(verify_api_key)])
async def get_habits():
    from api import app
    import datetime
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog: return {"habits": [], "today_done": [], "streaks": {}}

    data = await habit_cog._load_data()
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    today_logs = data.get("logs", {}).get(today_str, [])

    habits_list = []
    streaks = {}
    for h in data.get("habits", []):
        habits_list.append({"id": h["id"], "name": h["name"], "frequency_days": h.get("frequency_days", 1)})
        streaks[h["id"]] = habit_cog._get_habit_stats(data, h["id"], today_str)

    return {"habits": habits_list, "today_done": today_logs, "streaks": streaks}

class HabitCompleteRequest(BaseModel): habit_name: str
@router.post("/habits/complete", dependencies=[Depends(verify_api_key)])
async def complete_habit(req: HabitCompleteRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    result_msg = await habit_cog._process_habit_completion(req.habit_name)
    return {"status": "success", "message": result_msg}

class HabitAddRequest(BaseModel):
    name: str
    frequency_days: int = 1

@router.post("/habits/add", dependencies=[Depends(verify_api_key)])
async def add_habit(req: HabitAddRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        raise HTTPException(status_code=503, detail="HabitCog不在")

    data = await habit_cog._load_data()
    existing = next((h for h in data["habits"] if h["name"].lower() == req.name.lower()), None)
    if not existing:
        existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
        new_id = str(max(existing_ids) + 1)
        data["habits"].append({"id": new_id, "name": req.name, "frequency_days": req.frequency_days})
        await habit_cog._save_data(data)

    if hasattr(bot, "tasks_service") and bot.tasks_service:
        await bot.tasks_service.add_task(req.name, list_name="習慣")

    return {"status": "success"}

@router.post("/habits/update", dependencies=[Depends(verify_api_key)])
async def update_habit(req: BaseModel):
    return {"status": "success"}

@router.post("/habits/delete", dependencies=[Depends(verify_api_key)])
async def delete_habit_endpoint(req: BaseModel):
    return {"status": "success"}

@router.get("/task_candidates", dependencies=[Depends(verify_api_key)])
async def task_candidates():
    """タスク開始用の履歴（直近10件）と、終了用の現在実行中タスクを取得"""
    from api import app
    import datetime
    from config import JST
    import re

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service: return {"start": [], "end": []}

    service = chat_service.drive_service.get_service()
    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    
    # 履歴（過去7日分からユニークなタスクを抽出）
    start_candidates = []
    end_candidates = []
    seen = set()
    
    for i in range(7):
        d = (datetime.datetime.now(JST) - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{d}.md")
        if f_id:
            content = await chat_service.drive_service.read_text_file(service, f_id)
            if "## 🎯 Tasks" in content:
                section = content.split("## 🎯 Tasks")[1].split("##")[0]
                for line in section.split("\n"):
                    match = re.search(r"- \[(.*?)\] (.*)", line)
                    if match:
                        state = match.group(1)
                        # 時刻部分を削除
                        task_name = re.sub(r"\(.*?\)", "", match.group(2)).strip()
                        if task_name and task_name not in seen:
                            start_candidates.append(task_name)
                            seen.add(task_name)
                        if state == "/" and i == 0: # 今日の実行中
                             end_candidates.append(task_name)
    
    return {
        "start": start_candidates[:10],
        "end": end_candidates
    }

@router.get("/book_notes", dependencies=[Depends(verify_api_key)])
async def get_book_notes(title: str):
    return {"title": title, "content": "Book notes content..."}

@router.get("/links", dependencies=[Depends(verify_api_key)])
async def get_links():
    return {"links": await get_all_links()}

class LinkCreateRequest(BaseModel):
    title: str = "Untitled"
    url: str = ""
    type: str = "web"

@router.post("/links", dependencies=[Depends(verify_api_key)])
async def create_link(req: LinkCreateRequest):
    """手動でのリンク（レシピ等）追加"""
    from api import app
    await add_stocked_link(req.url, req.type, req.title)
    
    links = await get_all_links()
    if not links: raise HTTPException(status_code=500)
    new_link = links[0]
    
    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, req.title, req.type, req.url)
    
    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        import asyncio
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        asyncio.create_task(backup_db_to_drive(bot.drive_service, folder_id))

    return {"status": "success", "link_id": new_link["id"]}

class LinkUpdateRequest(BaseModel):
    title: str = ""
    purpose: str = ""
    summary: str = ""
    memo: str = ""
    target_date: str = ""
    linked_note_url: str = ""
    type: str = ""
    add_to_calendar: bool = False

@router.put("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def update_link(link_id: int, req: LinkUpdateRequest):
    from api import app
    import datetime

    link = await get_link_by_id(link_id)
    if not link: raise HTTPException(status_code=404, detail="リンク未検出")

    new_title = req.title or link["title"]
    new_type = req.type or link["type"]
    
    # DB更新
    await update_link_details(link_id, new_title, req.purpose, req.summary, req.memo, req.target_date, req.linked_note_url, new_type)

    # Obsidian更新 (Drive)
    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, new_title, new_type, link["url"], req.purpose, req.target_date, req.memo, req.summary, is_update=True)

    # カレンダー追加
    if req.add_to_calendar and req.target_date:
        bot = getattr(app.state, "bot", None)
        if bot and bot.calendar_service:
            prefix = {"map": "🗺️[行]", "recipe": "🍳[食]", "book": "📚[本]"}.get(new_type, "📎[記]")
            try:
                dt = datetime.datetime.strptime(req.target_date, "%Y-%m-%d")
                bot.calendar_service.get_service().events().insert(calendarId="primary", body={
                    "summary": f"{prefix} {new_title}", "description": f"目的: {req.purpose}\nメモ: {req.memo}\nURL: {link['url']}",
                    "start": {"date": dt.strftime("%Y-%m-%d")}, "end": {"date": (dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")}
                }).execute()
            except: pass
    
    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        import asyncio
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        asyncio.create_task(backup_db_to_drive(bot.drive_service, folder_id))

    return {"status": "success"}

@router.delete("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def delete_link(link_id: int):
    from api import app
    await delete_stocked_link(link_id)
    
    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        import asyncio
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        asyncio.create_task(backup_db_to_drive(bot.drive_service, folder_id))
        
    return {"status": "success"}