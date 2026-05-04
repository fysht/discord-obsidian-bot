import os
import logging
import asyncio
import datetime
import json
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from api.database import (
    save_message, get_history, add_stocked_link, get_all_links,
    get_link_by_id, update_link_details, mark_link_as_saved,
    delete_stocked_link, get_todays_log, clear_history,
    delete_message_by_id, toggle_message_star, get_starred_messages,
    search_messages, add_push_subscription, remove_push_subscription,
    add_english_phrase, get_english_phrases, delete_english_phrase,
    set_message_label, get_labeled_messages, get_all_labels,
)
from api import notification_service
from services.info_service import InfoService
from web_parser import fetch_maps_info, parse_url_with_readability
from config import (
    JST,
    require_env,
    TIMEOUT_HTTP_SHORT,
    TIMEOUT_HTTP_DEFAULT,
    TIMEOUT_PLAYWRIGHT,
)
from utils.async_utils import safe_create_task

router = APIRouter(prefix="/api")

API_KEY = require_env("PWA_API_KEY")

_sleep_trend_cache: dict = {"data": None, "expires_at": None}
_dashboard_sleep_cache: dict = {"data": None, "expires_at": None}
_fitbit_semaphore: asyncio.Semaphore | None = None

def _get_fitbit_semaphore() -> asyncio.Semaphore:
    global _fitbit_semaphore
    if _fitbit_semaphore is None:
        _fitbit_semaphore = asyncio.Semaphore(2)
    return _fitbit_semaphore

async def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="認証に失敗しました。")

class ChatRequest(BaseModel):
    message: str
    reply_to_id: Optional[int] = None
    english_mode: bool = False

class ChatResponse(BaseModel):
    reply: str
    user_message_id: Optional[int] = None
    assistant_message_id: Optional[int] = None
    translation: Optional[str] = None

class AuthRequest(BaseModel):
    password: str

APP_PASSWORD = require_env("PWA_PASSWORD")

@router.post("/auth")
async def authenticate(req: AuthRequest):
    if req.password != APP_PASSWORD:
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
                async with session.get(oembed, timeout=aiohttp.ClientTimeout(total=TIMEOUT_HTTP_SHORT)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title", "YouTube Video")
                        recipe_kw = ["レシピ", "作り方", "材料", "献立", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw):
                            link_type = "recipe"
        except Exception as e:
            logging.debug(f"YouTube oEmbed失敗: {e}")
        return {"title": title, "type": link_type}

    if "maps.google.com" in url or "maps.app.goo.gl" in url or "goo.gl/maps" in url or "/maps/" in url:
        link_type = "map"
        try:
            place_name, _ = await fetch_maps_info(url)
            if place_name and place_name != "Google Maps Location":
                title = place_name
        except Exception as e:
            logging.debug(f"Maps情報取得失敗: {e}")
        return {"title": title, "type": link_type}

    if "amazon.co.jp" in url or "amzn.to" in url or "amazon.com" in url:
        link_type = "book"
        try:
            pw_title, _ = await asyncio.wait_for(parse_url_with_readability(url), timeout=TIMEOUT_PLAYWRIGHT)
            if pw_title and pw_title not in ("No Title Found", "Untitled", ""):
                t = pw_title
                # Amazon固有のゴミを除去
                t = _re.sub(r"Amazon\.co\.jp\s*[:：]\s*", "", t)
                t = _re.sub(r"Amazon\.com\s*[:：]\s*", "", t)
                t = _re.sub(r"\s*\|\s*Amazon.*$", "", t)
                t = _re.sub(r"\s*:\s*Amazon.*$", "", t)
                # 「 | 著者名」以降を削除
                t = _re.sub(r"\s*[|｜]\s*.+$", "", t)
                # 【...】【...】などの補足を削除
                t = _re.sub(r"\s*【[^】]*】\s*$", "", t)
                # 「：副題」など長すぎる副題を削除（40文字超の場合メインタイトルのみ）
                colon_match = _re.match(r"^(.{3,40})[：:].+$", t)
                if colon_match:
                    t = colon_match.group(1)
                title = t.strip() or title
        except Exception as e:
            logging.error(f"Amazon Playwright fetch failed for {url}: {e}")
        return {"title": title if title else "Untitled", "type": link_type}

    recipe_domains = ["cookpad.com", "kurashiru.com", "delishkitchen.tv", "macaro-ni.jp",
                      "orangepage.net", "lettuceclub.net", "kyounoryouri.jp", "ajinomoto.co.jp"]
    if any(d in url for d in recipe_domains): link_type = "recipe"

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            }
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT_HTTP_DEFAULT), allow_redirects=True) as response:
                if response.status == 200:
                    html = await response.text(errors="replace")
                    match = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
                    if match:
                        title = match.group(1).strip()[:200]
                    if link_type == "web":
                        recipe_kw = ["レシピ", "作り方", "献立", "材料", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw): link_type = "recipe"
                        elif "材料" in html[:5000] and "作り方" in html[:5000]: link_type = "recipe"
    except Exception as e: logging.error(f"Link meta fetch failed for {url}: {e}")

    return {"title": title if title else "Untitled", "type": link_type}


