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


@router.post("/{message_id}/expense", dependencies=[Depends(verify_api_key)])
async def gmail_to_expense(message_id: str):
    """メール本文（注文確認メール等）から AI で支出情報を抽出して返す（保存はしない）。
    フロントはこの結果を支出入力モーダルにプレフィルする。"""
    from api import app
    from api.database import gmail_get
    from api.routers.expenses import analyze_expense_text

    bot = getattr(app.state, "bot", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail:
        raise HTTPException(status_code=503, detail="Gmail未接続")

    record = await gmail_get(message_id)
    subject = (record.get("subject") or "") if record else ""
    sender = (record.get("from_addr") or record.get("sender") or "") if record else ""

    try:
        full = await gmail.get_message(message_id)
    except Exception as e:
        logging.error(f"gmail_to_expense get_message error: {e}")
        full = None
    body = ((full or {}).get("body") or "")[:5000]

    text = f"件名: {subject}\n差出人: {sender}\n本文:\n{body}".strip()
    if not body and not subject:
        raise HTTPException(status_code=404, detail="メール本文を取得できませんでした")

    ex = await analyze_expense_text(text)
    # 店名が空ければ件名から補完
    if not (ex.get("vendor") or "").strip() and subject:
        ex["vendor"] = subject[:40]
    return {"ok": True, "expense": ex}


async def analyze_calendar_text(text: str) -> dict:
    """メール本文等から予定（イベント）情報を抽出して dict を返す。保存はしない。
    start / end は datetime-local 互換の 'YYYY-MM-DDTHH:MM' 形式。抽出できなければ空。"""
    import json
    from google.genai import types as _gt
    from api import app

    now = datetime.datetime.now(JST)
    fallback = {"has_event": False, "summary": "", "date": "", "start": "",
                "end": "", "location": "", "confidence": "low"}
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        return fallback

    prompt = (
        "次のメール内容から『カレンダーに登録すべき予定』を1件抽出し、必ず以下の JSON 形式だけを返してください。"
        "前置きや説明は禁止。\n\n"
        f"現在日時（JST）: {now.strftime('%Y-%m-%d %H:%M')}（相対表現はこれを基準に絶対日時へ変換）\n\n"
        f"メール内容:\n{text}\n\n"
        "{\n"
        '  "has_event": 予定が読み取れたら true、なければ false,\n'
        '  "summary": "予定のタイトル（簡潔に。例: 〇〇の打ち合わせ）",\n'
        '  "date": "YYYY-MM-DD（不明なら空文字）",\n'
        '  "start": "YYYY-MM-DDTHH:MM（開始日時。時刻不明なら日付＋T09:00など妥当な既定。不明なら空）",\n'
        '  "end": "YYYY-MM-DDTHH:MM（終了日時。不明なら開始の1時間後。不明なら空）",\n'
        '  "location": "場所（空可）",\n'
        '  "confidence": "high / medium / low"\n'
        "}\n"
        "日時がはっきりしないときは confidence='low' とすること。"
    )
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("receipt_ocr", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[_gt.Part.from_text(text=prompt)]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text)
        return data if isinstance(data, dict) else fallback
    except Exception as e:
        logging.error(f"analyze_calendar_text error: {e}")
        return fallback


@router.post("/{message_id}/calendar", dependencies=[Depends(verify_api_key)])
async def gmail_to_calendar(message_id: str):
    """メール本文から AI で予定情報を抽出して返す（保存はしない）。
    フロントはこの結果をカレンダー追加モーダルにプレフィルする。"""
    from api import app
    from api.database import gmail_get

    bot = getattr(app.state, "bot", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail:
        raise HTTPException(status_code=503, detail="Gmail未接続")

    record = await gmail_get(message_id)
    subject = (record.get("subject") or "") if record else ""
    sender = (record.get("from_addr") or record.get("sender") or "") if record else ""

    try:
        full = await gmail.get_message(message_id)
    except Exception as e:
        logging.error(f"gmail_to_calendar get_message error: {e}")
        full = None
    body = ((full or {}).get("body") or "")[:5000]

    text = f"件名: {subject}\n差出人: {sender}\n本文:\n{body}".strip()
    if not body and not subject:
        raise HTTPException(status_code=404, detail="メール本文を取得できませんでした")

    ev = await analyze_calendar_text(text)
    # タイトルが空なら件名で補完
    if not (ev.get("summary") or "").strip() and subject:
        ev["summary"] = subject[:60]
    return {"ok": True, "event": ev}


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

        # 受信日のデイリーノートに、保存したメールノートへのリンクを残す
        try:
            from api.routers._obsidian_helpers import append_lifelog_line
            filename_no_ext = file_name[:-3] if file_name.endswith(".md") else file_name
            # wikilink のエイリアスに [ ] | が入ると壊れるので除去
            label = subject.replace("[", "").replace("]", "").replace("|", " ").strip() or "メール"
            link = f"- [[Emails/{month_part}/{filename_no_ext}|✉️ {label}]]"
            await append_lifelog_line(date_part, link, heading="## ✉️ Emails", dedup=True)
        except Exception as e:
            logging.debug(f"gmail_save daily-note link failed: {e}")

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
