import os
import logging
import datetime
import json
import re
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from api.database import (
    save_message, 
    get_history, 
    add_stocked_link, 
    get_all_links, 
    get_link_by_id, 
    update_link_details, 
    mark_link_as_saved, 
    delete_stocked_link,
    get_todays_log,
    clear_history
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
    title = "Untitled"
    link_type = "web"

    if "youtube.com" in url or "youtu.be" in url:
        link_type = "youtube"
        try:
            oembed = f"https://www.youtube.com/oembed?url={url}&format=json"
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
        
    if any(d in url for d in ["goo.gl/maps", "/maps/", "google.com/maps"]):
        link_type = "map"
        try:
            place_name, _ = await fetch_maps_info(url)
            if place_name and place_name != "Google Maps Location":
                title = place_name
        except Exception: pass
        return {"title": title, "type": link_type}
        
    if "amazon.co.jp" in url or "amzn.to" in url:
        link_type = "book"

    recipe_domains = ["cookpad.com", "kurashiru.com", "delishkitchen.tv", "macaro-ni.jp", "orangepage.net", "lettuceclub.net", "kyounoryouri.jp", "ajinomoto.co.jp"]
    if any(d in url for d in recipe_domains): link_type = "recipe"

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as response:
                if response.status == 200:
                    html = await response.text(errors="replace")
                    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                    if match:
                        title = match.group(1).strip()[:200]
                    if link_type == "book":
                        title = title.replace("Amazon.co.jp:", "").replace("Amazon |", "").strip()
                    if link_type == "web":
                        recipe_kw = ["レシピ", "作り方", "献立", "材料", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw): link_type = "recipe"
                        elif "材料" in html[:5000] and "作り方" in html[:5000]: link_type = "recipe"
    except Exception as e:
        logging.error(f"Link meta fetch failed for {url}: {e}")

    return {"title": title or "Untitled", "type": link_type}

@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(req: ChatRequest):
    from api import app
    url_match = re.search(r"https?://[^\s]+", req.message)
    if url_match:
        url = url_match.group(0)
        try:
            meta = await _fetch_link_meta(url)
            await add_stocked_link(url, meta["type"], meta["title"])
            type_label = {"web": "🌐 ウェブ", "youtube": "📺 YouTube", "recipe": "🍳 レシピ", "map": "🗺️ マップ", "book": "📚 書籍"}.get(meta["type"], "🔗 リンク")
            reply = f"「{meta['title']}」を{type_label}としてストックしました。"
            await save_message("user", req.message)
            await save_message("assistant", reply)
            return ChatResponse(reply=reply)
        except Exception as e:
            logging.error(f"Link stock failed: {e}")

    bot = getattr(app.state, "bot", None)
    if not bot: raise HTTPException(status_code=503, detail="Botエンジン未初期化")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog: raise HTTPException(status_code=503, detail="AIコア未ロード")

    await save_message("user", req.message)
    from google.genai import types
    db_history = await get_history(limit=30)
    history_messages = []
    for m in reversed(db_history[1:]): 
        role = "model" if m["role"] == "assistant" else "user"
        history_messages.append(types.Content(role=role, parts=[types.Part.from_text(text=m["content"])]))

    reply = await partner_cog.generate_response_for_app(req.message, history_messages)
    await save_message("assistant", reply)

    if bot.drive_service:
        import asyncio
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        asyncio.create_task(backup_db_to_drive(bot.drive_service, folder_id))

    return ChatResponse(reply=reply)

@router.get("/history", dependencies=[Depends(verify_api_key)])
async def history(limit: int = 30):
    messages = await get_history(limit=limit)
    return {"messages": messages}

@router.get("/dashboard", dependencies=[Depends(verify_api_key)])
async def dashboard():
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service: return {"tasks": [], "alter_log": "", "error": "未接続"}

    service = chat_service.drive_service.get_service()
    if not service: return {"tasks": [], "alter_log": ""}

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
        except: pass

    try:
        info = getattr(bot, "info_service", InfoService())
        weather_data = await info.get_weather()
        raw_news = await info.get_news(limit=5)
        news = [{"title": n.split('\n')[0], "link": n.split('\n')[1] if '\n' in n else "#"} for n in raw_news]
    except:
        weather_data = {"summary": "取得失敗"}
        news = []

    sleep_stats = {"score": "N/A", "duration": "N/A"}
    fitbit_cog = bot.get_cog("FitbitCog")
    if fitbit_cog and fitbit_cog.is_ready:
        try:
            stats = await fitbit_cog.fitbit_service.get_stats(now.date())
            if stats: sleep_stats = {"score": stats.get("sleep_score") or "N/A", "duration": fitbit_cog._format_minutes(stats.get("total_sleep_minutes"))}
        except: pass

    return {
        "tasks": tasks, "alter_log": alter_log, "date": display_date,
        "g_calendar": g_calendar, "google_tasks_work": google_tasks_work,
        "google_tasks_private": google_tasks_private, "habits": habits,
        "weather": weather_data, "news": news, "sleep": sleep_stats
    }

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
    from api.database import update_link_details
    from utils.obsidian_utils import update_section

    link = await get_link_by_id(link_id)
    if not link: raise HTTPException(status_code=404, detail="リンク未検出")

    new_title = req.title or link["title"]
    # ★修正箇所： `new_title` を引数に追加
    await update_link_details(link_id, new_title, req.purpose, req.summary, req.memo, req.target_date, req.linked_note_url, req.type or link["type"])

    now = datetime.datetime.now(JST)
    chat_service = getattr(app.state, "chat_service", None)
    
    if chat_service and chat_service.drive_service:
        service = chat_service.drive_service.get_service()
        if service:
            link_type = req.type or link["type"]
            url = link["url"]
            folder_map = {"youtube": "YouTube", "recipe": "Recipes", "web": "WebClips", "map": "Places", "book": "BookNotes"}
            section_map = {"youtube": "## 📺 YouTube", "recipe": "## 🍳 Recipes", "web": "## 🔗 WebClips", "map": "## 🔗 WebClips", "book": "## 📖 Reading Log"}
            
            folder_name = folder_map.get(link_type, "WebClips")
            section_header = section_map.get(link_type, "## 🔗 WebClips")
            safe_title = re.sub(r'[\\/*?:"<>|]', "", new_title)[:80] or "Untitled"
            
            if link_type == "book":
                filename = f"{safe_title}.md"
                link_str = f"- [[{folder_name}/{safe_title}|{new_title}]]"
            else:
                timestamp = now.strftime("%Y%m%d%H%M%S")
                filename = f"{timestamp}-{safe_title}.md"
                link_str = f"- [[{folder_name}/{timestamp}-{safe_title}|{new_title}]]"

            daily_note_date = now.strftime("%Y-%m-%d")
            note_content = f"# {new_title}\n\n"
            if req.purpose: note_content += f"**🎯 目的:** {req.purpose}\n"
            if req.target_date: note_content += f"**📅 予定日:** {req.target_date}\n"
            if req.memo: note_content += f"**📝 メモ:** {req.memo}\n"
            if req.summary: note_content += f"**💡 要約:**\n{req.summary}\n"
            note_content += f"---\n## リンク\n{url}\n\n---\nSaved: {now.strftime('%Y-%m-%d %H:%M')}\n[[{daily_note_date}]]"

            try:
                drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
                f_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
                if not f_id: f_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

                existing = await chat_service.drive_service.find_file(service, f_id, filename) if link_type == "book" else None
                if existing: await chat_service.drive_service.update_text(service, existing, note_content)
                else: await chat_service.drive_service.upload_text(service, f_id, filename, note_content)

                daily_fid = await chat_service.drive_service.find_file(service, drive_root, "DailyNotes")
                if daily_fid:
                    df_id = await chat_service.drive_service.find_file(service, daily_fid, f"{daily_note_date}.md")
                    if df_id:
                        cur = await chat_service.drive_service.read_text_file(service, df_id)
                        if link_str not in cur: await chat_service.drive_service.update_text(service, df_id, update_section(cur, link_str, section_header))
                    else:
                        initial_content = f"---\ndate: {daily_note_date}\n---\n\n# Daily Note {daily_note_date}\n\n{section_header}\n{link_str}\n"
                        await chat_service.drive_service.upload_text(service, daily_fid, f"{daily_note_date}.md", initial_content)
            except Exception as e: logging.error(f"Obsidian Sync Error: {e}")

    if req.add_to_calendar and req.target_date:
        bot = getattr(app.state, "bot", None)
        if bot and bot.calendar_service:
            prefix = {"map": "🗺️[行]", "recipe": "🍳[食]", "book": "📚[本]"}.get(link_type, "📎[記]")
            try:
                dt = datetime.datetime.strptime(req.target_date, "%Y-%m-%d")
                bot.calendar_service.get_service().events().insert(calendarId="primary", body={
                    "summary": f"{prefix} {new_title}", "description": f"目的: {req.purpose}\nメモ: {req.memo}\nURL: {url}",
                    "start": {"date": dt.strftime("%Y-%m-%d")}, "end": {"date": (dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")}
                }).execute()
            except: pass

    return {"status": "success"}

@router.delete("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def delete_link(link_id: int):
    await delete_stocked_link(link_id)
    return {"status": "success"}

@router.get("/history", dependencies=[Depends(verify_api_key)])
async def get_history_endpoint(limit: int = 30):
    return {"messages": await get_history(limit=limit)}

@router.post("/reset_history", dependencies=[Depends(verify_api_key)])
async def reset_history_endpoint():
    await clear_history()
    return {"status": "success"}

# --- 以降、既存の他のエンドポイント ---