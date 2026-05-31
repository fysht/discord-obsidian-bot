"""ノート関連エンドポイント。
- 手書きメモ画像（単数/複数）→ Gemini Vision で読み取り
- Drive 上のノート一覧 / 永久ノート検索 / 保存（新規・追記）
"""

import asyncio
import base64
import datetime
import json
import logging
import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["notes"])


class NoteFromImageRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    hint: str = ""


class ImagePayload(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"


class NoteFromImagesRequest(BaseModel):
    images: List[ImagePayload]
    hint: str = ""


class SaveNoteRequest(BaseModel):
    mode: str  # "new" or "append"
    content: str
    action_items: List[str] = []
    title: str = ""
    category: str = "other"
    subject: str = ""
    target_id: str = ""
    target_folder: str = ""
    target_filename: str = ""


@router.post("/note_from_image", dependencies=[Depends(verify_api_key)])
async def note_from_image(req: NoteFromImageRequest):
    from google.genai import types
    from api import app

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足情報: {req.hint}" if req.hint else ""
    prompt = f"""この手書きメモの画像を読み取り、以下のJSON形式で返してください。
文字が読みにくい場合は文脈から補完してください。{hint_text}

{{
  "transcription": "文字起こし（原文に近い形）",
  "structured_content": "整理・構造化した内容（Markdown形式。箇条書きや見出しを使い読みやすく。必要なら補足も加える）",
  "category": "work か study か idea か task か other のいずれか",
  "subject": "categoryがstudyの場合の科目名（例: 数学、英語）。それ以外は空文字",
  "action_items": ["タスク・TODOがあれば文字列の配列で。なければ空配列"]
}}"""

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = types.Part.from_text(text=prompt)
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("memo_image", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=types.Content(role="user", parts=[image_part, text_part]),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"note_from_image error: {e}")
        raise HTTPException(status_code=500, detail=f"読み取りに失敗しました: {str(e)}")


@router.post("/note_from_images", dependencies=[Depends(verify_api_key)])
async def note_from_images(req: NoteFromImagesRequest):
    """複数画像を1ノートに統合読み取り。"""
    from google.genai import types
    from api import app

    if not req.images:
        raise HTTPException(status_code=400, detail="画像が指定されていません。")

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足情報: {req.hint}" if req.hint else ""
    prompt = f"""これら {len(req.images)} 枚の手書きメモ画像をまとめて読み取り、1つのノートとして以下のJSON形式で返してください。
画像の順番がメモの流れを表します。読みにくい箇所は文脈から補完してください。{hint_text}

{{
  "transcription": "全画像を統合した文字起こし（原文に近い形）",
  "structured_content": "整理・構造化した内容（Markdown形式。箇条書きや見出しを使い、複数画像の内容を統合する）",
  "category": "work か study か idea か task か other のいずれか",
  "subject": "categoryがstudyの場合の科目名（例: 数学、英語）。それ以外は空文字",
  "action_items": ["タスク・TODOがあれば文字列の配列で。なければ空配列"]
}}"""

    try:
        parts = []
        for img in req.images:
            image_bytes = base64.b64decode(img.image_base64)
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=img.mime_type))
        parts.append(types.Part.from_text(text=prompt))
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("memo_image", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=types.Content(role="user", parts=parts),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"note_from_images error: {e}")
        raise HTTPException(status_code=500, detail=f"読み取りに失敗しました: {str(e)}")


