"""メッセージ（会話履歴）関連エンドポイント。
削除 / お気に入りトグル / お気に入り一覧 / 全文検索 / ラベル付け / コレクション。
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.database import (
    delete_message_by_id, toggle_message_star, get_starred_messages,
    search_messages, set_message_label, get_all_labels, get_labeled_messages,
    backup_db_to_drive,
)
from api.routes import verify_api_key
from utils.async_utils import safe_create_task

router = APIRouter(prefix="/messages", tags=["messages"])


class LabelRequest(BaseModel):
    label: str


def _schedule_db_backup(name: str):
    from api import app
    bot = getattr(app.state, "bot", None)
    if bot and getattr(bot, "drive_service", None):
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name=name,
        )


@router.delete("/{message_id}", dependencies=[Depends(verify_api_key)])
async def delete_message(message_id: int):
    """会話履歴から1件削除し、Drive バックアップを起動。"""
    ok = await delete_message_by_id(message_id)
    if not ok:
        raise HTTPException(status_code=404, detail="該当メッセージが見つかりません。")
    _schedule_db_backup("db-backup-msg-delete")
    return {"status": "success"}


@router.post("/{message_id}/star", dependencies=[Depends(verify_api_key)])
async def star_message(message_id: int):
    """お気に入りトグル。新しい状態を返す。"""
    new_state = await toggle_message_star(message_id)
    if new_state is None:
        raise HTTPException(status_code=404, detail="該当メッセージが見つかりません。")
    _schedule_db_backup("db-backup-msg-star")
    return {"status": "success", "starred": new_state}


@router.get("/starred", dependencies=[Depends(verify_api_key)])
async def list_starred_messages(limit: int = 100):
    return {"messages": await get_starred_messages(limit=limit)}


@router.get("/search", dependencies=[Depends(verify_api_key)])
async def search_messages_endpoint(q: str = "", limit: int = 50):
    if not q.strip():
        return {"results": []}
    rows = await search_messages(q.strip(), limit=limit)
    return {"results": rows}


@router.post("/{message_id}/label", dependencies=[Depends(verify_api_key)])
async def set_label(message_id: int, req: LabelRequest):
    ok = await set_message_label(message_id, req.label.strip())
    if not ok:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません")
    return {"ok": True, "label": req.label.strip()}


@router.get("/collections", dependencies=[Depends(verify_api_key)])
async def list_collections():
    labels = await get_all_labels()
    return {"collections": labels}


@router.get("/labeled", dependencies=[Depends(verify_api_key)])
async def labeled_messages(label: str = ""):
    if not label:
        raise HTTPException(status_code=400, detail="labelを指定してください")
    msgs = await get_labeled_messages(label)
    return {"messages": msgs, "label": label}
