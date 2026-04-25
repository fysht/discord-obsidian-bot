import os
import logging
import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from api.database import save_message, get_history
from services.info_service import InfoService

router = APIRouter(prefix="/api")

# 簡易認証: 環境変数 PWA_API_KEY と照合
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
    """簡易認証: パスワードを検証してAPIキーを返す"""
    app_password = os.getenv("PWA_PASSWORD", "secretary")
    if req.password != app_password:
        raise HTTPException(status_code=401, detail="パスワードが正しくありません。")
    return {"api_key": API_KEY}


async def _fetch_link_meta(url: str) -> dict:
    """URLからタイトルと分類を取得する"""
    import aiohttp
    import re as _re

    title = "Untitled"
    link_type = "web"

    # YouTube判定
    if "youtube.com" in url or "youtu.be" in url:
        link_type = "youtube"
        try:
            oembed = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(oembed, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title", "YouTube Video")
                        
                        # タイトルからレシピ判定
                        recipe_kw = ["レシピ", "作り方", "材料", "献立", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw):
                            link_type = "recipe"
        except Exception:
            pass
        return {"title": title, "type": link_type}
        
    # Google Maps判定
    if "maps.google.com" in url or "maps.app.goo.gl" in url or "goo.gl/maps" in url:
        link_type = "map"
        
    # Amazon判定
    if "amazon.co.jp" in url or "amzn.to" in url:
        link_type = "book"

    # レシピドメイン判定
    recipe_domains = ["cookpad.com", "kurashiru.com", "delishkitchen.tv", "macaro-ni.jp",
                      "orangepage.net", "lettuceclub.net", "kyounoryouri.jp", "ajinomoto.co.jp"]
    if any(d in url for d in recipe_domains):
        link_type = "recipe"

    # HTMLからタイトル取得
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as response:
                if response.status == 200:
                    html = await response.text(errors="replace")
                    match = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
                    if match:
                        title = match.group(1).strip()[:200]
                    # Amazonタイトルのクリーンアップ
                    if link_type == "book":
                        title = title.replace("Amazon.co.jp:", "").replace("Amazon |", "").strip()
                    # タイトルやHTMLからレシピを追加判定
                    if link_type == "web":
                        recipe_kw = ["レシピ", "作り方", "献立", "材料", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw):
                            link_type = "recipe"
                        elif "材料" in html[:5000] and "作り方" in html[:5000]:
                            link_type = "recipe"
    except Exception as e:
        logging.error(f"Link meta fetch failed for {url}: {e}")

    return {"title": title if title else "Untitled", "type": link_type}


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(req: ChatRequest):
    """メッセージを送信してAIの応答を取得"""
    from api import app
    import re
    from api.database import add_stocked_link

    # URLが含まれているかチェックし、ストックに回す
    url_match = re.search(r"https?://[^\s]+", req.message)
    if url_match:
        url = url_match.group(0)

        try:
            # タイトルと分類を同期で取得してからDB保存
            meta = await _fetch_link_meta(url)
            await add_stocked_link(url, meta["type"], meta["title"])

            type_label = {"web": "🌐 ウェブ", "youtube": "📺 YouTube", "recipe": "🍳 レシピ"}.get(meta["type"], "🔗 リンク")
            reply = f"「{meta['title']}」を{type_label}としてストックしました。"
            await save_message("user", req.message)
            await save_message("assistant", reply)
            return ChatResponse(reply=reply)
        except Exception as e:
            logging.error(f"Link stock failed, falling back to AI: {e}")
            # ストックに失敗した場合は通常のAI応答にフォールバック


    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")

    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="AIコアがロードされていません。")

    # ユーザーメッセージを保存 (DB)
    await save_message("user", req.message)

    # 履歴を取得してContentの形式に変換
    from google.genai import types
    db_history = await get_history(limit=30)
    history_messages = []
    # 最新のメッセージ(今保存したもの)を除いた過去ログを渡す
    for m in reversed(db_history[1:]): 
        role = "model" if m["role"] == "assistant" else "user"
        history_messages.append(types.Content(role=role, parts=[types.Part.from_text(text=m["content"])]))

    # AI応答を生成 (PartnerCogの全21機能を利用)
    reply = await partner_cog.generate_response_for_app(req.message, history_messages)

    # AIの応答を保存
    await save_message("assistant", reply)

    # Google Driveへのバックアップを非同期で実行 (ユーザーを待たせない)
    import asyncio
    from api.database import backup_db_to_drive
    if bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        asyncio.create_task(backup_db_to_drive(bot.drive_service, folder_id))

    return ChatResponse(reply=reply)


@router.get("/history", dependencies=[Depends(verify_api_key)])
async def history(limit: int = 30):
    """会話履歴を取得"""
    messages = await get_history(limit=limit)
    return {"messages": messages}


