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


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(req: ChatRequest):
    """メッセージを送信してAIの応答を取得"""
    from api import app
    import re
    from api.database import add_stocked_link

    # URLが含まれているかチェックし、ストックに回す
    # 単純にURLらしき文字列があれば抽出
    url_match = re.search(r"https?://[^\s]+", req.message)
    if url_match:
        url = url_match.group(0)
        # 簡単な分類
        link_type = "web"
        if "youtube.com" in url or "youtu.be" in url:
            link_type = "youtube"
        elif "google.com/maps" in url or "goo.gl/maps" in url or "maps.app.goo.gl" in url:
            link_type = "map"
        elif any(d in url for d in ["cookpad.com", "kurashiru.com", "delishkitchen.tv", "macaro-ni.jp"]):
            link_type = "recipe"

        await add_stocked_link(url, link_type, "Untitled")
        reply = "リンクをストックしました！ダッシュボードの「積読」セクションから詳細を確認・要約できます。"
        await save_message("user", req.message)
        await save_message("assistant", reply)
        return ChatResponse(reply=reply)


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

# --- 機能9: 積読 (Stocked Links) 関連のエンドポイント ---

@router.get("/links", dependencies=[Depends(verify_api_key)])
async def get_links():
    """未読のストックリンク一覧を取得"""
    from api.database import get_unread_links
    links = await get_unread_links()
    return {"links": links}

@router.post("/links/{link_id}/summarize", dependencies=[Depends(verify_api_key)])
async def summarize_link(link_id: int):
    """ストックされたリンクをWebClipServiceで解析・要約・保存する"""
    from api import app
    from api.database import get_unread_links, mark_link_as_saved
    
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")

    webclip_cog = bot.get_cog("WebClipCog")
    if not webclip_cog:
        raise HTTPException(status_code=503, detail="WebClipCogがロードされていません。")

    # DBから該当リンクを探す
    links = await get_unread_links()
    target_link = next((lk for lk in links if lk["id"] == link_id), None)
    if not target_link:
        raise HTTPException(status_code=404, detail="リンクが見つかりません。")

    url = target_link["url"]
    
    # WebClipServiceに投げてObsidianに保存
    # (内部でタイトル取得、要約、DailyNote追記まで行う)
    try:
        parsed_info = await webclip_cog.webclip_service.parse_url_info(url, "")
        
        # Geminiを使って要約を生成
        if webclip_cog.webclip_service.gemini_client and parsed_info.get("raw_text") and len(parsed_info["raw_text"]) > 50:
            from google.genai import types
            prompt = f"以下のWebページの内容（またはメタデータ）を読み込み、最も重要なポイントを箇条書きで分かりやすく要約してください。\n\n【タイトル】{parsed_info['title']}\n【本文・詳細】\n{parsed_info['raw_text'][:5000]}"
            
            gemini_res = await webclip_cog.webclip_service.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            if gemini_res and gemini_res.text:
                parsed_info["raw_text"] = f"## ✨ AI Summary\n{gemini_res.text.strip()}\n\n---\n\n<details><summary>原文プレビュー</summary>\n\n{parsed_info['raw_text'][:1000]}...\n</details>"
        elif target_link["type"] == "youtube" or target_link["type"] == "map":
            # 動画やマップの場合はraw_textが少ないので基本情報をGeminiで整形
            from google.genai import types
            prompt = f"以下のURLリンク情報を元に、Obsidianに保存するための簡単な紹介文（1〜2行）を作成してください。\nタイトル: {parsed_info['title']}\nURL: {url}"
            gemini_res = await webclip_cog.webclip_service.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            if gemini_res and gemini_res.text:
                parsed_info["raw_text"] = f"## ✨ AI Info\n{gemini_res.text.strip()}"

        res = await webclip_cog.webclip_service.save_parsed_info(parsed_info, user_comment="Webストックからの要約保存")
        if res:
            await mark_link_as_saved(link_id)
            return {"status": "success", "message": f"「{res['title']}」を要約して保存しました。"}
        else:
            raise HTTPException(status_code=500, detail="保存に失敗しました。")
    except Exception as e:
        logging.error(f"Link Summarize Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

