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

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service:
        raise HTTPException(status_code=503, detail="AIサービスが初期化されていません。")

    # ユーザーメッセージを保存
    await save_message("user", req.message)

    # AI応答を生成
    reply = await chat_service.generate_response(req.message)

    # AIの応答を保存
    await save_message("assistant", reply)

    # Google Driveへのバックアップを非同期で実行 (ユーザーを待たせない)
    import asyncio
    from api.database import backup_db_to_drive
    if chat_service.drive_service:
        asyncio.create_task(backup_db_to_drive(chat_service.drive_service, chat_service.drive_folder_id))

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
    if not chat_service or not chat_service.drive_service:
        return {"tasks": [], "alter_log": "", "error": "サービス未接続"}

    service = chat_service.drive_service.get_service()
    if not service:
        return {"tasks": [], "alter_log": ""}

    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    if not folder_id:
        return {"tasks": [], "alter_log": ""}

    f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
    if not f_id:
        return {"tasks": [], "alter_log": ""}

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

    # Alter Logセクションの抽出
    alter_log = ""
    alter_match = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if alter_match:
        alter_log = alter_match.group(1).strip()

    return {"tasks": tasks, "alter_log": alter_log, "date": today_str}


class TaskActionRequest(BaseModel):
    action: str  # 'create', 'update', 'delete', 'toggle'
    old_text: str = ""
    new_text: str = ""

@router.post("/task_action", dependencies=[Depends(verify_api_key)])
async def task_action(req: TaskActionRequest):
    """ダッシュボードUIやボタンからタスクを操作するエンドポイント"""
    from api import app
    import datetime
    from config import JST
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    file_name = f"{today_str}.md"
    f_id = await chat_service.drive_service.find_file(service, folder_id, file_name)

    content = f"# Daily Note {today_str}\n"
    if f_id:
        try:
            content = await chat_service.drive_service.read_text_file(service, f_id)
        except Exception:
            pass

    # 既存のタスクセクション内容を正規表現で書き換え
    if req.action == "create":
        append_text = f"- [/] {req.new_text}"
        content = update_section(content, append_text, "## 🎯 Tasks")
    else:
        # 古いテキストを含む行を探す
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.startswith("- [") and req.old_text in line:
                if req.action == "delete":
                    lines.pop(i)
                elif req.action == "update":
                    prefix = line[:6] # '- [x] ' or '- [/] '
                    lines[i] = f"{prefix}{req.new_text}"
                elif req.action == "toggle":
                    if line.startswith("- [x]"):
                        lines[i] = line.replace("- [x]", "- [/]", 1)
                    else:
                        lines[i] = line.replace("- [/]", "- [x]", 1)
                break
        content = '\n'.join(lines)

    if f_id:
        await chat_service.drive_service.update_text(service, f_id, content)
    else:
        await chat_service.drive_service.upload_text(service, folder_id, file_name, content)

    return {"status": "success"}
