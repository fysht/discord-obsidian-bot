import os
import logging
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

    # Amazon等の厳しいサイトのタイトル取得確率を上げるためのヘッダー強化
    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            }
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as response:
                if response.status == 200:
                    html = await response.text(errors="replace")
                    match = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
                    if match:
                        # タグを除去して取得
                        title = _re.sub(r'<[^>]+>', '', match.group(1)).strip()[:200]
                        # Amazonや不要なドメイン表記のクリーンアップ
                        if link_type == "book" or "amazon" in url:
                            title = title.replace("Amazon.co.jp:", "").replace("Amazon |", "").replace("Amazon.co.jp :", "").strip()
                    
                    if link_type == "web":
                        recipe_kw = ["レシピ", "作り方", "献立", "材料", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw): link_type = "recipe"
                        elif "材料" in html[:5000] and "作り方" in html[:5000]: link_type = "recipe"
    except Exception as e: logging.error(f"Link meta fetch failed for {url}: {e}")

    return {"title": title if title else "Untitled", "type": link_type}


# --- Obsidian同期用共通関数 (英語表記統一・Markdown巨大化修正・増殖防止) ---
async def sync_link_to_obsidian(chat_service, title: str, link_type: str, url: str, 
                                purpose: str="", target_date: str="", memo: str="", summary: str="", 
                                added_at_str: str=None, old_title: str=None, old_type: str=None):
    """リンク情報をObsidianに作成・更新する"""
    if not chat_service or not chat_service.drive_service: return
    service = chat_service.drive_service.get_service()
    if not service: return
    
    import re
    # added_at_str（ストック追加日時）があればそれを基準にタイムスタンプを生成（ファイル名固定のため）
    if added_at_str:
        try:
            base_time = datetime.datetime.fromisoformat(added_at_str)
        except ValueError:
            base_time = datetime.datetime.now(JST)
    else:
        base_time = datetime.datetime.now(JST)

    now = datetime.datetime.now(JST)

    folder_map = {"youtube": "YouTube", "recipe": "Recipes", "web": "WebClips", "map": "Google Maps", "book": "Books"}
    section_map = {"youtube": "## 📺 YouTube", "recipe": "## 🍳 Recipes", "web": "## 🔗 WebClips", "map": "## 🗺️ Google Maps", "book": "## 📚 Books"}
    
    # 古いタイトルと古い種類から、上書きすべきファイル名を特定
    search_title = old_title if old_title else title
    search_type = old_type if old_type else link_type
    search_folder_name = folder_map.get(search_type, "WebClips")
    safe_search_title = re.sub(r'[\\/*?:"<>|]', "", search_title)[:80] or "Untitled"
    
    if search_type == "book":
        filename = f"{safe_search_title}.md"
    else:
        timestamp = base_time.strftime("%Y%m%d%H%M%S")
        filename = f"{timestamp}-{safe_search_title}.md"

    # 新しいリンク文字列やセクション
    new_folder_name = folder_map.get(link_type, "WebClips")
    new_section_header = section_map.get(link_type, "## 🔗 WebClips")
    new_link_str = f"- [[{search_folder_name}/{filename}|{title}]]" # ファイル名は変わらないのでsearch_folderを使う
    old_link_str = f"- [[{search_folder_name}/{filename}|{search_title}]]"

    # デイリーノートの対象日は「追加日時」とする（過去分を修正した際に本日のデイリーに飛ばないようにするため）
    daily_note_date = base_time.strftime("%Y-%m-%d")
    
    # 英語表記に統一しつつ、見出し線(---)の前に必ず空行(\n\n)を入れることで巨大化バグを防ぐ
    note_content = f"# {title}\n\n"
    if purpose: note_content += f"**🎯 Purpose:** {purpose}\n\n"
    if target_date: note_content += f"**📅 Target Date:** {target_date}\n\n"
    if memo: note_content += f"**📝 Memo:**\n{memo}\n\n"
    if summary: note_content += f"**💡 Summary:**\n{summary}\n\n"
    if url: note_content += f"---\n\n## Link\n{url}\n\n"
    note_content += f"---\n\nSaved: {now.strftime('%Y-%m-%d %H:%M')}\n[[{daily_note_date}]]\n"

    try:
        drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        f_id = await chat_service.drive_service.find_file(service, drive_root, search_folder_name)
        if not f_id: f_id = await chat_service.drive_service.create_folder(service, drive_root, search_folder_name)

        # 既存ファイルの検索・更新
        existing = await chat_service.drive_service.find_file(service, f_id, filename)
        if existing:
            await chat_service.drive_service.update_text(service, existing, note_content)
        else:
            await chat_service.drive_service.upload_text(service, f_id, filename, note_content)

        # デイリーノートの追記・移動
        daily_fid = await chat_service.drive_service.find_file(service, drive_root, "DailyNotes")
        if daily_fid:
            df_id = await chat_service.drive_service.find_file(service, daily_fid, f"{daily_note_date}.md")
            from utils.obsidian_utils import update_section
            if df_id:
                cur = await chat_service.drive_service.read_text_file(service, df_id)
                # タイトルやカテゴリが変更された場合、古い記述を消して新しい場所に移動
                if old_title and (old_title != title or old_type != link_type):
                    if old_link_str in cur:
                        cur = cur.replace(old_link_str + "\n", "")
                        cur = cur.replace(old_link_str, "")
                        cur = update_section(cur, new_link_str, new_section_header)
                        await chat_service.drive_service.update_text(service, df_id, cur)
                else:
                    # 新規または変更なし
                    if new_link_str not in cur:
                        cur = update_section(cur, new_link_str, new_section_header)
                        await chat_service.drive_service.update_text(service, df_id, cur)
            else:
                initial_content = f"---\ndate: {daily_note_date}\n---\n\n# Daily Note {daily_note_date}\n\n{new_section_header}\n{new_link_str}\n"
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

            # 投稿直後の最新リンクを取得して added_at を正しく同期に渡す
            links = await get_all_links()
            if links:
                new_link = links[0]
                chat_service = getattr(app.state, "chat_service", None)
                if chat_service:
                    await sync_link_to_obsidian(chat_service, meta["title"], meta["type"], url, added_at_str=new_link["added_at"])

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
    db_history = await get_history(limit=30)
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
async def history(limit: int = 30):
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
            if line.startswith("- [x]"): tasks.append({"text": line[6:].strip(), "done": True})
            elif line.startswith("- [/]"): tasks.append({"text": line[6:].strip(), "done": False})

    alter_log = ""
    alter_match = re.search(r"## 💡 Insights & Thoughts\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not alter_match: alter_match = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not alter_match: alter_match = re.search(r"## 🕵️ AI Assessment\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    
    if alter_match: alter_log = alter_match.group(1).strip()
    else: alter_log = "本日の観察ログはまだ生成されていません。"

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

    # === 天気・ニュース取得の強化 ===
    try:
        # 直接インスタンス化して取得を試みる
        info_svc = InfoService()
        weather_data = await info_svc.get_weather()
        
        raw_news = await info_svc.get_news(limit=5)
        news = []
        for n in raw_news:
            parts = n.split('\n')
            if len(parts) >= 2: news.append({"title": parts[0], "link": parts[1]})
            else: news.append({"title": n, "link": "#"})
    except Exception as e:
        logging.error(f"Weather/News fetch ERROR: {e}")
        weather_data = {"summary": "取得失敗"}
        news = []

    # === Fitbitデータ取得の強化 ===
    fitbit_cog = bot.get_cog("FitbitCog") if bot else None
    if fitbit_cog and fitbit_cog.is_ready:
        try:
            # 日付文字列（YYYY-MM-DD）として渡す
            target_date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            stats = await fitbit_cog.fitbit_service.get_stats(target_date_str)
            if stats:
                score = stats.get("sleep_score")
                raw_duration = stats.get("total_sleep_minutes")
                sleep_stats = {"score": score or "N/A", "duration": fitbit_cog._format_minutes(raw_duration) if raw_duration else "N/A"}
        except Exception as e:
             logging.error(f"API Dashboard Fitbit fetch error: {e}")
             pass

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
    await clear_history()
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

@router.post("/habits/update", dependencies=[Depends(verify_api_key)])
async def update_habit(req: BaseModel):
    return {"status": "success"}

@router.post("/habits/delete", dependencies=[Depends(verify_api_key)])
async def delete_habit_endpoint(req: BaseModel):
    return {"status": "success"}

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
    from api import app
    await add_stocked_link(req.url, req.type, req.title)
    
    links = await get_all_links()
    if not links: raise HTTPException(status_code=500)
    new_link = links[0]
    
    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, req.title, req.type, req.url, added_at_str=new_link["added_at"])
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

    old_type = link["type"]
    old_title = link["title"]
    new_title = req.title or old_title
    new_type = req.type or old_type
    
    await update_link_details(link_id, new_title, req.purpose, req.summary, req.memo, req.target_date, req.linked_note_url, new_type)

    chat_service = getattr(app.state, "chat_service", None)
    if chat_service:
        await sync_link_to_obsidian(
            chat_service, new_title, new_type, link["url"], 
            req.purpose, req.target_date, req.memo, req.summary, 
            link["added_at"], old_title, old_type
        )

    if req.add_to_calendar and req.target_date:
        bot = getattr(app.state, "bot", None)
        if bot and bot.calendar_service:
            prefix = {"map": "🗺️[行]", "recipe": "🍳[食]", "book": "📚[本]"}.get(new_type, "📎[記]")
            color_map = {"map": "10", "recipe": "11", "book": "9"}
            color_id = color_map.get(new_type, "1")
            try:
                dt = datetime.datetime.strptime(req.target_date, "%Y-%m-%d")
                bot.calendar_service.get_service().events().insert(calendarId="primary", body={
                    "summary": f"{prefix} {new_title}", 
                    "description": f"目的: {req.purpose}\nメモ: {req.memo}\nURL: {link['url']}",
                    "start": {"date": dt.strftime("%Y-%m-%d")}, 
                    "end": {"date": (dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")},
                    "colorId": color_id
                }).execute()
            except: pass

    return {"status": "success"}

@router.delete("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def delete_link(link_id: int):
    await delete_stocked_link(link_id)
    return {"status": "success"}