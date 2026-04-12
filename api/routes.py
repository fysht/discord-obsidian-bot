import os
import logging
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from api.database import save_message, get_history

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

    service = chat_service.drive_service.get_service()
    if not service:
        return {"tasks": [], "alter_log": ""}

    now = datetime.datetime.now(JST)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    # 表示用には曜日を含める
    display_date = f"{now.strftime('%Y-%m-%d')} ({weekdays[now.weekday()]})"
    # ファイル検索用には曜日を含めない
    today_str = now.strftime("%Y-%m-%d")

    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    if not folder_id:
        return {"tasks": [], "alter_log": "", "date": display_date}

    f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
    if not f_id:
        return {"tasks": [], "alter_log": "", "date": display_date}

    try:
        content = await chat_service.drive_service.read_text_file(service, f_id)
    except Exception:
        return {"tasks": [], "alter_log": ""}

    # Tasksセクションの抽出
    tasks = []
    task_match = re.search(r"## 🎯 Tasks\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if task_match:
        for line in task_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- [x]"):
                tasks.append({"text": line[6:].strip(), "done": True})
            elif line.startswith("- [/]"):
                tasks.append({"text": line[6:].strip(), "done": False})

    # ライフログ（旧Alter Log相当）セクションの抽出
    alter_log = ""
    # 新しい形式 (## 🪟 ライフログ) を優先的に探し、なければ旧形式 (## 🪞 Alter Log) を探す
    alter_match = re.search(r"## 🪟 ライフログ\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not alter_match:
        alter_match = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    
    if alter_match:
        alter_log = alter_match.group(1).strip()

    # Google Calendar
    g_calendar = []
    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        g_calendar = await chat_service.calendar_service.get_raw_events_for_date(today_str)

    # Google Tasks (旧名: g_tasks -> google_tasks)
    google_tasks = []
    if hasattr(chat_service, "tasks_service") and chat_service.tasks_service:
        google_tasks = await chat_service.tasks_service.get_raw_tasks()

    # Weather & News (InfoServiceを使用)
    from services.info_service import InfoService
    weather = "取得失敗"
    news = []
    try:
        weather_val, _, _ = await bot.info_service.get_weather() if hasattr(bot, "info_service") else await InfoService().get_weather()
        weather = weather_val.strip('"')
        news = await (bot.info_service.get_news(limit=5) if hasattr(bot, "info_service") else InfoService().get_news(limit=5))
    except Exception:
        pass

    # Fitbit (Sleep & Health)
    sleep_stats = {}
    fitbit_cog = bot.get_cog("FitbitCog")
    if fitbit_cog and getattr(fitbit_cog, "is_ready", False):
        try:
            stats = await fitbit_cog.fitbit_service.get_stats(datetime.datetime.now(JST).date())
            if stats:
                sleep_stats = {
                    "score": stats.get("sleep_score", "N/A"),
                    "duration": fitbit_cog._format_minutes(stats.get("total_sleep_minutes", 0)),
                    "steps": stats.get("steps", "N/A"),
                    "calories": stats.get("calories_out", "N/A")
                }
        except Exception:
            pass

    return {
        "tasks": tasks, 
        "alter_log": alter_log, 
        "date": display_date,
        "g_calendar": g_calendar,
        "google_tasks": google_tasks,
        "weather": weather,
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
        content = update_section(content, append_text, "## 🎯 Tasks")
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
    action: str # 'update', 'delete', 'toggle'
    task_id: str
    title: str = None
    completed: bool = None

@router.post("/google_tasks_action", dependencies=[Depends(verify_api_key)])
async def google_tasks_action(req: GTaskActionRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service: raise HTTPException(status_code=503, detail="タスクサービス未設定")

    if req.action == "delete":
        res = await bot.tasks_service.delete_task(req.task_id)
    elif req.action == "update":
        res = await bot.tasks_service.update_task(req.task_id, title=req.title)
    elif req.action == "toggle":
        res = await bot.tasks_service.update_task(req.task_id, completed=req.completed)
    else: res = "不明なアクションです"

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
            if "## 🎯 Tasks" in content:
                section = content.split("## 🎯 Tasks")[1].split("##")[0]
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

