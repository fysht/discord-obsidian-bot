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