# --- Obsidian同期用共通関数 (英語表記統一) ---
async def sync_link_to_obsidian(chat_service, title: str, link_type: str, url: str,
                                purpose: str="", target_date: str="", memo: str="", summary: str="",
                                is_update: bool = False, old_title: str = ""):
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

    # old_titleが指定されている場合はそちらでも検索（タイトル変更時の対応）
    search_titles = [safe_title]
    if old_title and old_title != title:
        safe_old = re.sub(r'[\\/*?:"<>|]', "", old_title)[:80]
        if safe_old:
            search_titles.append(safe_old)

    try:
        drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        f_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
        if not f_id: f_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

        for search_title in search_titles:
            q_title = search_title.replace("'", "\\'")
            query = f"'{f_id}' in parents and name contains '{q_title}' and trashed = false"
            results = await asyncio.to_thread(lambda: service.files().list(q=query, fields="files(id, name)").execute())
            for f in results.get("files", []):
                fname = f["name"]
                if fname == f"{search_title}.md" or fname.endswith(f"-{search_title}.md"):
                    existing_id = f["id"]
                    target_filename = fname
                    break
            if existing_id:
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
    from api.database import backup_db_to_drive

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
            user_id = await save_message("user", req.message, reply_to=req.reply_to_id)
            asst_id = await notification_service.save_message_and_notify("assistant", reply)
            return ChatResponse(reply=reply, user_message_id=user_id, assistant_message_id=asst_id)
        except Exception as e:
            logging.error(f"Link stock failed, falling back to AI: {e}")

    bot = getattr(app.state, "bot", None)
    if not bot: raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog: raise HTTPException(status_code=503, detail="AIコアがロードされていません。")

    user_id = await save_message("user", req.message, reply_to=req.reply_to_id)

    from google.genai import types
    db_history = await get_history(limit=15)
    history_messages = []
    for m in reversed(db_history[1:]):
        role = "model" if m["role"] == "assistant" else "user"
        history_messages.append(types.Content(role=role, parts=[types.Part.from_text(text=m["content"])]))

    # 返信先のコンテキストをプロンプト前置で添付
    user_message = req.message
    translation = None
    if req.reply_to_id:
        try:
            quoted = next((m for m in db_history if m.get("id") == req.reply_to_id), None)
            if quoted:
                snippet = quoted["content"][:300]
                user_message = f"[返信先メッセージ: 「{snippet}」]\n\n{req.message}"
        except Exception as e:
            logging.debug(f"reply context attach failed: {e}")

    # ENモード: 日本語入力を英訳してからAIに送る
    import re as _re
    is_japanese = bool(_re.search(r'[ぁ-ん゠-ヿ一-鿿]', req.message))
    if req.english_mode and is_japanese:
        try:
            from google.genai import types as _types
            gemini_client = getattr(bot, "gemini_client", None)
            if gemini_client:
                trans_resp = await gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash-preview-04-17",
                    contents=f"Translate the following Japanese text to natural English. Output only the English translation, nothing else.\n\n{req.message}"
                )
                translation = trans_resp.text.strip()
                if req.reply_to_id:
                    user_message = f"[Replying to: 「{snippet}」]\n\n{translation}" if 'snippet' in dir() else translation
                else:
                    user_message = translation
        except Exception as e:
            logging.debug(f"EN mode translation failed: {e}")

    # 英語メッセージのフィードバック（ENモードOFF、英語入力時）
    english_feedback_hint = ""
    if not req.english_mode and not is_japanese and _re.search(r'[a-zA-Z]', req.message) and len(req.message) > 5:
        english_feedback_hint = "\n\n[SYSTEM HINT: The user has written in English. Naturally include brief, encouraging feedback on their English (grammar, naturalness, word choice) within your response. Keep feedback short and positive.]"

    reply = await partner_cog.generate_response_for_app(
        user_message + english_feedback_hint, history_messages, english_mode=req.english_mode
    )
    asst_id = await notification_service.save_message_and_notify("assistant", reply)

    if bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-chat",
        )

    return ChatResponse(reply=reply, user_message_id=user_id, assistant_message_id=asst_id, translation=translation)

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
                # タスク形式 ([x], [/]) と タイムライン形式の両方に対応
                cb_match = re.search(r"- \[(.)\] (.*)", line)
                if cb_match:
                    tasks.append({"text": cb_match.group(2), "done": (cb_match.group(1) == 'x')})
                else:
                    # タイムライン形式 (HH:mm - HH:mm活動名)
                    tasks.append({"text": line[2:].strip(), "is_log": True})

    alter_log = ""
    daily_journal = ""
    next_actions = ""
    mit_items: list[str] = []

    def extract_section(text, header):
        m = re.search(rf"{re.escape(header)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def extract_alter_log(text):
        m = re.search(r"## 💡 Insights & Thoughts\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m: m = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m: m = re.search(r"## 🕵️ AI Assessment\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    alter_log = extract_alter_log(content)
    daily_journal = extract_section(content, "## 📔 Daily Journal") or ""
    next_actions_raw = extract_section(content, "## 🚀 Next Actions") or ""
    next_actions = next_actions_raw
    mit_raw = extract_section(content, "## 🎯 MIT") or ""
    mit_items = [
        re.sub(r"^- \[[ x]\] ", "", l).strip()
        for l in mit_raw.splitlines()
        if l.strip().startswith("- [")
    ]

    # 当日のログがない場合、昨日のノートから取得を試みる
    yesterday_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if folder_id:
        if not alter_log or not daily_journal:
            y_fid = await chat_service.drive_service.find_file(service, folder_id, f"{yesterday_str}.md")
            if y_fid:
                try:
                    y_content = await chat_service.drive_service.read_text_file(service, y_fid)
                    if not alter_log:
                        alter_log = extract_alter_log(y_content)
                        if alter_log: alter_log = f"【昨日の分析】\n{alter_log}"
                    if not daily_journal:
                        dj = extract_section(y_content, "## 📔 Daily Journal")
                        if dj: daily_journal = f"【昨日のジャーナル】\n{dj}"
                    if not next_actions:
                        na = extract_section(y_content, "## 🚀 Next Actions")
                        if na: next_actions = na
                except Exception as e:
                    logging.debug(f"yesterday content fetch failed: {e}")

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
                if len(parts) >= 2: news.append({"title": parts[0], "link": parts[1]})
                else: news.append({"title": str(n), "link": "#"})
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
                    sleep_stats = {"score": score or "N/A", "duration": fitbit_cog._format_minutes(raw_duration) if raw_duration else "N/A"}
                    cached["data"] = sleep_stats
                    cached["expires_at"] = now_dt + datetime.timedelta(minutes=10)
            except Exception as e:
                logging.debug(f"fitbit dashboard stats failed: {e}")

    return {
        "tasks": tasks, "alter_log": alter_log, "date": display_date, "g_calendar": g_calendar,
        "google_tasks_work": google_tasks_work, "google_tasks_private": google_tasks_private,
        "habits": habits, "weather": weather_data, "news": news, "sleep": sleep_stats,
        "daily_journal": daily_journal,
        "next_actions": next_actions,
        "mit": mit_items,
    }

class TaskActionRequest(BaseModel):
    action: str
    old_text: str = ""
    new_text: str = ""
    line_index: int = -1  # ライフログ行インデックス（編集/削除用）

@router.post("/task_action", dependencies=[Depends(verify_api_key)])
async def task_action(req: TaskActionRequest):
    from api import app
    import datetime
    import re
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
    elif req.action in ("edit_log", "delete_log"):
        # ライフログ行の編集/削除（line_index で特定）
        lifelog_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        if lifelog_match:
            section_start_pos = content.index("## 🪟 Lifelog\n") + len("## 🪟 Lifelog\n")
            section_text = lifelog_match.group(1)
            section_lines = section_text.split("\n")
            log_lines = [(idx, line) for idx, line in enumerate(section_lines) if line.strip().startswith("- ")]
            if 0 <= req.line_index < len(log_lines):
                target_idx, _old_line = log_lines[req.line_index]
                if req.action == "delete_log":
                    section_lines.pop(target_idx)
                elif req.action == "edit_log" and req.new_text:
                    section_lines[target_idx] = f"- {req.new_text}"
                new_section = "\n".join(section_lines)
                content = content[:section_start_pos] + new_section + content[section_start_pos + len(section_text):]
    else:
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith("- ") and req.old_text and req.old_text in line:
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

    await clear_history()

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-reset",
        )

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
    due: Optional[str] = None  # RFC3339 (YYYY-MM-DDTHH:MM:SS.000Z) または YYYY-MM-DD

@router.post("/google_tasks_action", dependencies=[Depends(verify_api_key)])
async def google_tasks_action(req: GTaskActionRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service: raise HTTPException(status_code=503, detail="タスクサービス未設定")

    if req.action == "add":
        res = await bot.tasks_service.add_task(req.title, list_name=req.list_name, due=req.due)
    elif req.action == "delete":
        res = await bot.tasks_service.delete_task(req.task_id, list_name=req.list_name)
    elif req.action == "update":
        res = await bot.tasks_service.update_task(req.task_id, title=req.title, due=req.due, list_name=req.list_name)
    elif req.action == "toggle":
        res = await bot.tasks_service.update_task(req.task_id, completed=req.completed, list_name=req.list_name)
    else:
        res = "不明なアクションです"
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
    now_dt = datetime.datetime.now(JST)
    cached = _sleep_trend_cache
    if cached["data"] and cached["expires_at"] and now_dt < cached["expires_at"]:
        return cached["data"]

    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"trend": []}
    fitbit_cog = bot.get_cog("FitbitCog")
    if not fitbit_cog or not fitbit_cog.is_ready:
        return {"trend": []}

    async def fetch_day(i):
        date = now_dt.date() - datetime.timedelta(days=i)
        try:
            async with _get_fitbit_semaphore():
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

    results = []
    for i in range(6, -1, -1):
        results.append(await fetch_day(i))

    result = {"trend": results}
    cached["data"] = result
    # 直近2日のいずれかのスコアがnullの場合は短時間だけキャッシュ（データ同期待ち）
    recent_missing = any(results[i].get("score") is None for i in [-1, -2] if i + len(results) >= 0)
    ttl = datetime.timedelta(minutes=2) if recent_missing else datetime.timedelta(minutes=10)
    cached["expires_at"] = now_dt + ttl
    return result

@router.post("/daily_report", dependencies=[Depends(verify_api_key)])
async def daily_report():
    return {"message": "日次整理が完了しました。"}

def _parse_habit_trigger(notes: str) -> tuple[str, str]:
    """notes 先頭行が "⏰ <trigger>" なら trigger と残りに分割。なければ ('', notes)"""
    if not notes:
        return "", ""
    lines = notes.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("⏰"):
        trigger = first[1:].lstrip(" ：:").strip()
        rest = "\n".join(lines[1:]).lstrip("\n")
        return trigger, rest
    return "", notes


def _serialize_habit_notes(trigger: str, rest: str) -> str:
    trigger = (trigger or "").strip()
    rest = rest or ""
    if trigger:
        if rest:
            return f"⏰ {trigger}\n\n{rest}"
        return f"⏰ {trigger}"
    return rest


@router.get("/habits", dependencies=[Depends(verify_api_key)])
async def get_habits():
    from api import app
    import datetime
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    tasks_service = getattr(bot, "tasks_service", None) if bot else None

    if not tasks_service:
        return {"habits": [], "today_done": [], "streaks": {}}

    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")

    # Google Tasks「習慣」リストをマスターとして取得
    raw_uncompleted = await tasks_service.get_raw_tasks("習慣")
    completed_today_titles = await tasks_service.get_completed_tasks_today("習慣")

    # name -> (task_id, trigger) のマップ（未完了タスクのみ。完了タスクは notes 取得対象外）
    task_meta_by_name = {}
    for t in raw_uncompleted:
        trig, _ = _parse_habit_trigger(t.get("notes", ""))
        task_meta_by_name[t["title"]] = {"task_id": t["id"], "trigger": trig}

    # 未完了 + 今日完了済み = 今日表示すべき全習慣
    all_names = [t["title"] for t in raw_uncompleted] + completed_today_titles
    if not all_names:
        return {"habits": [], "today_done": [], "streaks": {}}

    def _meta(name):
        return task_meta_by_name.get(name, {"task_id": "", "trigger": ""})

    if not habit_cog:
        habits_list = []
        for i, n in enumerate(all_names):
            m = _meta(n)
            habits_list.append({
                "id": str(i), "name": n, "frequency_days": 1,
                "trigger": m["trigger"], "task_id": m["task_id"],
            })
        today_done = [str(i) for i, n in enumerate(all_names) if n in completed_today_titles]
        return {"habits": habits_list, "today_done": today_done, "streaks": {}}

    # HabitCog データと同期（Google Tasks にあってHabitCog にないものを追加）
    data = await habit_cog._load_data()
    changed = False
    for name in all_names:
        existing = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if not existing:
            existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
            new_id = str(max(existing_ids) + 1)
            data["habits"].append({"id": new_id, "name": name, "frequency_days": 1})
            changed = True

    # 今日の完了ログに Google Tasks 完了済みを反映
    if today_str not in data["logs"]:
        data["logs"][today_str] = []
    for name in completed_today_titles:
        matching = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if matching and matching["id"] not in data["logs"][today_str]:
            data["logs"][today_str].append(matching["id"])
            changed = True

    if changed:
        await habit_cog._save_data(data)

    today_logs = data.get("logs", {}).get(today_str, [])
    today_date = datetime.datetime.now(JST).date()

    def _is_due_today(habit_data: dict, h_id: str) -> bool:
        freq = habit_data.get("frequency_days", 1)
        if freq <= 1:
            return True
        # 直近の完了日を探す
        for i in range(1, 90):
            d = (today_date - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            if h_id in data.get("logs", {}).get(d, []):
                days_since = i
                return days_since >= freq
        return True  # 一度も完了していなければ今日が対象

    # Google Tasks の順序を維持してレスポンスを組み立てる
    habits_list = []
    streaks = {}
    for name in all_names:
        matching = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if matching:
            m = _meta(name)
            freq = matching.get("frequency_days", 1)
            due_today = _is_due_today(matching, matching["id"])
            habits_list.append({
                "id": matching["id"],
                "name": matching["name"],
                "frequency_days": freq,
                "trigger": m["trigger"],
                "task_id": m["task_id"],
                "due_today": due_today,
            })
            streaks[matching["id"]] = habit_cog._get_habit_stats(data, matching["id"], today_str)

    return {"habits": habits_list, "today_done": today_logs, "streaks": streaks}

class HabitCompleteRequest(BaseModel): habit_name: str
@router.post("/habits/complete", dependencies=[Depends(verify_api_key)])
async def complete_habit(req: HabitCompleteRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"status": "error", "message": "HabitCog not available"}
    result_msg = await habit_cog.complete_habit(req.habit_name)
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
    raise HTTPException(status_code=501, detail="この機能は未実装です。")


class HabitTriggerRequest(BaseModel):
    habit_name: str
    trigger: str = ""


@router.post("/habits/trigger", dependencies=[Depends(verify_api_key)])
async def set_habit_trigger(req: HabitTriggerRequest):
    """習慣の trigger（いつやるか）を Google Tasks の notes に保存する"""
    from api import app
    bot = getattr(app.state, "bot", None)
    tasks_service = getattr(bot, "tasks_service", None) if bot else None
    if not tasks_service:
        raise HTTPException(status_code=503, detail="タスクサービス未設定")

    raw = await tasks_service.get_raw_tasks("習慣")
    target = next((t for t in raw if t["title"] == req.habit_name), None)
    if not target:
        target = next((t for t in raw if req.habit_name.lower() in t["title"].lower()), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"習慣「{req.habit_name}」が見つかりません")

    _, rest = _parse_habit_trigger(target.get("notes", ""))
    new_notes = _serialize_habit_notes(req.trigger, rest)

    res = await tasks_service.update_task(
        target["id"], notes=new_notes, list_name="習慣"
    )
    return {"status": "success", "message": res, "trigger": req.trigger.strip()}

@router.post("/habits/delete", dependencies=[Depends(verify_api_key)])
async def delete_habit_endpoint(req: BaseModel):
    raise HTTPException(status_code=501, detail="この機能は未実装です。")

@router.get("/habits/history", dependencies=[Depends(verify_api_key)])
async def get_habit_history(days: int = 28):
    import datetime as dt
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"history": []}
    data = await habit_cog._load_data()
    today = dt.datetime.now(JST).date()
    total_habits = len(data.get("habits", []))
    history = []
    for i in range(days - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        done = len(data.get("logs", {}).get(d_str, []))
        rate = (done / total_habits) if total_habits > 0 else 0.0
        history.append({"date": d.strftime("%m/%d"), "rate": round(rate, 2), "done": done, "total": total_habits})
    return {"history": history}

@router.get("/task_candidates", dependencies=[Depends(verify_api_key)])
async def task_candidates():
    """タスク開始用はGoogle Tasksの「タスク候補」リストから、終了用はLifelogの実行中タスクを取得"""
    from api import app
    import datetime
    from config import JST
    import re

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service: return {"start": [], "end": []}

    # --- 開始候補: Google Tasks「タスク候補」リスト ---
    start_candidates = []
    tasks_service = getattr(chat_service, "tasks_service", None)
    if tasks_service:
        try:
            raw_tasks = await tasks_service.get_raw_tasks("タスク候補")
            start_candidates = [t["title"] for t in raw_tasks if t.get("title")]
        except Exception as e:
            logging.debug(f"タスク候補リスト取得失敗: {e}")

    # --- 終了候補: 今日のLifelogから実行中（▶）のタスクを抽出 ---
    end_candidates = []
    if chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
            if folder_id:
                today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
                if f_id:
                    content = await chat_service.drive_service.read_text_file(service, f_id)
                    lifelog_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
                    if lifelog_match:
                        for line in lifelog_match.group(1).split("\n"):
                            line = line.strip()
                            if "▶" in line:
                                # "- HH:MM ▶ タスク名" からタスク名を抽出
                                m = re.search(r"▶\s*(.+)$", line)
                                if m:
                                    end_candidates.append(m.group(1).strip())
        except Exception as e:
            logging.debug(f"終了候補取得失敗: {e}")

    return {
        "start": start_candidates,
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
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-link-create",
        )

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
    tags: str = ""

@router.put("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def update_link(link_id: int, req: LinkUpdateRequest):
    from api import app
    import datetime

    link = await get_link_by_id(link_id)
    if not link: raise HTTPException(status_code=404, detail="リンク未検出")

    old_title = link["title"] or ""
    new_title = req.title or old_title
    new_type = req.type or link["type"]
    existing_cal_event_id = link.get("calendar_event_id", "")

    # カレンダー処理（重複防止）
    new_cal_event_id = existing_cal_event_id
    if req.add_to_calendar and req.target_date:
        bot = getattr(app.state, "bot", None)
        if bot and bot.calendar_service:
            prefix = {"map": "🗺️[行]", "recipe": "🍳[食]", "book": "📚[本]"}.get(new_type, "📎[記]")
            cal_body = {
                "summary": f"{prefix} {new_title}",
                "description": f"目的: {req.purpose}\nメモ: {req.memo}\nURL: {link['url']}",
                "start": {"date": req.target_date},
                "end": {"date": (datetime.datetime.strptime(req.target_date, "%Y-%m-%d") + datetime.timedelta(days=1)).strftime("%Y-%m-%d")},
            }
            try:
                cal_svc = bot.calendar_service.get_service()
                if existing_cal_event_id:
                    cal_svc.events().update(calendarId="primary", eventId=existing_cal_event_id, body=cal_body).execute()
                else:
                    result = cal_svc.events().insert(calendarId="primary", body=cal_body).execute()
                    new_cal_event_id = result.get("id", "")
            except Exception as e:
                logging.warning(f"link calendar add/update failed: {e}")

    # DB更新
    await update_link_details(link_id, new_title, req.purpose, req.summary, req.memo, req.target_date, req.linked_note_url, new_type, req.tags, new_cal_event_id)

    # Obsidian更新 (Drive) — old_titleを渡してUntitled→新タイトルの更新に対応
    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, new_title, new_type, link["url"], req.purpose, req.target_date, req.memo, req.summary, is_update=True, old_title=old_title)

    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-link-update",
        )

    return {"status": "success"}

@router.delete("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def delete_link(link_id: int):
    from api import app
    await delete_stocked_link(link_id)

    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-link-delete",
        )

    return {"status": "success"}


# ===== 手書きメモ読み取り・保存 =====

class NoteFromImageRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    hint: str = ""

@router.post("/note_from_image", dependencies=[Depends(verify_api_key)])
async def note_from_image(req: NoteFromImageRequest):
    import base64
    from google.genai import types
    from api import app

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足情報: {req.hint}" if req.hint else ""
    prompt = f"""この手書きメモの画像を読み取り、以下のJSON形式で返してください。
文字が読みにくい場合は文脈から補完してください。{hint_text}

{{
  "transcription": "文字起こし（原文に近い形）",
  "structured_content": "整理・構造化した内容（Markdown形式。箇条書きや見出しを使い読みやすく。必要なら補足も加える）",
  "category": "work か study か idea か task か other のいずれか",
  "subject": "categoryがstudyの場合の科目名（例: 数学、英語）。それ以外は空文字",
  "action_items": ["タスク・TODOがあれば文字列の配列で。なければ空配列"]
}}"""

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = types.Part.from_text(text=prompt)
        response = await bot.gemini_client.aio.models.generate_content(
            model="gemini-2.5-pro",
            contents=types.Content(role="user", parts=[image_part, text_part]),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"note_from_image error: {e}")
        raise HTTPException(status_code=500, detail=f"読み取りに失敗しました: {str(e)}")


@router.get("/notes/list", dependencies=[Depends(verify_api_key)])
async def get_notes_list():
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"notes": []}

    service = chat_service.drive_service.get_service()
    if not service:
        return {"notes": []}

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    notes = []

    # 今日のデイリーノートを先頭に固定
    notes.append({
        "id": "TODAY_DAILY",
        "name": f"今日のデイリーノート ({today_str})",
        "folder": "DailyNotes",
        "filename": f"{today_str}.md",
    })

    # StudyLogs 内のファイルを取得
    try:
        study_folder = await chat_service.drive_service.find_file(service, drive_root, "StudyLogs")
        if study_folder:
            results = await asyncio.to_thread(
                lambda: service.files().list(
                    q=f"'{study_folder}' in parents and trashed = false",
                    fields="files(id, name)",
                    orderBy="modifiedTime desc",
                ).execute()
            )
            for f in results.get("files", []):
                display = f["name"].replace("_ノート.md", "").replace(".md", "")
                notes.append({
                    "id": f["id"],
                    "name": display,
                    "folder": "StudyLogs",
                    "filename": f["name"],
                })
    except Exception as e:
        logging.error(f"notes/list StudyLogs error: {e}")

    return {"notes": notes}