@router.get("/notes/list", dependencies=[Depends(verify_api_key)])
async def get_notes_list():
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"notes": []}

    service = chat_service.drive_service.get_service()
    if not service:
        return {"notes": []}

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    notes = []
    notes.append({
        "id": "TODAY_DAILY",
        "name": f"今日のデイリーノート ({today_str})",
        "folder": "DailyNotes",
        "filename": f"{today_str}.md",
    })

    try:
        study_folder = await chat_service.drive_service.find_file(service, drive_root, "StudyLogs")
        if study_folder:
            results = await asyncio.to_thread(
                lambda: service.files().list(
                    q=f"'{study_folder}' in parents and trashed = false",
                    fields="files(id, name)",
                    orderBy="modifiedTime desc",
                ).execute()
            )
            for f in results.get("files", []):
                display = f["name"].replace("_ノート.md", "").replace(".md", "")
                notes.append({
                    "id": f["id"],
                    "name": display,
                    "folder": "StudyLogs",
                    "filename": f["name"],
                })
    except Exception as e:
        logging.error(f"notes/list StudyLogs error: {e}")

    return {"notes": notes}


@router.get("/notes/search", dependencies=[Depends(verify_api_key)])
async def search_notes(q: str = "", limit: int = 8):
    """永久ノート（Notes フォルダ）からタイトル類似のファイルを検索する。"""
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"candidates": []}
    service = chat_service.drive_service.get_service()
    if not service:
        return {"candidates": []}
    q = (q or "").strip()
    if len(q) < 1:
        return {"candidates": []}

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    candidates = []
    try:
        notes_folder = await chat_service.drive_service.find_file(service, drive_root, "Notes")
        if not notes_folder:
            return {"candidates": []}
        results = await asyncio.to_thread(
            lambda: service.files().list(
                q=f"'{notes_folder}' in parents and trashed = false",
                fields="files(id, name, modifiedTime)",
                orderBy="modifiedTime desc",
                pageSize=200,
            ).execute()
        )
        files = results.get("files", [])

        def display_name(fname: str) -> str:
            base = fname[:-3] if fname.endswith(".md") else fname
            import re as _re
            return _re.sub(r"^\d{8,14}-", "", base)

        q_lower = q.lower()
        scored = []
        for f in files:
            disp = display_name(f["name"])
            disp_lower = disp.lower()
            if q_lower in disp_lower:
                score = 100 - disp_lower.index(q_lower) - (0 if disp_lower.startswith(q_lower) else 10)
            else:
                overlap = sum(1 for ch in set(q_lower) if ch in disp_lower)
                if overlap < max(1, len(set(q_lower)) // 2):
                    continue
                score = overlap
            scored.append((score, {
                "id": f["id"],
                "name": disp,
                "folder": "Notes",
                "filename": f["name"],
                "modified": f.get("modifiedTime", ""),
            }))
        scored.sort(key=lambda x: (-x[0], x[1]["name"]))
        candidates = [s[1] for s in scored[:max(1, min(limit, 20))]]
    except Exception as e:
        logging.error(f"notes/search error: {e}")
    return {"candidates": candidates}


@router.post("/save_note", dependencies=[Depends(verify_api_key)])
async def save_note(req: SaveNoteRequest):
    from api import app
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    now = datetime.datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.strftime("%Y-%m-%d")

    try:
        if req.mode == "append":
            folder_name = req.target_folder
            filename = req.target_filename

            folder_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
            if not folder_id:
                folder_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

            file_id = await chat_service.drive_service.find_file(service, folder_id, filename)

            if file_id:
                existing = await chat_service.drive_service.read_text_file(service, file_id)
            else:
                existing = f"# {filename.replace('.md','')}\n"

            if folder_name == "DailyNotes":
                # DailyNote は構造化された日次ノートなので、従来通り
                # `## 🔎 Insights` セクションに整列して追記する
                new_content = update_section(
                    existing, f"*{now_str} 追記*\n{req.content}", "## 🔎 Insights"
                )
            else:
                # StudyLogs/BookNotes 等の永続ノートはセクション見出しを使わず、
                # 「日時 → 空行 → 本文 → 空行」のシンプルなブロックを末尾に追記する。
                # （見返したときの体裁を整えるため、タイトルや "Learning Log" 等の
                #   セクション見出しは付けない）
                base = (existing or "").rstrip()
                block = f"\n\n---\n\n**{now_str}**\n\n{req.content}\n"
                new_content = base + block

            if file_id:
                await chat_service.drive_service.update_text(service, file_id, new_content)
            else:
                await chat_service.drive_service.upload_text(service, folder_id, filename, new_content)

        else:
            # 新規作成
            title = req.title or f"メモ_{now_str}"

            if req.category == "study":
                folder_name = "StudyLogs"
                subject = req.subject or title
                filename = f"{subject}_ノート.md"
                initial = (
                    f"---\ntitle: {subject} 学習ノート\ndate: {today_str}\ntags: [study]\n---\n\n"
                    f"# {subject} 学習ノート\n\n## 📝 Learning Log\n"
                )
                section = "## 📝 Learning Log"
            elif req.category == "reading":
                import re as _re
                folder_name = "BookNotes"
                safe_title = _re.sub(r'[\\/*?:"<>|]', "", title)[:80] or "Untitled"
                filename = f"{safe_title}.md"
                initial = (
                    f"---\ntitle: {safe_title}\ndate: {today_str}\ntags: [book]\n---\n\n"
                    f"# {safe_title}\n\n## 📖 Reading Log\n"
                )
                section = "## 📖 Reading Log"
            else:
                folder_name = "Notes"
                filename = f"{now.strftime('%Y%m%d%H%M%S')}-{title[:40]}.md"
                initial = f"---\ntitle: {title}\ndate: {today_str}\n---\n\n# {title}\n"
                section = None

            folder_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
            if not folder_id:
                folder_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

            existing_id = await chat_service.drive_service.find_file(service, folder_id, filename)

            if existing_id:
                existing = await chat_service.drive_service.read_text_file(service, existing_id)
                if section:
                    new_content = update_section(existing, f"*{now_str}*\n{req.content}", section)
                else:
                    new_content = existing + f"\n\n*{now_str}*\n{req.content}\n"
                await chat_service.drive_service.update_text(service, existing_id, new_content)
            else:
                if section:
                    new_content = update_section(initial, f"*{now_str}*\n{req.content}", section)
                else:
                    new_content = initial + f"\n{req.content}\n\nSaved: {now_str}\n"
                await chat_service.drive_service.upload_text(service, folder_id, filename, new_content)

        # action_items を Google Tasks に追加
        if req.action_items and bot and getattr(bot, "tasks_service", None):
            list_name = "仕事" if req.category == "work" else "プライベート"
            for item in req.action_items:
                if item.strip():
                    try:
                        await bot.tasks_service.add_task(item.strip(), list_name=list_name)
                    except Exception as e:
                        logging.error(f"save_note task add error: {e}")

        # 読書ノートはデイリーノートの Reading Log にもリンクを追記
        if req.mode == "new" and req.category == "reading":
            try:
                import re as _re
                safe_title = _re.sub(r'[\\/*?:"<>|]', "", req.title or "Untitled")[:80] or "Untitled"
                daily_link = f"- [[BookNotes/{safe_title}|{safe_title}]]"
                daily_fid = await chat_service.drive_service.find_file(service, drive_root, "DailyNotes")
                if daily_fid:
                    df_id = await chat_service.drive_service.find_file(service, daily_fid, f"{today_str}.md")
                    if df_id:
                        cur = await chat_service.drive_service.read_text_file(service, df_id)
                        if daily_link not in cur:
                            await chat_service.drive_service.update_text(
                                service, df_id, update_section(cur, daily_link, "## 📖 Reading Log")
                            )
                    else:
                        initial_dn = f"---\ndate: {today_str}\n---\n\n# Daily Note {today_str}\n"
                        await chat_service.drive_service.upload_text(
                            service, daily_fid, f"{today_str}.md",
                            update_section(initial_dn, daily_link, "## 📖 Reading Log"),
                        )
            except Exception as e:
                logging.error(f"save_note reading daily link error: {e}")

    except Exception as e:
        logging.error(f"save_note error: {e}")
        raise HTTPException(status_code=500, detail=f"保存に失敗しました: {str(e)}")

    return {"status": "success"}
