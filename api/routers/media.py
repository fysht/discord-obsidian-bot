"""撮影画像の保管庫（写真／書類を分けて整理）。

実体は Google Drive（`Photos/YYYY-MM/` と `Documents/YYYY-MM/`）に保存し、
DB (media_items) を索引として持つ。写真と書類は kind で区別し、保存先フォルダも分ける。
分類は Gemini Vision で自動判定し、ユーザーが後から手動修正できる（タイトルも軽く付与）。
"""

import base64
import datetime
import logging
import os
import tempfile

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from config import JST
from api.routes import verify_api_key

router = APIRouter(prefix="/media", tags=["media"])


def _drive_view_url(drive_id: str) -> str:
    return f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else ""


def _norm_kind(kind: str) -> str:
    return kind if kind in ("photo", "document") else "photo"


class MediaAnalyzeRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"


class MediaSaveRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    kind: str = "photo"            # 'photo' | 'document'
    title: str = ""
    date: str = ""                 # YYYY-MM-DD（空なら当日）


class MediaUpdateRequest(BaseModel):
    kind: str | None = None
    title: str | None = None


@router.post("/analyze", dependencies=[Depends(verify_api_key)])
async def media_analyze(req: MediaAnalyzeRequest):
    """画像が「写真」か「書類」かを Gemini Vision で判定し、短いタイトルを付ける（保存はしない）。"""
    from google.genai import types as _gt
    from api import app
    import json

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        # AI 未接続でも保存自体はできるよう、フォールバックを返す
        return {"ok": True, "kind": "photo", "title": "", "fallback": True}

    prompt = (
        "この画像を分類してください。必ず以下の JSON だけを返す（前置き・説明禁止）。\n"
        "{\n"
        '  "kind": "photo または document",\n'
        '  "title": "内容が一目で分かる短いタイトル（日本語・最大30文字）"\n'
        "}\n"
        "判定基準:\n"
        "- document: 書類・帳票・レシート・契約書・手紙・名刺・スクリーンショット・"
        "ホワイトボード・スライド・本やノートのページなど『文字情報が主役』のもの。\n"
        "- photo: 風景・人物・料理・物・イベントなど『記録写真』。\n"
        "title は document なら書類名や用途（例:『電気料金の明細』）、photo なら被写体（例:『海辺の夕焼け』）。"
    )
    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = _gt.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = _gt.Part.from_text(text=prompt)
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("receipt_ocr", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[image_part, text_part]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text)
        kind = _norm_kind((data.get("kind") or "").strip())
        title = (data.get("title") or "").strip()[:40]
        return {"ok": True, "kind": kind, "title": title}
    except Exception as e:
        logging.error(f"media_analyze error: {e}")
        # 失敗しても保存はできるようフォールバック
        return {"ok": True, "kind": "photo", "title": "", "fallback": True}


@router.post("", dependencies=[Depends(verify_api_key)])
async def media_save(req: MediaSaveRequest):
    """画像を Google Drive の `Photos/` または `Documents/` 配下（月別）へ保存し、DB に索引を登録する。"""
    from api import app
    from api.database import add_media_item
    from api.routers._obsidian_helpers import append_lifelog_line

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"ok": False, "error": "Drive未接続"}

    kind = _norm_kind(req.kind)
    now = datetime.datetime.now(JST)
    date_str = req.date or now.strftime("%Y-%m-%d")
    month_str = date_str[:7] if len(date_str) >= 7 else now.strftime("%Y-%m")
    root_folder_name = "Documents" if kind == "document" else "Photos"

    try:
        service = chat_service.drive_service.get_service()
        if not service:
            return {"ok": False, "error": "Drive未接続"}
        root = chat_service.drive_folder_id
        base_folder = await chat_service.drive_service.find_file(service, root, root_folder_name)
        if not base_folder:
            base_folder = await chat_service.drive_service.create_folder(service, root, root_folder_name)
        month_folder = await chat_service.drive_service.find_file(service, base_folder, month_str)
        if not month_folder:
            month_folder = await chat_service.drive_service.create_folder(service, base_folder, month_str)

        mime = req.mime_type or "image/jpeg"
        suffix = ".jpg" if ("jpeg" in mime or "jpg" in mime) else (".png" if "png" in mime else ".img")
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        prefix = "doc" if kind == "document" else "photo"
        filename = f"{prefix}_{timestamp}{suffix}"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            tf.write(base64.b64decode(req.image_base64))
            tmp_path = tf.name
        try:
            drive_id = await chat_service.drive_service.upload_file(
                service, month_folder, filename, tmp_path, mime_type=mime
            )
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        if not drive_id:
            return {"ok": False, "error": "アップロード失敗"}

        title = (req.title or "").strip()[:80]
        item_id = await add_media_item(kind, drive_id, filename, title, date_str)

        # Obsidian の対象日 DailyNote の `## 📷 Media` に時刻順で1行追記（あとで見返せるよう）
        try:
            icon = "📄" if kind == "document" else "📷"
            label = title or ("書類" if kind == "document" else "写真")
            url = _drive_view_url(drive_id)
            line = f"- {now.strftime('%H:%M')} {icon} [{label}]({url})"
            await append_lifelog_line(date_str, line, heading="## 📷 Media", sort_by_time=True)
        except Exception as e:
            logging.debug(f"media_save obsidian append failed: {e}")

        return {
            "ok": True,
            "item": {
                "id": item_id, "kind": kind, "drive_id": drive_id,
                "filename": filename, "title": title, "date": date_str,
                "view_url": _drive_view_url(drive_id),
            },
        }
    except Exception as e:
        logging.error(f"media_save error: {e}")
        return {"ok": False, "error": "保存失敗"}