class SaveNoteRequest(BaseModel):
    mode: str  # "new" or "append"
    content: str
    action_items: List[str] = []
    # 新規の場合
    title: str = ""
    category: str = "other"
    subject: str = ""
    # 追記の場合
    target_id: str = ""
    target_folder: str = ""
    target_filename: str = ""

@router.post("/save_note", dependencies=[Depends(verify_api_key)])
async def save_note(req: SaveNoteRequest):
    from api import app
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    now = datetime.datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.strftime("%Y-%m-%d")

    try:
        if req.mode == "append":
            folder_name = req.target_folder
            filename = req.target_filename

            folder_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
            if not folder_id:
                folder_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

            file_id = await chat_service.drive_service.find_file(service, folder_id, filename)

            if file_id:
                existing = await chat_service.drive_service.read_text_file(service, file_id)
            else:
                existing = f"# {filename.replace('.md','')}\n"

            if folder_name == "DailyNotes":
                section = "## 💡 Insights & Thoughts"
            else:
                section = "## 📝 Learning Log"

            new_content = update_section(existing, f"*{now_str} 追記*\n{req.content}", section)

            if file_id:
                await chat_service.drive_service.update_text(service, file_id, new_content)
            else:
                await chat_service.drive_service.upload_text(service, folder_id, filename, new_content)

        else:
            # 新規作成
            title = req.title or f"メモ_{now_str}"

            if req.category == "study":
                folder_name = "StudyLogs"
                subject = req.subject or title
                filename = f"{subject}_ノート.md"
                initial = (
                    f"---\ntitle: {subject} 学習ノート\ndate: {today_str}\ntags: [study]\n---\n\n"
                    f"# {subject} 学習ノート\n\n## 📝 Learning Log\n"
                )
                section = "## 📝 Learning Log"
            else:
                folder_name = "Notes"
                filename = f"{now.strftime('%Y%m%d%H%M%S')}-{title[:40]}.md"
                initial = f"---\ntitle: {title}\ndate: {today_str}\n---\n\n# {title}\n"
                section = None

            folder_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
            if not folder_id:
                folder_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

            existing_id = await chat_service.drive_service.find_file(service, folder_id, filename)

            if existing_id:
                existing = await chat_service.drive_service.read_text_file(service, existing_id)
                if section:
                    new_content = update_section(existing, f"*{now_str}*\n{req.content}", section)
                else:
                    new_content = existing + f"\n\n*{now_str}*\n{req.content}\n"
                await chat_service.drive_service.update_text(service, existing_id, new_content)
            else:
                if section:
                    new_content = update_section(initial, f"*{now_str}*\n{req.content}", section)
                else:
                    new_content = initial + f"\n{req.content}\n\nSaved: {now_str}\n"
                await chat_service.drive_service.upload_text(service, folder_id, filename, new_content)

        # action_items を Google Tasks に追加
        if req.action_items and bot and getattr(bot, "tasks_service", None):
            list_name = "仕事" if req.category == "work" else "プライベート"
            for item in req.action_items:
                if item.strip():
                    try:
                        await bot.tasks_service.add_task(item.strip(), list_name=list_name)
                    except Exception as e:
                        logging.error(f"save_note task add error: {e}")

    except Exception as e:
        logging.error(f"save_note error: {e}")
        raise HTTPException(status_code=500, detail=f"保存に失敗しました: {str(e)}")

    return {"status": "success"}


