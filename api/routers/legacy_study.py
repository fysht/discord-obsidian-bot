"""旧 StudyCog ベースの勉強エンドポイント（subjects 一覧 / メモ保存）。
（新しい目標/教材/セッション機能は api/routers/study.py に分離済み）"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="/study", tags=["study-legacy"])


class StudyMemoRequest(BaseModel):
    subject: str
    memo: str


@router.get("/subjects", dependencies=[Depends(verify_api_key)])
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
                subject = name.replace("_ノート.md", "").replace(".md", "")
                subjects.append(subject)
    except Exception as e:
        logging.debug(f"study subjects fetch: {e}")
    return {"subjects": subjects}


@router.post("/save", dependencies=[Depends(verify_api_key)])
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
