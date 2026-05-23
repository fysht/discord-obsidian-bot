"""EDINET（金融庁書類検索/ダウンロード）関連エンドポイント。"""

import asyncio
import datetime
import logging
import os
import re
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/edinet", tags=["edinet"])


class EdinetFindRequest(BaseModel):
    ticker: str
    days: int = 400
    only_earnings: bool = True


class EdinetDownloadRequest(BaseModel):
    doc_id: str
    sec_code: Optional[str] = None
    submit_date: Optional[str] = None
    doc_type_label: Optional[str] = None


@router.post("/find", dependencies=[Depends(verify_api_key)])
async def edinet_find(req: EdinetFindRequest):
    """指定証券コードの過去 days 日分の EDINET 提出書類を検索する。"""
    from services import edinet_service
    if not edinet_service.get_api_key():
        return {"ok": False, "error": "サーバ側に EDINET_API_KEY が未設定です。.env に追加して bot を再起動してください。"}
    try:
        result = await edinet_service.find_documents_for_security_code(
            req.ticker, days=req.days, only_earnings=req.only_earnings
        )
    except Exception as e:
        logging.exception(f"edinet_find error: {e}")
        return {"ok": False, "error": f"EDINET 検索に失敗: {e}"}
    return result


@router.post("/download", dependencies=[Depends(verify_api_key)])
async def edinet_download(req: EdinetDownloadRequest):
    """EDINET から書類 PDF を取得し、Drive 上 Investment/EarningsDocs/EDINET/ に保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return {"ok": False, "error": "Drive サービス未初期化"}

    from services import edinet_service
    if not edinet_service.get_api_key():
        return {"ok": False, "error": "サーバ側に EDINET_API_KEY が未設定です"}

    data = await edinet_service.download_document(req.doc_id, doc_type=2)
    if not data:
        return {"ok": False, "error": "EDINET から PDF を取得できませんでした（PDF未提供か API キー無効）"}

    drive = bot.drive_service
    service = drive.get_service()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not service or not folder_id:
        return {"ok": False, "error": "Drive 認証/フォルダ未設定"}

    inv_folder = await drive.find_file(service, folder_id, "Investment")
    if not inv_folder:
        inv_folder = await drive.create_folder(service, folder_id, "Investment")
    docs_folder = await drive.find_file(service, inv_folder, "EarningsDocs")
    if not docs_folder:
        docs_folder = await drive.create_folder(service, inv_folder, "EarningsDocs")
    edinet_folder = await drive.find_file(service, docs_folder, "EDINET")
    if not edinet_folder:
        edinet_folder = await drive.create_folder(service, docs_folder, "EDINET")

    sec = (req.sec_code or "")[:4] or "unknown"
    submit_day = (req.submit_date or datetime.datetime.now(JST).strftime("%Y-%m-%d"))[:10]
    label = req.doc_type_label or ""
    safe_label = re.sub(r"[\\/:*?\"<>|]", "_", label)[:24]
    filename = f"EDINET_{sec}_{submit_day}_{safe_label}_{req.doc_id}.pdf".replace("__", "_")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        existing = await drive.find_file(service, edinet_folder, filename)
        if existing:
            try:
                await asyncio.to_thread(lambda: service.files().delete(fileId=existing).execute())
            except Exception as e:
                logging.warning(f"EDINET 旧ファイル削除失敗: {e}")
        file_id = await drive.upload_file(
            service, edinet_folder, filename, tmp_path, mime_type="application/pdf"
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return {
        "ok": True,
        "file_id": file_id,
        "filename": filename,
        "drive_path": f"Investment/EarningsDocs/EDINET/{filename}",
        "bytes": len(data),
    }