# ===== メッセージ操作 (削除 / star / 検索) =====

@router.delete("/messages/{message_id}", dependencies=[Depends(verify_api_key)])
async def delete_message(message_id: int):
    """会話履歴から1件削除し、Driveバックアップを起動。"""
    from api import app
    from api.database import backup_db_to_drive

    ok = await delete_message_by_id(message_id)
    if not ok:
        raise HTTPException(status_code=404, detail="該当メッセージが見つかりません。")

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-msg-delete",
        )
    return {"status": "success"}


@router.post("/messages/{message_id}/star", dependencies=[Depends(verify_api_key)])
async def star_message(message_id: int):
    """お気に入りトグル。新しい状態を返す。"""
    from api import app
    from api.database import backup_db_to_drive

    new_state = await toggle_message_star(message_id)
    if new_state is None:
        raise HTTPException(status_code=404, detail="該当メッセージが見つかりません。")

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-msg-star",
        )
    return {"status": "success", "starred": new_state}


@router.get("/messages/starred", dependencies=[Depends(verify_api_key)])
async def list_starred_messages(limit: int = 100):
    return {"messages": await get_starred_messages(limit=limit)}


@router.get("/messages/search", dependencies=[Depends(verify_api_key)])
async def search_messages_endpoint(q: str = "", limit: int = 50):
    if not q.strip():
        return {"results": []}
    rows = await search_messages(q.strip(), limit=limit)
    return {"results": rows}