@router.get("", dependencies=[Depends(verify_api_key)])
async def media_list(kind: str = "", limit: int = 200):
    """保存済み画像の一覧を返す。kind=photo/document で絞り込み可。"""
    from api.database import list_media_items
    k = kind if kind in ("photo", "document") else None
    items = await list_media_items(kind=k, limit=max(1, min(int(limit), 500)))
    for it in items:
        it["view_url"] = _drive_view_url(it.get("drive_id", ""))
    photo_n = sum(1 for it in items if it.get("kind") == "photo")
    doc_n = sum(1 for it in items if it.get("kind") == "document")
    return {"ok": True, "items": items, "counts": {"photo": photo_n, "document": doc_n}}


@router.get("/{item_id}/image")
async def media_image(item_id: int, k: str = "", x_api_key: str = Header(None)):
    """保存画像の実体を Drive から取得して返す（サムネイル/プレビュー表示用プロキシ）。
    <img src> はヘッダを送れないため、API キーはクエリ ?k= でも受け付ける。"""
    from fastapi import Response
    from api import app
    from api.routes import API_KEY
    from api.database import get_media_item

    if (x_api_key or k) != API_KEY:
        raise HTTPException(status_code=401, detail="認証エラー")

    item = await get_media_item(item_id)
    if not item or not item.get("drive_id"):
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Drive未接続")
    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")
    data = await chat_service.drive_service.download_bytes(service, item["drive_id"])
    if data is None:
        raise HTTPException(status_code=502, detail="画像の取得に失敗しました")
    fn = (item.get("filename") or "").lower()
    media_type = "image/png" if fn.endswith(".png") else "image/jpeg"
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "private, max-age=86400"})


@router.patch("/{item_id}", dependencies=[Depends(verify_api_key)])
async def media_update(item_id: int, req: MediaUpdateRequest):
    """種別（写真／書類）やタイトルを手動修正する。"""
    from api.database import update_media_item
    ok = await update_media_item(item_id, kind=req.kind, title=req.title)
    if not ok:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    return {"ok": True}


@router.delete("/{item_id}", dependencies=[Depends(verify_api_key)])
async def media_delete(item_id: int):
    """索引を削除し、Drive 上のファイルもゴミ箱へ移動する。"""
    from api import app
    from api.database import get_media_item, delete_media_item

    item = await get_media_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="画像が見つかりません")

    # Drive 実体をゴミ箱へ（失敗しても索引削除は続行）
    drive_id = item.get("drive_id")
    if drive_id:
        try:
            chat_service = getattr(app.state, "chat_service", None)
            if chat_service and chat_service.drive_service:
                service = chat_service.drive_service.get_service()
                if service:
                    await chat_service.drive_service.delete_file(service, drive_id)
        except Exception as e:
            logging.debug(f"media_delete drive trash failed: {e}")

    await delete_media_item(item_id)
    return {"ok": True}