@router.get("/dashboard", dependencies=[Depends(verify_api_key)])
async def dashboard():
    """今日のダッシュボードデータを取得"""
    from api import app
    import datetime
    from config import JST
    import re

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
    # 表示用には「年月」と曜日を含める
    display_date = f"{now.year}年{now.month}月{now.day}日 ({weekdays[now.weekday()]})"
    # ファイル検索用には曜日を含めない
    today_str = now.strftime("%Y-%m-%d")

    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    content = ""
    if folder_id:
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if f_id:
            try:
                content = await chat_service.drive_service.read_text_file(service, f_id)
            except Exception:
                pass

    # Lifelog（旧Tasks相当）セクションの抽出
    tasks = []
    task_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if task_match:
        for line in task_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- [x]"):
                tasks.append({"text": line[6:].strip(), "done": True})
            elif line.startswith("- [/]"):
                tasks.append({"text": line[6:].strip(), "done": False})

    # 観察日記 (Alter Log / Insights & Thoughts) の抽出
    alter_log = ""
    # 新しい名称 (Insights & Thoughts) を優先
    alter_match = re.search(r"## 💡 Insights & Thoughts\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not alter_match:
        # 以前の名称 (Alter Log)
        alter_match = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not alter_match:
        # さらに以前の名称 (AI Assessment) へのフォールバック
        alter_match = re.search(r"## 🕵️ AI Assessment\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    
    if alter_match:
        alter_log = alter_match.group(1).strip()
    else:
        alter_log = "本日の観察ログはまだ生成されていません。"

    # Google Calendar
    g_calendar = []
    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        g_calendar = await chat_service.calendar_service.get_raw_events_for_date(today_str)

    # Google Tasks (仕事 / プライベート)
    google_tasks_work = []
    google_tasks_private = []
    habits = []
    if hasattr(chat_service, "tasks_service") and chat_service.tasks_service:
        try:
            google_tasks_work = await chat_service.tasks_service.get_raw_tasks("仕事")
            google_tasks_private = await chat_service.tasks_service.get_raw_tasks("プライベート")
            habits = await chat_service.tasks_service.get_raw_tasks("習慣")
        except Exception as te:
            logging.error(f"Dashboard Google Tasks Fetch Error: {te}")

    try:
        weather_data = await bot.info_service.get_weather() if hasattr(bot, "info_service") else await InfoService().get_weather()
        raw_news = await (bot.info_service.get_news(limit=5) if hasattr(bot, "info_service") else InfoService().get_news(limit=5))
        news = []
        for n in raw_news:
            parts = n.split('\n')
            if len(parts) >= 2:
                news.append({"title": parts[0], "link": parts[1]})
            else:
                news.append({"title": n, "link": "#"})
    except Exception as e:
        import traceback
        logging.error(f"Dashboard Weather/News fetch ERROR: {e}\n{traceback.format_exc()}")
        weather_data = {"summary": "取得失敗 (Except)"}
        news = []

    # Fitbitデータ取得
    fitbit_cog = bot.get_cog("FitbitCog")
    if fitbit_cog and fitbit_cog.is_ready:
        try:
            target_date = datetime.datetime.now(JST).date()
            stats = await fitbit_cog.fitbit_service.get_stats(target_date)
            if stats:
                score = stats.get("sleep_score")
                raw_duration = stats.get("total_sleep_minutes")
                duration = fitbit_cog._format_minutes(raw_duration) if raw_duration else "N/A"
                sleep_stats = {"score": score or "N/A", "duration": duration}
        except Exception as e:
            logging.error(f"API Dashboard Fitbit fetch error: {e}")

    return {
        "tasks": tasks, 
        "alter_log": alter_log, 
        "date": display_date,
        "g_calendar": g_calendar,
        "google_tasks_work": google_tasks_work,
        "google_tasks_private": google_tasks_private,
        "habits": habits,
        "weather": weather_data,
        "news": news,
        "sleep": sleep_stats
    }

class TaskActionRequest(BaseModel):
    action: str  # 'create', 'update', 'delete', 'toggle'
    old_text: str = ""
    new_text: str = ""

@router.post("/task_action", dependencies=[Depends(verify_api_key)])
async def task_action(req: TaskActionRequest):
    """Obsidian (DailyNote) のタスクセクションを操作"""
    from api import app
    import datetime
    from config import JST
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    # ファイル名用（曜日なし）
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    file_name = f"{today_str}.md"
    f_id = await chat_service.drive_service.find_file(service, folder_id, file_name)

    content = f"# Daily Note {today_str}\n"
    if f_id:
        try:
            content = await chat_service.drive_service.read_text_file(service, f_id)
        except Exception: pass

    if req.action == "create":
        append_text = f"- [/] {req.new_text}"
        content = update_section(content, append_text, "## 🪟 Lifelog")
    else:
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith("- [") and req.old_text in line:
                if req.action == "delete": lines.pop(i)
                elif req.action == "update":
                    prefix = line[:6]
                    if "[" in prefix and "]" in prefix:
                        lines[i] = f"{prefix}{req.new_text}"
                    else:
                        lines[i] = line.replace(req.old_text, req.new_text, 1)
                elif req.action == "toggle":
                    if "- [x]" in line: lines[i] = line.replace("- [x]", "- [/]", 1)
                    else: lines[i] = line.replace("- [/]", "- [x]", 1)
                break
        content = '\n'.join(lines)

    if f_id: await chat_service.drive_service.update_text(service, f_id, content)
    else: await chat_service.drive_service.upload_text(service, folder_id, file_name, content)
    return {"status": "success"}

@router.post("/reset_history", dependencies=[Depends(verify_api_key)])
async def reset_history():
    from api.database import clear_history
    await clear_history()
    return {"status": "success"}

class CalendarActionRequest(BaseModel):
    action: str # 'add', 'update', 'delete'
    event_id: Optional[str] = None
    summary: str = None
    description: str = None
    start_time: str = None # 'YYYY-MM-DD HH:MM:S' または 'YYYY-MM-DD'
    end_time: str = None

@router.post("/calendar_action", dependencies=[Depends(verify_api_key)])
async def calendar_action(req: CalendarActionRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.calendar_service: raise HTTPException(status_code=503, detail="カレンダーサービス未設定")
    
    if req.action == "add":
        # デフォルトで今日の日付にするなどの処理はService側か呼び出し側で行う
        start = req.start_time or datetime.datetime.now(JST).strftime("%Y-%m-%d 10:00:00")
        end = req.end_time or (datetime.datetime.strptime(start[:19], "%Y-%m-%d %H:%M:%S") + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S") if " " in start else start
        res = await bot.calendar_service.create_event(req.summary, start, end, req.description or "")
    elif req.action == "delete":
        if not req.event_id: raise HTTPException(status_code=400, detail="event_idが必要です")
        res = await bot.calendar_service.delete_event(req.event_id)
    elif req.action == "update":
        if not req.event_id: raise HTTPException(status_code=400, detail="event_idが必要です")
        res = await bot.calendar_service.update_event(req.event_id, summary=req.summary, description=req.description)
    else: res = "不明なアクションです"
    
    return {"status": "success", "message": res}

class GTaskActionRequest(BaseModel):
    action: str # 'add', 'update', 'delete', 'toggle'
    task_id: Optional[str] = None
    title: str = None
    completed: bool = None
    list_name: str = None

@router.post("/google_tasks_action", dependencies=[Depends(verify_api_key)])
async def google_tasks_action(req: GTaskActionRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service: raise HTTPException(status_code=503, detail="タスクサービス未設定")

    if req.action == "add":
        if not req.title: raise HTTPException(status_code=400, detail="titleが必要です")
        res = await bot.tasks_service.add_task(req.title, list_name=req.list_name)
    elif req.action == "delete":
        if not req.task_id: raise HTTPException(status_code=400, detail="task_idが必要です")
        res = await bot.tasks_service.delete_task(req.task_id, list_name=req.list_name)
    elif req.action == "update":
        if not req.task_id: raise HTTPException(status_code=400, detail="task_idが必要です")
        res = await bot.tasks_service.update_task(req.task_id, title=req.title, list_name=req.list_name)
    elif req.action == "toggle":
        if not req.task_id: raise HTTPException(status_code=400, detail="task_idが必要です")
        res = await bot.tasks_service.update_task(req.task_id, completed=req.completed, list_name=req.list_name)
    else: res = "不明なアクションです"

    return {"status": "success", "message": res}

class ExecuteToolRequest(BaseModel):
    tool_name: str
    args: dict

@router.post("/execute_tool", dependencies=[Depends(verify_api_key)])
async def execute_tool(req: ExecuteToolRequest):
    """AIが提案したアクションをユーザーの承認後に実行"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if req.tool_name == "calendar_add":
        res = await bot.calendar_service.create_event(req.args["summary"], req.args["start"], req.args["end"], req.args.get("description", ""))
    elif req.tool_name == "task_add":
        res = await bot.tasks_service.add_task(req.args["title"], list_name=req.args.get("list_name"))
    elif req.tool_name == "task_delete":
        res = await bot.tasks_service.delete_task_by_keyword(req.args["keyword"], list_name=req.args.get("list_name"))
    else:
        raise HTTPException(status_code=400, detail="不明なツールです")
    return {"status": "success", "message": res}

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
    
    # 履歴（過去7日分くらいからユニークなタスクを抽出）
    start_candidates = []
    end_candidates = []
    seen = set()
    
    for i in range(7):
        d = (datetime.datetime.now(JST) - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{d}.md")
        if f_id:
            content = await chat_service.drive_service.read_text_file(service, f_id)
            if "## 🪟 Lifelog" in content:
                section = content.split("## 🪟 Lifelog")[1].split("##")[0]
                for line in section.split("\n"):
                    match = re.search(r"- \[(.*?)\] (.*)", line)
                    if match:
                        state = match.group(1)
                        task_name = match.group(2).split("(")[0].strip() # 時刻部分は除外
                        if task_name and task_name not in seen:
                            start_candidates.append(task_name)
                            seen.add(task_name)
                        if state == "/" and i == 0: # 今日の実行中
                             end_candidates.append(task_name)
    
    return {
        "start": start_candidates[:10],
        "end": end_candidates
    }


@router.post("/daily_report", dependencies=[Depends(verify_api_key)])
async def daily_report():
    """日次レポートを手動実行する（Daily Journal, Events & Actions, Insights & Thoughts, Next Actionsを生成しObsidianに保存）"""
    from api import app
    from config import JST
    from api.database import get_todays_log
    import json
    import re
    from prompts import PROMPT_DAILY_ORGANIZE
    from utils.obsidian_utils import update_section, update_frontmatter

    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    if not bot or not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    gemini_client = bot.gemini_client
    if not gemini_client:
        raise HTTPException(status_code=503, detail="AI未接続")

    now = datetime.datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")

    # 今日の会話ログ取得
    log_text = await get_todays_log()
    if not log_text.strip():
        return {"message": "今日の会話ログが空のため、日次整理をスキップしました。"}

    # 未完了タスク取得
    current_tasks_text = "タスクAPIに接続されていません。"
    if chat_service.tasks_service:
        try:
            current_tasks_text = await chat_service.tasks_service.get_uncompleted_tasks()
        except Exception:
            pass

    # 天気取得
    weather_data = await bot.info_service.get_weather()
    weather = weather_data.get("summary", "取得失敗")
    max_t = weather_data.get("max_temp", "N/A")
    min_t = weather_data.get("min_temp", "N/A")

    # ロケーション情報取得
    location_log_text = "（記録なし）"
    service = chat_service.drive_service.get_service()
    if service:
        daily_folder = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
        if daily_folder:
            daily_file = await chat_service.drive_service.find_file(service, daily_folder, f"{today_str}.md")
            if daily_file:
                try:
                    raw_content = await chat_service.drive_service.read_text_file(service, daily_file)
                    match = re.search(r"## 📍 Location History\n(.*?)(?=\n## |\Z)", raw_content, re.DOTALL)
                    if match and match.group(1).strip():
                        location_log_text = match.group(1).strip()
                except Exception:
                    pass

    # Gemini呼び出し
    from google.genai import types
    prompt = f"{PROMPT_DAILY_ORGANIZE}\n【現在の未完了タスク】\n{current_tasks_text}\n\n【今日の移動記録】\n{location_log_text}\n\n--- Chat Log ---\n{log_text}"

    result = {
        "journal": "",
        "events": [],
        "insights": [],
        "next_actions": [],
        "message": "日次整理が完了しました。",
    }

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        res_data = json.loads(response.text)
        result.update(res_data)
    except Exception as e:
        logging.error(f"Daily Report AI Error: {e}")
        return {"message": f"AI整理中にエラーが発生しました: {e}"}

    # Obsidianに保存
    result["meta"] = {
        "weather": f'"{ weather}"' if weather != "取得失敗" else "取得失敗",
        "temp_max": f'"{max_t}"' if max_t != "N/A" else "N/A",
        "temp_min": f'"{min_t}"' if min_t != "N/A" else "N/A",
    }

    daily_folder = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    if not daily_folder:
        daily_folder = await chat_service.drive_service.create_folder(service, chat_service.drive_folder_id, "DailyNotes")

    f_id = await chat_service.drive_service.find_file(service, daily_folder, f"{today_str}.md")
    content = f"# Daily Note {today_str}\n"
    if f_id:
        try:
            raw = await chat_service.drive_service.read_text_file(service, f_id)
            if raw:
                content = raw
        except Exception:
            pass

    meta = result.get("meta", {})
    updates_fm = {"date": today_str}
    if meta.get("weather") != "N/A":
        updates_fm["weather"] = meta.get("weather")
    if meta.get("temp_max") != "N/A":
        updates_fm["temp_max"] = meta.get("temp_max")
    if meta.get("temp_min") != "N/A":
        updates_fm["temp_min"] = meta.get("temp_min")
    content = update_frontmatter(content, updates_fm)

    if result.get("journal"):
        content = update_section(content, result["journal"], "## 📔 Daily Journal")
    if result.get("events") and len(result["events"]) > 0:
        content = update_section(
            content,
            "\n".join(result["events"]) if isinstance(result["events"], list) else str(result["events"]),
            "## 📝 Events & Actions",
        )
    if result.get("insights") and len(result["insights"]) > 0:
        content = update_section(
            content,
            "\n".join(result["insights"]) if isinstance(result["insights"], list) else str(result["insights"]),
            "## 💡 Insights & Thoughts",
        )
    if result.get("next_actions") and len(result["next_actions"]) > 0:
        formatted_actions = []
        for act in result["next_actions"]:
            if isinstance(act, str):
                formatted_actions.append(act if act.startswith("-") else f"- {act}")
            elif isinstance(act, dict):
                title = act.get("title", "")
                lst = act.get("list", "")
                prefix = f"[{lst}] " if lst else ""
                formatted_actions.append(f"- {prefix}{title}")
        content = update_section(content, "\n".join(formatted_actions), "## 🚀 Next Actions")

    if f_id:
        await chat_service.drive_service.update_text(service, f_id, content)
    else:
        await chat_service.drive_service.upload_text(service, daily_folder, f"{today_str}.md", content)

    # Next ActionsをGoogle Tasksに登録
    if result.get("next_actions") and chat_service.tasks_service:
        for act_data in result["next_actions"]:
            act_title = ""
            list_name = None
            if isinstance(act_data, str):
                act_title = re.sub(r"^-\s*", "", act_data).strip()
            elif isinstance(act_data, dict):
                act_title = act_data.get("title", "").strip()
                list_name = act_data.get("list")
            if act_title:
                try:
                    await chat_service.tasks_service.add_task(title=act_title, list_name=list_name)
                except Exception:
                    pass

    return {"message": result.get("message", "日次整理が完了しました。")}


class HabitCompleteRequest(BaseModel):
    habit_name: str


@router.get("/habits", dependencies=[Depends(verify_api_key)])
async def get_habits():
    """習慣リストと今日の達成状況・ストリーク情報を返す"""
    from api import app
    from config import JST

    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"habits": [], "today_done": [], "streaks": {}}

    data = await habit_cog._load_data()
    
    # --- Google Tasksからの同期 ---
    if bot.tasks_service:
        try:
            google_habits = await bot.tasks_service.get_raw_tasks("習慣")
            existing_names = [h["name"].lower() for h in data.get("habits", [])]
            changed = False
            for gh in google_habits:
                title = gh["title"]
                if title.lower() not in existing_names:
                    existing_ids = [int(h["id"]) for h in data.get("habits", [])]
                    new_id = str(max(existing_ids) + 1) if existing_ids else "1"
                    data.setdefault("habits", []).append({"id": new_id, "name": title, "frequency_days": 1})
                    existing_names.append(title.lower())
                    changed = True
            if changed:
                await habit_cog._save_data(data)
        except Exception as e:
            logging.error(f"Failed to sync google habits: {e}")
    # ------------------------------

    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    today_logs = data.get("logs", {}).get(today_str, [])

    habits_list = []
    streaks = {}
    for h in data.get("habits", []):
        habits_list.append({"id": h["id"], "name": h["name"], "frequency_days": h.get("frequency_days", 1)})
        streaks[h["id"]] = habit_cog._get_habit_stats(data, h["id"], today_str)

    return {
        "habits": habits_list,
        "today_done": today_logs,
        "streaks": streaks,
    }


@router.post("/habits/complete", dependencies=[Depends(verify_api_key)])
async def complete_habit(req: HabitCompleteRequest):
    """習慣をワンタップ完了する"""
    from api import app

    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        raise HTTPException(status_code=503, detail="習慣サービス未接続")

    result_msg = await habit_cog._process_habit_completion(req.habit_name)
    return {"status": "success", "message": result_msg}


class HabitModifyRequest(BaseModel):
    habit_id: str
    old_title: str
    title: str = None  # updateの場合に使用


@router.post("/habits/update", dependencies=[Depends(verify_api_key)])
async def update_habit(req: HabitModifyRequest):
    """習慣の名前を変更し、Google Tasksにも反映させる"""
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog: return {"status": "error", "message": "機能未接続"}

    data = await habit_cog._load_data()
    target = next((h for h in data.get("habits", []) if h["id"] == req.habit_id), None)
    if target and req.title:
        target["name"] = req.title
        await habit_cog._save_data(data)

    if bot.tasks_service:
        try:
            gtasks = await bot.tasks_service.get_raw_tasks("習慣")
            gtask = next((t for t in gtasks if t["title"] == req.old_title), None)
            if gtask and req.title:
                await bot.tasks_service.update_task(gtask["id"], title=req.title, list_name="習慣")
        except: pass
    return {"status": "success"}


@router.post("/habits/delete", dependencies=[Depends(verify_api_key)])
async def delete_habit_endpoint(req: HabitModifyRequest):
    """習慣を削除し、Google Tasksからも削除する"""
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog: return {"status": "error", "message": "機能未接続"}

    data = await habit_cog._load_data()
    data["habits"] = [h for h in data.get("habits", []) if h["id"] != req.habit_id]
    await habit_cog._save_data(data)

    if bot.tasks_service:
        try:
            gtasks = await bot.tasks_service.get_raw_tasks("習慣")
            gtask = next((t for t in gtasks if t["title"] == req.old_title), None)
            if gtask:
                await bot.tasks_service.delete_task(gtask["id"], list_name="習慣")
        except: pass
    return {"status": "success"}


@router.get("/book_notes", dependencies=[Depends(verify_api_key)])
async def get_book_notes(title: str):
    """指定書籍のBookNotesの内容を返す"""
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    import re
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:50]
    book_folder = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "BookNotes")
    if not book_folder:
        return {"title": title, "content": ""}

    f_id = await chat_service.drive_service.find_file(service, book_folder, f"{safe_title}.md")
    if not f_id:
        return {"title": title, "content": ""}

    content = await chat_service.drive_service.read_text_file(service, f_id)
    return {"title": title, "content": content}


# --- 機能1: 朝ブリーフィング / 夜レビュー ---

@router.post("/briefing", dependencies=[Depends(verify_api_key)])
async def generate_briefing():
    """今日のブリーフィングを生成してチャット履歴に保存する"""
    from api import app
    import datetime
    from config import JST
    from services.info_service import InfoService

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    now = datetime.datetime.now(JST)
    is_morning = now.hour < 14  # 14時より前なら朝ブリーフィング

    # 天気・ニュース取得
    info_service = getattr(bot, "info_service", InfoService()) if bot else InfoService()
    weather_data = await info_service.get_weather()
    weather_text = weather_data.get("summary", "取得できませんでした")
    max_t = weather_data.get("max_temp", "N/A")
    min_t = weather_data.get("min_temp", "N/A")
    news_list = await info_service.get_news(limit=3)
    news_text = "\n".join([f"- {n}" for n in news_list]) if news_list else "取得できませんでした"

    # カレンダー予定取得
    schedule_text = "（取得できませんでした）"
    if chat_service.calendar_service:
        today_str = now.strftime("%Y-%m-%d")
        schedule_text = await chat_service.calendar_service.list_events_for_date(today_str)

    # 未完了タスク取得
    tasks_text = "（取得できませんでした）"
    if chat_service.tasks_service:
        tasks_text = await chat_service.tasks_service.get_uncompleted_tasks()

    # 睡眠データ
    sleep_text = ""
    if bot:
        fitbit_cog = bot.get_cog("FitbitCog")
        if fitbit_cog and fitbit_cog.is_ready:
            try:
                stats = await fitbit_cog.fitbit_service.get_stats(now.date())
                if stats and stats.get("sleep_score"):
                    sleep_text = f"\n睡眠スコア: {stats['sleep_score']}, 睡眠時間: {fitbit_cog._format_minutes(stats.get('total_sleep_minutes', 0))}"
            except Exception:
                pass

    gemini_client = chat_service.gemini_client if chat_service else None
    if not gemini_client and bot:
        gemini_client = bot.gemini_client
    if not gemini_client:
        raise HTTPException(status_code=503, detail="AI未接続")

    if is_morning:
        prompt = f"""あなたは有能な秘書・マネージャーとして、朝のブリーフィングを行います。
以下の情報を踏まえて、今日の天候やコンディションを気遣いながら、簡潔に報告してください（5〜10行程度）。

【天気】{weather_text} (最高{max_t}℃ / 最低{min_t}℃){sleep_text}
【今日の予定】
{schedule_text}
【未完了タスク】
{tasks_text}
【ニュース】
{news_text}

【秘書としての重要任務】
報告の最後に、今日の「メイン目標」をユーザーに決めさせてください。
その際、上記の「今日の予定」の空き時間（予定が入っていない時間帯）を分析し、
「〇〇時から〇〇時が空いているから、ここで『（未完了タスク等）』を一つ終わらせるのはどう？」
と、タイムブロッキング（カレンダー枠の確保）を具体的に提案してください。
提案に同意してくれたら、後で[ACTION:calendar_add:summary=〇〇|start=2026-XX-XXT10:00:00|end=2026-XX-XXT11:00:00] の形式でボタンを出せるよう、まずはスケジュールを提案するだけに留めてください。
"""
    else:
        # 夜のレビュー
        from api.database import get_todays_log
        today_log = await get_todays_log()
        prompt = f"""夜のレビューを生成して。「お疲れさま！」から始めて、今日の活動を振り返ってまとめてね。
短すぎず長すぎず（5〜10行くらい）。最後に「明日はどうする？」って自然に聞いてみて。

【今日の会話ログ】
{today_log if today_log.strip() else '今日は特に会話がありませんでした。'}
【今日の予定（振り返り用）】
{schedule_text}
【未完了タスク】
{tasks_text}"""

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        reply = response.text.strip()
        await save_message("assistant", reply)
        return {"reply": reply, "type": "morning" if is_morning else "evening"}
    except Exception as e:
        logging.error(f"Briefing Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 機能7: タスクブレイクダウン ---

@router.post("/task_breakdown", dependencies=[Depends(verify_api_key)])
async def task_breakdown(req: ChatRequest):
    """大きなタスクをサブタスクに分解し、Google Tasksへの追加を提案する"""
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    gemini_client = (chat_service.gemini_client if chat_service else None) or (bot.gemini_client if bot else None)
    if not gemini_client:
        raise HTTPException(status_code=503, detail="AI未接続")

    prompt = f"""以下のタスクを、具体的で実行可能な3〜6個のサブタスクに分解して。
各サブタスクは「〜する」で終わる短い文にして。所要時間の目安も付けて。
出力はJSON配列で: [{{"title": "サブタスク名", "estimate": "10分"}}]
余計な説明やマークダウンの装飾は不要、JSON配列だけを返して。

タスク: {req.message}"""

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        import json
        raw = response.text.strip()
        # ```json ... ``` の囲みを除去
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        subtasks = json.loads(raw)
        return {"task": req.message, "subtasks": subtasks}
    except Exception as e:
        logging.error(f"Task Breakdown Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class TaskTriageRequest(PydanticBase):
    list_name: str = "仕事"

@router.post("/task_triage", dependencies=[Depends(verify_api_key)])
async def task_triage(req: TaskTriageRequest):
    """指定されたリストのタスクをAIが整理（トリアージ）提案を行う"""
    from api import app
    import json

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    gemini_client = (chat_service.gemini_client if chat_service else None) or (bot.gemini_client if bot else None)
    tasks_service = (chat_service.tasks_service if chat_service else None) or (bot.tasks_service if bot else None)

    if not gemini_client or not tasks_service:
        raise HTTPException(status_code=503, detail="AIまたはTasksAPIに接続できません")

    raw_tasks = await tasks_service.get_raw_tasks(req.list_name)
    if not raw_tasks:
        reply = f"「{req.list_name}」のリストを確認したけど、未完了のタスクはゼロだったよ！素晴らしいね！"
        await save_message("assistant", reply)
        return {"reply": reply}

    tasks_json = json.dumps(raw_tasks, ensure_ascii=False)
    
    prompt = f"""あなたは有能な秘書・マネージャーです。
以下のJSONは、ゆうすけの「{req.list_name}」リストの未完了タスクです。

【指令】
このタスク一覧の中から、以下の条件に合いそうな「整理（削除、分解、日時変更）」が必要そうなタスクを最大3つ見つけ出し、
それぞれについて「どうする？」と提案してください。

・名前が抽象的すぎて何をすればいいか分かりにくいタスク（「〜について」など）
・長期間残っていそうなタスク、または重すぎるタスク（推測でOK）

【出力フォーマット】
タスクのIDやJSONは出さず、チャットで話しかけるように人間らしく返信してください。
例：「リストをチェックしたよ！いくつか整理したほうがよさそうなタスクを見つけたから確認させて！
1. 『〇〇』：これ、粒度が大きすぎない？ 細かく分解（Breakdown）する？
2. 『〇〇』：ずっと残ってそうだけど、今週やる？ それともひとまず消しちゃう？
どれか一つでも対応するなら教えてね！」

【タスク一覧】
{tasks_json}
"""
    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        reply = response.text.strip()
        await save_message("assistant", reply)
        return {"reply": reply}
    except Exception as e:
        logging.error(f"Task Triage Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# リクエストボディ用のモデルを定義
from pydantic import BaseModel as PydanticBase
from typing import List

class SubtaskItem(PydanticBase):
    title: str
    estimate: str = ""

class ApplyBreakdownRequest(PydanticBase):
    list_name: str = "プライベート"
    subtasks: List[SubtaskItem]

@router.post("/task_breakdown/apply", dependencies=[Depends(verify_api_key)])
async def apply_task_breakdown(req: ApplyBreakdownRequest):
    """サブタスクをGoogle Tasksに一括追加"""
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.tasks_service:
        raise HTTPException(status_code=503, detail="Google Tasks未接続")

    added = 0
    for st in req.subtasks:
        try:
            await chat_service.tasks_service.add_task(st.title, req.list_name)
            added += 1
        except Exception as e:
            logging.error(f"Add subtask error: {e}")

    reply = f"{added}個のサブタスクを「{req.list_name}」リストに追加しました。"
    await save_message("assistant", reply)
    return {"added": added, "message": reply}


# --- 機能14: 健康と気分の相関分析 ---

@router.post("/health_correlation", dependencies=[Depends(verify_api_key)])
async def health_correlation():
    """過去1週間のDailyNoteとFitbitデータからAIが相関を分析する"""
    from api import app
    import datetime
    from config import JST

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    gemini_client = (chat_service.gemini_client if chat_service else None) or (bot.gemini_client if bot else None)
    if not gemini_client:
        raise HTTPException(status_code=503, detail="AI未接続")

    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    now = datetime.datetime.now(JST)
    drive_folder = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    daily_folder = await chat_service.drive_service.find_file(service, drive_folder, "DailyNotes")
    if not daily_folder:
        raise HTTPException(status_code=404, detail="DailyNotesフォルダが見つかりません")

    gathered = []
    for i in range(7):
        target = now - datetime.timedelta(days=i)
        fname = f"{target.strftime('%Y-%m-%d')}.md"
        fid = await chat_service.drive_service.find_file(service, daily_folder, fname)
        if fid:
            try:
                content = await chat_service.drive_service.read_text_file(service, fid)
                # 主要セクションのみ抽出してトークン削減
                lines = content.split("\n")
                key_lines = []
                capture = False
                for line in lines:
                    if line.startswith("## "):
                        capture = any(k in line for k in ["Alter Log", "Insights", "Events", "Health", "Sleep", "Journal"])
                    if capture:
                        key_lines.append(line)
                if key_lines:
                    gathered.append(f"=== {target.strftime('%Y-%m-%d')} ===\n" + "\n".join(key_lines))
            except Exception:
                pass

    if not gathered:
        return {"analysis": "分析するためのデータが不足しています。数日間ログを記録してから再度お試しください。"}

    combined = "\n\n".join(reversed(gathered))
    prompt = f"""あなたはライフコーチ兼データアナリストだ。以下の1週間分のライフログを分析して、気分・行動と健康（睡眠・運動）の相関関係を見つけて。
「ゆうすけ」に語りかけるように、タメ口で親しみやすく、でも深い洞察で。

出力形式:
### 📊 今週の傾向
（睡眠と気分の傾向、活動量と生産性の関係など）

### 💡 見つけたパターン
（具体的な日付を引用して「この日は〇〇だったから△△だったみたい」など）

### 🎯 来週への提案
（1〜2個、具体的で実行可能なアドバイス）

【データ】
{combined}"""

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        analysis = response.text.strip()
        await save_message("assistant", f"【週間ヘルスレポート】\n{analysis}")
        return {"analysis": analysis}
    except Exception as e:
        logging.error(f"Health Correlation Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 機能9: 積読 (Stocked Links) 関連のエンドポイント ---


@router.get("/links", dependencies=[Depends(verify_api_key)])
async def get_links():
    """ストックリンク一覧を取得（未読・保存済み両方）"""
    from api.database import get_all_links
    links = await get_all_links()
    return {"links": links}

class LinkUpdateRequest(BaseModel):
    purpose: str = ""
    summary: str = ""
    memo: str = ""
    target_date: str = ""
    linked_note_url: str = ""
    type: str = ""
    add_to_calendar: bool = False

@router.put("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def update_link(link_id: int, req: LinkUpdateRequest):
    """リンクの詳細情報を更新する"""
    from api import app
    from api.database import get_link_by_id, update_link_details

    link = await get_link_by_id(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="リンクが見つかりません")

    await update_link_details(
        link_id, req.purpose, req.summary, req.memo, req.target_date, req.linked_note_url, req.type or link["type"]
    )

    # カレンダーに追加フラグが立っており、日付が設定されている場合
    if req.add_to_calendar and req.target_date:
        bot = getattr(app.state, "bot", None)
        if bot and bot.calendar_service:
            # title にバッジをつける
            prefix = "🗺️[行]" if req.type == "map" else "🍳[食]" if req.type == "recipe" else "📎[記]"
            cal_title = f"{prefix} {link['title']}"
            desc = f"■目的: {req.purpose}\n■メモ: {req.memo}\n■リンク: {link['url']}"
            try:
                # 終日予定として登録。start=XXXX-XX-XX, end=XXXX-XX-XX (翌日にはしない簡易版か、Google API的には翌日を指定する)
                # target_dateは yyyy-mm-dd を想定
                import datetime
                date_obj = datetime.datetime.strptime(req.target_date, "%Y-%m-%d")
                start_date = date_obj.strftime("%Y-%m-%d")
                end_date = (date_obj + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                
                service = bot.calendar_service.get_service()
                event_body = {
                    "summary": cal_title,
                    "description": desc,
                    "colorId": "10" if req.type == "map" else "11", # 10=Green(Basil), 11=Red(Tomato)
                    "start": {"date": start_date},
                    "end": {"date": end_date}
                }
                service.events().insert(calendarId="primary", body=event_body).execute()
            except Exception as e:
                logging.error(f"Calendar Sync Error for link {link_id}: {e}")

    return {"status": "success"}


@router.post("/links/{link_id}/summarize", dependencies=[Depends(verify_api_key)])
async def summarize_link(link_id: int):
    """ストックされたリンクをGeminiで要約し、Obsidianに保存する"""
    from api import app
    from api.database import get_link_by_id, mark_link_as_saved
    import aiohttp

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    target_link = await get_link_by_id(link_id)
    if not target_link:
        raise HTTPException(status_code=404, detail="リンクが見つかりません。")

    url = target_link["url"]
    link_type = target_link["type"]
    link_title = target_link["title"]

    gemini_client = (chat_service.gemini_client if chat_service else None) or (bot.gemini_client if bot else None)
    if not gemini_client:
        raise HTTPException(status_code=503, detail="AI未接続")

    try:
        # コンテンツ取得（WebClipServiceを使わず直接取得）
        page_text = ""
        if link_type == "youtube":
            # YouTubeはURLとタイトル情報だけでGeminiに要約させる
            page_text = f"YouTube動画: {link_title}\nURL: {url}"
        else:
            # Webページ/レシピはHTMLから本文を抽出
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True) as resp:
                        if resp.status == 200:
                            html = await resp.text(errors="replace")
                            import re
                            # scriptタグとstyleタグを除去
                            html_clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
                            # HTMLタグを除去してテキスト化
                            text = re.sub(r"<[^>]+>", " ", html_clean)
                            text = re.sub(r"\s+", " ", text).strip()
                            page_text = text[:8000]
            except Exception as e:
                logging.error(f"Page fetch error: {e}")
                page_text = f"タイトル: {link_title}\nURL: {url}\n（ページ本文の取得に失敗しました）"

        # 種類に応じたプロンプト
        if link_type == "youtube":
            prompt = f"""以下のYouTube動画をObsidianに保存するためのノートを生成して。
動画タイトルから内容を推測し、どんな動画かの紹介文（2〜3行）を書いて。

動画タイトル: {link_title}
URL: {url}

出力フォーマット:
## 概要
（紹介文）

## リンク
{url}"""
        elif link_type == "recipe":
            prompt = f"""以下はレシピページの内容です。Obsidianに保存するためのレシピノートを作成して。

タイトル: {link_title}
URL: {url}

ページ本文:
{page_text[:5000]}

出力フォーマット:
## 材料
（材料リスト）

## 作り方
（手順）

## メモ
（ポイントや補足）

## リンク
{url}"""
        else:
            prompt = f"""以下のWebページの内容を要約して、Obsidianに保存するノートを作成して。
最も重要なポイントを箇条書きで分かりやすくまとめて。

タイトル: {link_title}
URL: {url}

ページ本文:
{page_text[:5000]}

出力フォーマット:
## 要約
（箇条書き）

## リンク
{url}"""

        gemini_res = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        summary_text = gemini_res.text.strip() if gemini_res and gemini_res.text else ""

        if not summary_text:
            raise HTTPException(status_code=500, detail="要約の生成に失敗しました")

        # Obsidianに保存
        import datetime, re
        from config import JST
        from utils.obsidian_utils import update_section

        now = datetime.datetime.now(JST)
        service = chat_service.drive_service.get_service()
        if not service:
            raise HTTPException(status_code=503, detail="Drive未接続")

        folder_map = {"youtube": "YouTube", "recipe": "Recipes", "web": "WebClips"}
        folder_name = folder_map.get(link_type, "WebClips")
        safe_title = re.sub(r'[\\/*?:"<>|]', "", link_title)[:80] or "Untitled"
        timestamp = now.strftime("%Y%m%d%H%M%S")
        filename = f"{timestamp}-{safe_title}.md"
        daily_note_date = now.strftime("%Y-%m-%d")

        # ファイル内容
        note_content = f"# {link_title}\n\n{summary_text}\n\n---\nSaved: {now.strftime('%Y-%m-%d %H:%M')}\n[[{daily_note_date}]]"

        # フォルダの作成・取得
        drive_folder = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        folder_id = await chat_service.drive_service.find_file(service, drive_folder, folder_name)
        if not folder_id:
            folder_id = await chat_service.drive_service.create_folder(service, drive_folder, folder_name)

        await chat_service.drive_service.upload_text(service, folder_id, filename, note_content)

        # DailyNoteにリンクを追記
        section_map = {"youtube": "## 📺 YouTube", "recipe": "## 🍳 Recipes", "web": "## 🔗 WebClips"}
        section_header = section_map.get(link_type, "## 🔗 WebClips")
        link_str = f"- [[{folder_name}/{timestamp}-{safe_title}|{link_title}]]"

        daily_folder_id = await chat_service.drive_service.find_file(service, drive_folder, "DailyNotes")
        if daily_folder_id:
            daily_fname = f"{daily_note_date}.md"
            daily_fid = await chat_service.drive_service.find_file(service, daily_folder_id, daily_fname)
            if daily_fid:
                current = await chat_service.drive_service.read_text_file(service, daily_fid)
                updated = update_section(current, link_str, section_header)
                await chat_service.drive_service.update_text(service, daily_fid, updated)

        await mark_link_as_saved(link_id)
        return {"status": "success", "message": f"「{link_title}」を要約して保存しました。"}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Link Summarize Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class ManualSummaryRequest(BaseModel):
    summary: str

@router.post("/links/{link_id}/summarize_manual", dependencies=[Depends(verify_api_key)])
async def summarize_link_manual(link_id: int, req: ManualSummaryRequest):
    """ストックされたリンクの要約を手動で指定してObsidianに保存する"""
    from api import app
    from api.database import get_link_by_id, mark_link_as_saved
    import datetime, re
    from config import JST
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    target_link = await get_link_by_id(link_id)
    if not target_link:
        raise HTTPException(status_code=404, detail="リンクが見つかりません。")

    url = target_link["url"]
    link_type = target_link["type"]
    link_title = target_link["title"]
    summary_text = req.summary

    try:
        now = datetime.datetime.now(JST)
        service = chat_service.drive_service.get_service()
        if not service:
            raise HTTPException(status_code=503, detail="Drive未接続")

        folder_map = {"youtube": "YouTube", "recipe": "Recipes", "web": "WebClips"}
        folder_name = folder_map.get(link_type, "WebClips")
        safe_title = re.sub(r'[\\/*?:"<>|]', "", link_title)[:80] or "Untitled"
        timestamp = now.strftime("%Y%m%d%H%M%S")
        filename = f"{timestamp}-{safe_title}.md"
        daily_note_date = now.strftime("%Y-%m-%d")

        # ファイル内容 (ユーザーが貼り付けた要約を利用)
        note_content = f"# {link_title}\n\n{summary_text}\n\n---\n## リンク\n{url}\n\n---\nSaved: {now.strftime('%Y-%m-%d %H:%M')}\n[[{daily_note_date}]]"

        # フォルダの作成・取得
        drive_folder = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        folder_id = await chat_service.drive_service.find_file(service, drive_folder, folder_name)
        if not folder_id:
            folder_id = await chat_service.drive_service.create_folder(service, drive_folder, folder_name)

        await chat_service.drive_service.upload_text(service, folder_id, filename, note_content)

        # DailyNoteにリンクを追記
        section_map = {"youtube": "## 📺 YouTube", "recipe": "## 🍳 Recipes", "web": "## 🔗 WebClips"}
        section_header = section_map.get(link_type, "## 🔗 WebClips")
        link_str = f"- [[{folder_name}/{timestamp}-{safe_title}|{link_title}]]"

        daily_folder_id = await chat_service.drive_service.find_file(service, drive_folder, "DailyNotes")
        if daily_folder_id:
            daily_fname = f"{daily_note_date}.md"
            daily_fid = await chat_service.drive_service.find_file(service, daily_folder_id, daily_fname)
            if daily_fid:
                current = await chat_service.drive_service.read_text_file(service, daily_fid)
                updated = update_section(current, link_str, section_header)
                await chat_service.drive_service.update_text(service, daily_fid, updated)

        await mark_link_as_saved(link_id)
        return {"status": "success", "message": f"「{link_title}」の手動要約を保存しました。"}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Link Manual Summarize Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def delete_link(link_id: int):
    """ストックリンクを削除する"""
    from api.database import delete_stocked_link
    await delete_stocked_link(link_id)
    return {"status": "success", "message": "リンクを削除しました。"}