# ===== 手書きメモ: 複数画像対応 =====

class ImagePayload(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"


class NoteFromImagesRequest(BaseModel):
    images: List[ImagePayload]
    hint: str = ""


@router.post("/note_from_images", dependencies=[Depends(verify_api_key)])
async def note_from_images(req: NoteFromImagesRequest):
    """複数画像を1ノートに統合読み取り。"""
    import base64
    from google.genai import types
    from api import app

    if not req.images:
        raise HTTPException(status_code=400, detail="画像が指定されていません。")

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足情報: {req.hint}" if req.hint else ""
    prompt = f"""これら {len(req.images)} 枚の手書きメモ画像をまとめて読み取り、1つのノートとして以下のJSON形式で返してください。
画像の順番がメモの流れを表します。読みにくい箇所は文脈から補完してください。{hint_text}

{{
  "transcription": "全画像を統合した文字起こし（原文に近い形）",
  "structured_content": "整理・構造化した内容（Markdown形式。箇条書きや見出しを使い、複数画像の内容を統合する）",
  "category": "work か study か idea か task か other のいずれか",
  "subject": "categoryがstudyの場合の科目名（例: 数学、英語）。それ以外は空文字",
  "action_items": ["タスク・TODOがあれば文字列の配列で。なければ空配列"]
}}"""

    try:
        parts = []
        for img in req.images:
            image_bytes = base64.b64decode(img.image_base64)
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=img.mime_type))
        parts.append(types.Part.from_text(text=prompt))
        response = await bot.gemini_client.aio.models.generate_content(
            model="gemini-2.5-pro",
            contents=types.Content(role="user", parts=parts),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"note_from_images error: {e}")
        raise HTTPException(status_code=500, detail=f"読み取りに失敗しました: {str(e)}")


# ===== ストックリンク一括既読化 =====

class LinkBulkStatusRequest(BaseModel):
    link_ids: List[int]
    status: str = "saved"


# ===== MIT (Most Important Tasks) =====

class MitSetRequest(BaseModel):
    items: List[str]


@router.post("/mit_set", dependencies=[Depends(verify_api_key)])
async def mit_set(req: MitSetRequest):
    """今日の MIT を DailyNote の `## 🎯 MIT` セクションに書き込む。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    msg = await partner_cog._set_mit_to_obsidian(req.items)
    return {"status": "success", "message": msg}


@router.post("/mit_rollover", dependencies=[Depends(verify_api_key)])
async def mit_rollover():
    """今日の未達 MIT を翌日に持ち越す。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    msg = await partner_cog._rollover_mit()
    return {"status": "success", "message": msg}


# ===== Web Push 通知 =====

class PushSubscriptionRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


@router.get("/vapid_public_key")
async def vapid_public_key():
    """VAPID 公開鍵を返す。サブスクリプション時にフロントが SW に渡す。
    認証不要にしているのは、未ログイン状態でも SW 登録時に取得したいため
    （秘密性は無く、漏れても問題ない値）。"""
    return {"key": notification_service.get_public_key(), "configured": notification_service.is_configured()}


@router.post("/push/subscribe", dependencies=[Depends(verify_api_key)])
async def push_subscribe(req: PushSubscriptionRequest):
    if not req.endpoint or not req.p256dh or not req.auth:
        raise HTTPException(status_code=400, detail="購読情報が不完全です。")
    await add_push_subscription(req.endpoint, req.p256dh, req.auth)
    return {"status": "success"}


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@router.post("/push/unsubscribe", dependencies=[Depends(verify_api_key)])
async def push_unsubscribe(req: PushUnsubscribeRequest):
    await remove_push_subscription(req.endpoint)
    return {"status": "success"}


@router.post("/push/test", dependencies=[Depends(verify_api_key)])
async def push_test():
    """通知テスト送信。設定確認用。"""
    count = await notification_service.send_push("通知テスト", "通知が届けば設定はOKだよ！")
    return {"status": "success", "delivered": count}


@router.post("/links/bulk_status", dependencies=[Depends(verify_api_key)])
async def bulk_update_link_status(req: LinkBulkStatusRequest):
    """複数リンクのステータスを一括更新する。"""
    from api import app
    from api.database import backup_db_to_drive

    if not req.link_ids:
        return {"status": "success", "updated": 0}
    # mark_link_as_saved は単一更新だが、一括で繰り返す（件数高々100オーダー想定）
    for lid in req.link_ids:
        try:
            await mark_link_as_saved(lid)
        except Exception as e:
            logging.warning(f"bulk_status update {lid} failed: {e}")

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-bulk-status",
        )
    return {"status": "success", "updated": len(req.link_ids)}


# ===== タスク分解 (AI Task Breakdown) =====

class TaskBreakdownRequest(BaseModel):
    message: str


class TaskBreakdownApplyRequest(BaseModel):
    list_name: str = "プライベート"
    subtasks: List[dict]
    parent_title: Optional[str] = ""


