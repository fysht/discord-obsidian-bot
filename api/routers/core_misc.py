"""core_misc: 小さい補助エンドポイント群。
- /history（会話履歴取得）
- /error_log（バックグラウンドタスク失敗ログ）
- /permanent_notes/confirm（永久ノート確定保存）
- /reset_history（履歴全削除）
- /daily_report（プレースホルダ）
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.database import (
    clear_history, get_history, get_recent_errors, backup_db_to_drive,
)
from api.routes import verify_api_key
from utils.async_utils import safe_create_task

router = APIRouter(prefix="", tags=["core"])


class PermanentNoteConfirmRequest(BaseModel):
    title: str
    content: str


@router.get("/history", dependencies=[Depends(verify_api_key)])
async def history(limit: int = 100):
    return {"messages": await get_history(limit=limit)}


@router.get("/error_log", dependencies=[Depends(verify_api_key)])
async def error_log(limit: int = 100):
    """直近のバックグラウンドタスク失敗ログ。デバッグ用。"""
    return {"errors": await get_recent_errors(limit=limit)}


@router.post("/permanent_notes/confirm", dependencies=[Depends(verify_api_key)])
async def permanent_notes_confirm(req: PermanentNoteConfirmRequest):
    """ユーザーが永久ノートの確認モーダルで『保存』を押した時に呼ばれる。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service:
        raise HTTPException(status_code=503, detail="ChatService 未初期化")
    title = (req.title or "").strip()
    content = (req.content or "").strip()
    if not title:
        return {"ok": False, "error": "タイトルが空です"}
    try:
        msg = await chat_service._create_permanent_note(title, content)
        return {"ok": True, "message": msg}
    except Exception as e:
        logging.error(f"permanent_notes confirm error: {e}")
        return {"ok": False, "error": "保存処理でエラー"}


@router.post("/reset_history", dependencies=[Depends(verify_api_key)])
async def reset_history():
    """会話履歴を全削除して Drive にバックアップを起動する。"""
    from api import app
    await clear_history()
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-reset",
        )
    return {"status": "success"}


@router.post("/daily_report", dependencies=[Depends(verify_api_key)])
async def daily_report():
    """互換用のプレースホルダ。"""
    return {"message": "日次整理が完了しました。"}
