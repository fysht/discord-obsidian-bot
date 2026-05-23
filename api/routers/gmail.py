"""Gmail インボックス（要約一覧 + 既読/ゴミ箱/Obsidian保存/手動ポーリング）。"""

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/gmail", tags=["gmail"])


@router.get("/inbox", dependencies=[Depends(verify_api_key)])
async def gmail_inbox(state: str = "pending", limit: int = 50):
    """`state` は pending / archived / trashed / all。"""
    from api.database import gmail_list, gmail_count_unnotified_high
    state = state.strip().lower() if state else "pending"
    if state not in ("pending", "archived", "trashed", "all"):
        state = "pending"
    rows = await gmail_list(state=state, limit=max(1, min(int(limit or 50), 200)))
    return {
        "state": state,
        "items": rows,
        "high_pending_count": await gmail_count_unnotified_high(),
    }


@router.post("/{message_id}/read", dependencies=[Depends(verify_api_key)])
async def gmail_mark_read(message_id: str):
    """Gmail 側で既読化し、DB の state を 'archived' に。"""
    from api import app
    from api.database import gmail_update
    bot = getattr(app.state, "bot", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail:
        raise HTTPException(status_code=503, detail="Gmail未接続")
    ok = await gmail.mark_as_read(message_id)
    if ok:
        await gmail_update(message_id, state="archived")
    return {"ok": ok}


@router.post("/{message_id}/trash", dependencies=[Depends(verify_api_key)])
async def gmail_trash(message_id: str):
    """Gmail でゴミ箱に移動し、DB の state を 'trashed' に。"""
    from api import app
    from api.database import gmail_update
    bot = getattr(app.state, "bot", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail:
        raise HTTPException(status_code=503, detail="Gmail未接続")
    ok = await gmail.trash(message_id)
    if ok:
        await gmail_update(message_id, state="trashed")
    return {"ok": ok}


@router.post("/{message_id}/save", dependencies=[Depends(verify_api_key)])
async def gmail_save_to_obsidian(message_id: str):
    """重要メールを Google Drive (Obsidian) の `Emails/{YYYY-MM}/` に Markdown として保存。"""
    from api import app
    from api.database import gmail_get, gmail_update
    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail or not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Gmail / Drive未接続")

    record = await gmail_get(message_id)
    if not record:
        raise HTTPException(status_code=404, detail="DBに該当メールがありません")

    if record.get("saved_drive_id"):
        return {"ok": True, "drive_id": record["saved_drive_id"], "already_saved": True}

    full = await gmail.get_message(message_id)
    body_excerpt = ((full or {}).get("body") or "")[:5000]

    received_at = record.get("received_at") or datetime.datetime.now(JST).isoformat()
    try:
        date_part = received_at[:10] if len(received_at) >= 10 else datetime.datetime.now(JST).strftime("%Y-%m-%d")
        month_part = date_part[:7]
    except Exception:
        date_part = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        month_part = date_part[:7]

    subject = (record.get("subject") or "(件名なし)")
    safe_subject = "".join(c if c.isalnum() or c in " 　-_.()[]" else "_" for c in subject)[:80].strip().replace(" ", "_") or "email"
    file_name = f"{date_part}_{safe_subject}.md"

    importance = record.get("importance") or "medium"
    summary = record.get("summary") or ""
    from_addr = record.get("from_addr") or ""

    md_body = (
        "---\n"
        f"title: {subject}\n"
        f"from: {from_addr}\n"
        f"date: {received_at}\n"
        f"importance: {importance}\n"
        f"gmail_id: {message_id}\n"
        f"gmail_thread_id: {record.get('thread_id', '')}\n"
        "tags: [email]\n"
        "---\n\n"
        f"# {subject}\n\n"
        f"- **差出人**: {from_addr}\n"
        f"- **受信日時**: {received_at}\n"
        f"- **重要度**: {importance}\n"
        f"- **Gmail で開く**: https://mail.google.com/mail/u/0/#all/{record.get('thread_id', '') or message_id}\n\n"
        "## マネージャー要約\n"
        f"{summary or '（要約なし）'}\n\n"
        "## 本文（抜粋）\n"
        "```\n"
        f"{body_excerpt}\n"
        "```\n"
    )

    try:
        service = chat_service.drive_service.get_service()
        if not service:
            return {"ok": False, "error": "Drive未接続"}
        root = chat_service.drive_folder_id
        emails_folder = await chat_service.drive_service.find_file(service, root, "Emails")
        if not emails_folder:
            emails_folder = await chat_service.drive_service.create_folder(service, root, "Emails")
        month_folder = await chat_service.drive_service.find_file(service, emails_folder, month_part)
        if not month_folder:
            month_folder = await chat_service.drive_service.create_folder(service, emails_folder, month_part)

        drive_id = await chat_service.drive_service.upload_text(
            service, month_folder, file_name, md_body
        )
        if not drive_id:
            return {"ok": False, "error": "Drive 書き込みに失敗"}
        await gmail_update(
            message_id,
            saved_drive_id=drive_id,
            saved_at=datetime.datetime.now(JST).isoformat(),
        )
        return {"ok": True, "drive_id": drive_id, "file_name": file_name}
    except Exception as e:
        logging.error(f"gmail_save_to_obsidian error: {e}")
        return {"ok": False, "error": "保存に失敗しました"}


@router.post("/refresh", dependencies=[Depends(verify_api_key)])
async def gmail_refresh():
    """ユーザー操作による手動ポーリング起動。新着の取り込みを即時実行。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Bot未起動")
    cog = bot.get_cog("GmailWatchCog")
    if not cog:
        return {"ok": False, "error": "GmailWatchCog 未ロード"}
    try:
        await cog._run()
        return {"ok": True}
    except Exception as e:
        logging.error(f"gmail_refresh error: {e}")
        return {"ok": False, "error": "ポーリングに失敗しました"}