@router.get("/tasks_for_breakdown", dependencies=[Depends(verify_api_key)])
async def tasks_for_breakdown():
    """既存タスクから分解候補を返す（仕事＋プライベート、未完了のみ）"""
    from api import app
    bot = getattr(app.state, "bot", None)
    tasks_service = getattr(bot, "tasks_service", None) if bot else None
    if not tasks_service:
        return {"tasks": []}

    result = []
    for list_name in ["仕事", "プライベート"]:
        try:
            raw = await tasks_service.get_raw_tasks(list_name)
            for t in raw:
                result.append({
                    "id": t["id"],
                    "title": t["title"],
                    "list_name": list_name,
                })
        except Exception as e:
            logging.debug(f"tasks_for_breakdown list {list_name}: {e}")
    return {"tasks": result}


@router.post("/task_breakdown", dependencies=[Depends(verify_api_key)])
async def task_breakdown(req: TaskBreakdownRequest):
    """親タスクをAIでサブタスクに分解する。"""
    from api import app
    from prompts import PROMPT_TASK_BREAKDOWN
    from google.genai import types as gtypes

    parent = (req.message or "").strip()
    if not parent:
        raise HTTPException(status_code=400, detail="タスク内容を指定してください")

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = PROMPT_TASK_BREAKDOWN.replace("{parent_task}", parent)
    try:
        response = await bot.gemini_client.aio.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        data = json.loads(response.text or "{}")
        subtasks = data.get("subtasks", [])
        if not isinstance(subtasks, list):
            subtasks = []
        return {"subtasks": subtasks, "parent": parent}
    except Exception as e:
        logging.error(f"task_breakdown error: {e}")
        raise HTTPException(status_code=500, detail=f"タスク分解に失敗しました: {str(e)}")


@router.post("/task_breakdown/apply", dependencies=[Depends(verify_api_key)])
async def task_breakdown_apply(req: TaskBreakdownApplyRequest):
    """分解結果を Google Tasks に追加する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service:
        raise HTTPException(status_code=503, detail="タスクサービス未設定")

    list_name = req.list_name or "プライベート"
    added = 0
    for st in req.subtasks:
        title = (st.get("title") or "").strip()
        if not title:
            continue
        estimate = st.get("estimate")
        notes = f"⏱ {estimate}" if estimate else ""
        if req.parent_title:
            notes = f"親: {req.parent_title}\n{notes}".strip()
        try:
            await bot.tasks_service.add_task(title, list_name=list_name, notes=notes)
            added += 1
        except TypeError:
            await bot.tasks_service.add_task(title, list_name=list_name)
            added += 1
        except Exception as e:
            logging.error(f"task add error: {e}")

    return {"status": "success", "added": added, "message": f"{added}件をタスクに追加したよ！"}


# ===== 読書機能 (Reading) =====

class ReadingMemoRequest(BaseModel):
    book_title: str
    memo: str


class ReadingPromptRequest(BaseModel):
    book_title: str
    previous_prompts: List[str] = []


@router.get("/reading/books", dependencies=[Depends(verify_api_key)])
async def reading_books():
    """読書候補となる書籍一覧を返す。
    1) ストック済みリンクの type=='book'
    2) BookNotes フォルダ内の既存ノート（過去に読書ログがある書籍）"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)

    books_from_links = []
    try:
        links = await get_all_links()
        for link in links:
            if link.get("type") == "book":
                books_from_links.append({
                    "title": link.get("title", "Untitled"),
                    "source": "stock",
                    "link_id": link.get("id"),
                    "url": link.get("url", ""),
                })
    except Exception as e:
        logging.debug(f"links fetch error: {e}")

    books_from_notes = []
    if chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            if service:
                folder_id = await chat_service.drive_service.find_file(
                    service, chat_service.drive_folder_id, "BookNotes"
                )
                if folder_id:
                    query = f"'{folder_id}' in parents and mimeType='text/markdown' and trashed=false"
                    results = await asyncio.to_thread(
                        lambda: service.files().list(
                            q=query, fields="files(id, name, modifiedTime)",
                            orderBy="modifiedTime desc", pageSize=30
                        ).execute()
                    )
                    for f in results.get("files", []):
                        title = f["name"].replace(".md", "")
                        if not any(b["title"] == title for b in books_from_links):
                            books_from_notes.append({
                                "title": title,
                                "source": "notes",
                                "link_id": None,
                                "url": "",
                            })
        except Exception as e:
            logging.debug(f"book notes fetch: {e}")

    return {"books": books_from_links + books_from_notes}


@router.post("/reading/save", dependencies=[Depends(verify_api_key)])
async def reading_save(req: ReadingMemoRequest):
    """読書メモを書籍ノートに保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    book_cog = bot.get_cog("BookCog") if bot else None
    if not book_cog:
        raise HTTPException(status_code=503, detail="BookCog不在")

    title = (req.book_title or "").strip() or "無題の書籍"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")

    ok = await book_cog.append_book_memo(title, memo)
    if not ok:
        raise HTTPException(status_code=500, detail="保存に失敗しました")
    return {"status": "success", "message": f"「{title}」のノートに保存したよ。"}


@router.post("/reading/prompt", dependencies=[Depends(verify_api_key)])
async def reading_prompt(req: ReadingPromptRequest):
    """読書中のマネージャーからの問いかけを生成する。"""
    from api import app
    from prompts import PROMPT_BOOK_READING_PROMPT
    from google.genai import types as gtypes

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prev = "\n".join(f"- {p}" for p in (req.previous_prompts or [])) or "（まだなし）"
    prompt = PROMPT_BOOK_READING_PROMPT.replace(
        "{book_title}", req.book_title or "無題"
    ).replace("{previous_prompts}", prev)

    try:
        response = await bot.gemini_client.aio.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
        )
        text = (response.text or "").strip()
        return {"prompt": text}
    except Exception as e:
        logging.error(f"reading_prompt error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== 勉強機能 (Study) =====

class StudyMemoRequest(BaseModel):
    subject: str
    memo: str


@router.get("/study/subjects", dependencies=[Depends(verify_api_key)])
async def study_subjects():
    """既存の学習科目一覧を返す。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"subjects": []}

    service = chat_service.drive_service.get_service()
    if not service:
        return {"subjects": []}

    subjects = []
    try:
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "StudyLogs"
        )
        if folder_id:
            query = f"'{folder_id}' in parents and trashed=false"
            results = await asyncio.to_thread(
                lambda: service.files().list(
                    q=query, fields="files(id, name, modifiedTime)",
                    orderBy="modifiedTime desc", pageSize=30
                ).execute()
            )
            for f in results.get("files", []):
                name = f["name"]
                # ファイル名は "{subject}_ノート.md" 形式
                subject = name.replace("_ノート.md", "").replace(".md", "")
                subjects.append(subject)
    except Exception as e:
        logging.debug(f"study subjects fetch: {e}")
    return {"subjects": subjects}


@router.post("/study/save", dependencies=[Depends(verify_api_key)])
async def study_save(req: StudyMemoRequest):
    """学習メモを科目ノートに保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    study_cog = bot.get_cog("StudyCog") if bot else None
    if not study_cog:
        raise HTTPException(status_code=503, detail="StudyCog不在")

    subject = (req.subject or "").strip() or "雑記"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")

    ok = await study_cog.append_study_memo(subject, memo)
    if not ok:
        raise HTTPException(status_code=500, detail="保存に失敗しました")
    return {"status": "success", "message": f"「{subject}」の学習ノートに保存したよ。"}


# ===== ゼロ秒思考機能 (Zero Second Thinking) =====

class ZTThemeRequest(BaseModel):
    context: str = ""


class ZTDeepDiveRequest(BaseModel):
    original_theme: str
    user_memo: str


class ZTSaveRequest(BaseModel):
    theme: str
    memo: str
    session_id: Optional[str] = None  # 同一セッション（深掘り含む）を1つのノートに集約


@router.post("/zerosec/themes", dependencies=[Depends(verify_api_key)])
async def zerosec_themes(req: ZTThemeRequest):
    """ゼロ秒思考のテーマ候補を5つ返す。"""
    from api import app
    from prompts import PROMPT_ZT_THEMES_DETAILED
    from google.genai import types as gtypes

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = PROMPT_ZT_THEMES_DETAILED.replace(
        "{context}", req.context or "（特になし）"
    )
    try:
        response = await bot.gemini_client.aio.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        data = json.loads(response.text or "{}")
        themes = data.get("themes", [])
        if not isinstance(themes, list):
            themes = []
        return {"themes": themes[:5]}
    except Exception as e:
        logging.error(f"zerosec_themes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/zerosec/deep_dive", dependencies=[Depends(verify_api_key)])
async def zerosec_deep_dive(req: ZTDeepDiveRequest):
    """ユーザーが書いたメモから、深掘り用の追加テーマを5つ生成する。"""
    from api import app
    from prompts import PROMPT_ZT_DEEP_DIVE
    from google.genai import types as gtypes

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = (PROMPT_ZT_DEEP_DIVE
              .replace("{original_theme}", req.original_theme or "")
              .replace("{user_memo}", req.user_memo or ""))
    try:
        response = await bot.gemini_client.aio.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        data = json.loads(response.text or "{}")
        themes = data.get("themes", [])
        if not isinstance(themes, list):
            themes = []
        return {"themes": themes[:5]}
    except Exception as e:
        logging.error(f"zerosec_deep_dive error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/zerosec/save", dependencies=[Depends(verify_api_key)])
async def zerosec_save(req: ZTSaveRequest):
    """ゼロ秒思考のメモをノートに保存し、ライフログにも記録する。
    session_id があれば同一ファイルに追記、なければ新規作成。"""
    from api import app
    import re as _re

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    theme = (req.theme or "").strip() or "無題のテーマ"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")

    folder_id = await chat_service.drive_service.find_file(
        service, chat_service.drive_folder_id, "ZeroSecondThinking"
    )
    if not folder_id:
        folder_id = await chat_service.drive_service.create_folder(
            service, chat_service.drive_folder_id, "ZeroSecondThinking"
        )

    now = datetime.datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    # session_id が指定されていれば既存ファイルに追記
    session_id = req.session_id or now.strftime("%Y%m%d%H%M%S")
    safe_theme = _re.sub(r'[\\/*?:"<>|]', "", theme)[:60]
    file_name = f"{today_str}_{session_id}_{safe_theme}.md"

    # 既存セッションの場合は session_id プレフィックスでファイル検索
    existing_id = None
    if req.session_id:
        try:
            query = (f"'{folder_id}' in parents and trashed=false "
                     f"and name contains '{session_id}'")
            results = await asyncio.to_thread(
                lambda: service.files().list(
                    q=query, fields="files(id, name)"
                ).execute()
            )
            files = results.get("files", [])
            if files:
                existing_id = files[0]["id"]
                file_name = files[0]["name"]
        except Exception as e:
            logging.debug(f"zt existing file lookup: {e}")

    formatted_memo = memo.replace("\n", "\n> ")
    section_block = (
        f"\n## 🧠 {time_str} {theme}\n\n> {formatted_memo}\n"
    )

    if existing_id:
        existing = await chat_service.drive_service.read_text_file(service, existing_id)
        new_content = (existing or "").rstrip() + "\n" + section_block
        await chat_service.drive_service.update_text(service, existing_id, new_content)
    else:
        header = (
            f"---\ntitle: ゼロ秒思考 {today_str} {time_str}\n"
            f"date: {today_str}\ntags: [zero_second_thinking]\n---\n\n"
            f"# ゼロ秒思考セッション ({today_str} {time_str})\n"
            + section_block
        )
        await chat_service.drive_service.upload_text(service, folder_id, file_name, header)

    # ライフログにも記録（PartnerCog 経由）
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if partner_cog:
        try:
            await partner_cog._log_life_activity_to_obsidian(
                f"ゼロ秒思考: {theme[:30]}", "end"
            )
        except Exception as e:
            logging.debug(f"zt lifelog error: {e}")

    return {
        "status": "success",
        "session_id": session_id,
        "message": f"「{theme}」のメモを保存したよ。",
    }


@router.post("/zerosec/log_start", dependencies=[Depends(verify_api_key)])
async def zerosec_log_start(req: ZTThemeRequest):
    """ゼロ秒思考の開始をライフログに記録する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if not partner_cog:
        return {"status": "skipped"}
    try:
        theme = (req.context or "テーマ未指定")[:30]
        await partner_cog._log_life_activity_to_obsidian(
            f"ゼロ秒思考: {theme}", "start"
        )
    except Exception as e:
        logging.debug(f"zt start log: {e}")
    return {"status": "success"}


# ===== タスク整理 =====

class TaskTriageRequest(BaseModel):
    list_name: str = "仕事"

@router.post("/task_triage", dependencies=[Depends(verify_api_key)])
async def task_triage(req: TaskTriageRequest):
    """指定リストのタスクをAIに整理提案させる。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not getattr(chat_service, "tasks_service", None):
        raise HTTPException(status_code=503, detail="Tasks サービス未接続")

    tasks = await chat_service.tasks_service.get_raw_tasks(req.list_name)
    if not tasks:
        return {"reply": f"「{req.list_name}」リストに未完了タスクがないよ。"}

    task_list_str = "\n".join(
        f"- {t['title']}" + (f" (締切: {t['due'][:10]})" if t.get('due') else "")
        for t in tasks
    )

    gemini_client = getattr(bot, "gemini_client", None) if bot else None
    if not gemini_client:
        return {"reply": f"「{req.list_name}」のタスク一覧:\n{task_list_str}\n\n（AI分析は現在利用できません）"}

    prompt = (
        f"あなたはタスク管理のプロフェッショナルです。\n"
        f"以下の「{req.list_name}」リストのタスクを分析し、整理提案をしてください。\n\n"
        f"タスク一覧:\n{task_list_str}\n\n"
        f"以下の観点で提案してください:\n"
        f"1. 優先度（高/中/低）の分類\n"
        f"2. 完了・削除を推奨するタスク\n"
        f"3. グループ化・統合できるタスク\n"
        f"4. 今日取り組むべきタスクのおすすめ\n\n"
        f"簡潔かつ実用的に日本語で回答してください。"
    )

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        reply = response.text.strip() if response.text else "分析に失敗しました。"
    except Exception as e:
        logging.error(f"Task triage AI error: {e}")
        reply = f"AI分析でエラーが発生しました。\n\nタスク一覧:\n{task_list_str}"

    return {"reply": reply}


# ===== ロケーションログ 手動同期 =====

class LocationSyncRequest(BaseModel):
    date: str = ""
    date_from: str = ""
    date_to: str = ""


@router.post("/location_log/sync", dependencies=[Depends(verify_api_key)])
async def location_log_sync(req: LocationSyncRequest):
    """指定日付（または日付範囲）のロケーションログをGoogle DriveのTimeline JSONから同期する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    cog = bot.get_cog("LocationLogCog")
    if not cog:
        raise HTTPException(status_code=503, detail="LocationLogCogが利用できません。")

    # 日付範囲が指定された場合は各日をループ同期
    date_from = (req.date_from or "").strip()
    date_to = (req.date_to or "").strip()
    single_date = (req.date or "").strip()

    if date_from and date_to:
        try:
            start = datetime.datetime.strptime(date_from, "%Y-%m-%d")
            end = datetime.datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="日付形式が不正です (YYYY-MM-DD)")
        if (end - start).days > 14:
            raise HTTPException(status_code=400, detail="最大14日間まで同期できます")
        if end < start:
            start, end = end, start

        results = []
        current = start
        while current <= end:
            d_str = current.strftime("%Y-%m-%d")
            try:
                r = await cog.perform_manual_sync(d_str)
                results.append(f"{d_str}: {r}")
            except Exception as e:
                results.append(f"{d_str}: エラー - {e}")
            current += datetime.timedelta(days=1)
        return {"status": "success", "message": "\n".join(results)}
    else:
        target_date = single_date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
        result = await cog.perform_manual_sync(target_date)
        return {"status": "success", "message": result}


# ===== 天気場所 =====

@router.get("/weather", dependencies=[Depends(verify_api_key)])
async def get_weather_data(location: str = ""):
    """指定場所の天気データを返す。locationはYahoo!天気コード (例: 33/6710)。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    info_svc = getattr(bot, "info_service", None) if bot else None
    if not info_svc:
        info_svc = InfoService()
    loc = location.strip() or None
    data = await info_svc.get_weather(location=loc)
    return data


@router.get("/weather/locations")
async def get_weather_locations():
    """利用可能な天気の場所一覧を都道府県別の階層構造で返す。"""
    from services.info_service import YAHOO_WEATHER_BY_PREFECTURE
    prefectures = []
    for pref_name, regions in YAHOO_WEATHER_BY_PREFECTURE.items():
        prefectures.append({
            "name": pref_name,
            "regions": regions,
        })
    return {"prefectures": prefectures}


# ===== 英語フレーズ帳 =====

class PhraseSaveRequest(BaseModel):
    phrase: str
    translation: str = ""
    context: str = ""

@router.get("/english_phrases", dependencies=[Depends(verify_api_key)])
async def list_english_phrases():
    phrases = await get_english_phrases()
    return {"phrases": phrases}

@router.post("/english_phrases", dependencies=[Depends(verify_api_key)])
async def save_english_phrase(req: PhraseSaveRequest):
    phrase_id = await add_english_phrase(req.phrase.strip(), req.translation.strip(), req.context.strip())
    return {"id": phrase_id}

@router.delete("/english_phrases/{phrase_id}", dependencies=[Depends(verify_api_key)])
async def remove_english_phrase(phrase_id: int):
    deleted = await delete_english_phrase(phrase_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="フレーズが見つかりません")
    return {"deleted": True}


class TranslateSaveRequest(BaseModel):
    text: str

@router.post("/english_phrases/translate_and_save", dependencies=[Depends(verify_api_key)])
async def translate_and_save_phrase(req: TranslateSaveRequest):
    """ユーザーのテキスト（日本語）を英訳してフレーズ帳に保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.gemini_client:
        raise HTTPException(status_code=503, detail="AIサービス未接続")
    try:
        resp = await bot.gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash-preview-04-17",
            contents=f"Translate the following Japanese text to natural, everyday English. Output only the English translation.\n\n{req.text}"
        )
        translation = resp.text.strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"翻訳に失敗しました: {e}")

    phrase_id = await add_english_phrase(translation, req.text, req.text[:300])
    return {"id": phrase_id, "phrase": translation, "translation": req.text}


# ===== メッセージコレクション =====

class LabelRequest(BaseModel):
    label: str

@router.post("/messages/{message_id}/label", dependencies=[Depends(verify_api_key)])
async def set_label(message_id: int, req: LabelRequest):
    ok = await set_message_label(message_id, req.label.strip())
    if not ok:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません")
    return {"ok": True, "label": req.label.strip()}

@router.get("/messages/collections", dependencies=[Depends(verify_api_key)])
async def list_collections():
    labels = await get_all_labels()
    return {"collections": labels}

@router.get("/messages/labeled", dependencies=[Depends(verify_api_key)])
async def labeled_messages(label: str = ""):
    if not label:
        raise HTTPException(status_code=400, detail="labelを指定してください")
    msgs = await get_labeled_messages(label)
    return {"messages": msgs, "label": label}


# ===== Fitbit全データ =====

@router.get("/fitbit_all_data", dependencies=[Depends(verify_api_key)])
async def fitbit_all_data(days: int = 14):
    """過去N日分のFitbitデータを返す（最大30日）。"""
    from api import app
    days = max(1, min(days, 30))
    bot = getattr(app.state, "bot", None)
    fitbit_cog = bot.get_cog("FitbitCog") if bot else None
    if not fitbit_cog or not fitbit_cog.is_ready:
        return {"data": []}

    now_dt = datetime.datetime.now(JST)
    results = []
    for i in range(days - 1, -1, -1):
        date = now_dt.date() - datetime.timedelta(days=i)
        try:
            async with _get_fitbit_semaphore():
                stats = await fitbit_cog.fitbit_service.get_stats(date)
            if stats:
                raw_dur = stats.get("total_sleep_minutes")
                results.append({
                    "date": date.strftime("%m/%d"),
                    "date_full": date.strftime("%Y-%m-%d"),
                    "sleep_score": stats.get("sleep_score"),
                    "sleep_duration": fitbit_cog._format_minutes(raw_dur) if raw_dur else None,
                    "steps": stats.get("steps"),
                    "calories": stats.get("calories"),
                })
            else:
                results.append({"date": date.strftime("%m/%d"), "date_full": date.strftime("%Y-%m-%d")})
        except Exception:
            results.append({"date": date.strftime("%m/%d"), "date_full": date.strftime("%Y-%m-%d")})

    return {"data": results}


# ===== ブリーフィング =====

@router.post("/briefing", dependencies=[Depends(verify_api_key)])
async def briefing():
    """朝（12時前）はモーニングブリーフィング、午後以降はイブニングレビューを生成する。"""
    from api import app
    import datetime

    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    now = datetime.datetime.now(JST)
    is_morning = now.hour < 12
    briefing_type = "morning" if is_morning else "evening"

    # コンテキスト収集
    context_parts = []

    # 今日の予定
    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        try:
            events = await chat_service.calendar_service.get_raw_events_for_date(now.strftime("%Y-%m-%d"))
            if events:
                ev_str = "\n".join(f"- {e.get('summary', '?')} ({e.get('start_time', '?')}〜{e.get('end_time', '?')})" for e in events[:10])
                context_parts.append(f"今日の予定:\n{ev_str}")
        except Exception:
            pass

    # タスク
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

    # 天気
    try:
        info_svc = getattr(bot, "info_service", None)
        if info_svc:
            w = await info_svc.get_weather()
            if w and w.get("summary") not in ("取得失敗", None):
                context_parts.append(f"天気: {w.get('summary', '不明')} (最高{w.get('max_temp','--')}℃ / 最低{w.get('min_temp','--')}℃)")
    except Exception:
        pass

    # ライフログ（夕方レビュー用）
    if not is_morning and chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
            if folder_id:
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{now.strftime('%Y-%m-%d')}.md")
                if f_id:
                    content = await chat_service.drive_service.read_text_file(service, f_id)
                    import re
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
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        reply = response.text.strip() if response.text else "ブリーフィング生成に失敗しました。"
    except Exception as e:
        logging.error(f"Briefing AI error: {e}")
        reply = f"AI生成でエラーが発生しました。\n\n{context}"

    return {"reply": reply, "type": briefing_type}