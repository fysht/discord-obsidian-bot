"""タスク関連 AI 機能（Lifelog タスク操作 / 分解 / 整理 / 候補）。

Google Tasks の純粋な CRUD は google_tasks.py が担当。
こちらは PWA からの ActionLog 編集 + AI 機能群。
"""

import datetime
import json
import logging
import re as _re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["tasks-ai"])


class TaskActionRequest(BaseModel):
    action: str
    old_text: str = ""
    new_text: str = ""
    line_index: int = -1  # ライフログ行インデックス（編集/削除用）


class TaskBreakdownRequest(BaseModel):
    message: str


class TaskBreakdownApplyRequest(BaseModel):
    list_name: str = "プライベート"
    subtasks: List[dict]
    parent_title: Optional[str] = ""


class TaskTriageRequest(BaseModel):
    list_name: str = "仕事"


@router.post("/task_action", dependencies=[Depends(verify_api_key)])
async def task_action(req: TaskActionRequest):
    from api import app
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

    if req.action == "create":
        content = update_section(content, f"- [/] {req.new_text}", "## 🪟 Lifelog")
    elif req.action in ("edit_log", "delete_log"):
        lifelog_match = _re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        if lifelog_match:
            section_start_pos = content.index("## 🪟 Lifelog\n") + len("## 🪟 Lifelog\n")
            section_text = lifelog_match.group(1)
            section_lines = section_text.split("\n")
            log_lines = [(idx, line) for idx, line in enumerate(section_lines) if line.strip().startswith("- ")]
            if 0 <= req.line_index < len(log_lines):
                target_idx, _old_line = log_lines[req.line_index]
                if req.action == "delete_log":
                    section_lines.pop(target_idx)
                elif req.action == "edit_log" and req.new_text:
                    section_lines[target_idx] = f"- {req.new_text}"
                new_section = "\n".join(section_lines)
                content = content[:section_start_pos] + new_section + content[section_start_pos + len(section_text):]
    else:
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith("- ") and req.old_text and req.old_text in line:
                if req.action == "delete":
                    lines.pop(i)
                elif req.action == "update":
                    prefix = line[:6]
                    lines[i] = f"{prefix}{req.new_text}" if "[" in prefix and "]" in prefix else line.replace(req.old_text, req.new_text, 1)
                elif req.action == "toggle":
                    lines[i] = line.replace("- [x]", "- [/]", 1) if "- [x]" in line else line.replace("- [/]", "- [x]", 1)
                break
        content = '\n'.join(lines)

    if f_id:
        await chat_service.drive_service.update_text(service, f_id, content)
    else:
        await chat_service.drive_service.upload_text(service, folder_id, file_name, content)
    return {"status": "success"}


@router.get("/task_candidates", dependencies=[Depends(verify_api_key)])
async def task_candidates():
    """タスク開始用は Google Tasks「タスク候補」リストから取得。
    終了用は実行中ライフログタスク + タスク候補リスト（開始忘れ対応）。"""
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service:
        return {"start": [], "end": []}

    start_candidates = []
    tasks_service = getattr(chat_service, "tasks_service", None)
    if tasks_service:
        try:
            raw_tasks = await tasks_service.get_raw_tasks("タスク候補")
            start_candidates = [t["title"] for t in raw_tasks if t.get("title")]
        except Exception as e:
            logging.debug(f"タスク候補リスト取得失敗: {e}")

    running = []
    if chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
            if folder_id:
                today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
                if f_id:
                    content = await chat_service.drive_service.read_text_file(service, f_id)
                    lifelog_match = _re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
                    if lifelog_match:
                        for line in lifelog_match.group(1).split("\n"):
                            line = line.strip()
                            if "▶" in line:
                                m = _re.search(r"▶\s*(.+)$", line)
                                if m:
                                    running.append(m.group(1).strip())
        except Exception as e:
            logging.debug(f"終了候補（実行中）取得失敗: {e}")

    end_candidates = list(dict.fromkeys(running + start_candidates))
    return {"start": start_candidates, "end": end_candidates, "running": running}


@router.get("/tasks_for_breakdown", dependencies=[Depends(verify_api_key)])
async def tasks_for_breakdown():
    """既存タスクから分解候補を返す（仕事＋プライベート、未完了のみ）"""
    from api import app
    bot = getattr(app.state, "bot", None)
    tasks_service = getattr(bot, "tasks_service", None) if bot else None
    if not tasks_service:
        return {"tasks": []}

    result = []
    for list_name in ["仕事", "プライベート"]:
        try:
            raw = await tasks_service.get_raw_tasks(list_name)
            for t in raw:
                result.append({"id": t["id"], "title": t["title"], "list_name": list_name})
        except Exception as e:
            logging.debug(f"tasks_for_breakdown list {list_name}: {e}")
    return {"tasks": result}


@router.post("/task_breakdown", dependencies=[Depends(verify_api_key)])
async def task_breakdown(req: TaskBreakdownRequest):
    """親タスクを AI でサブタスクに分解する。"""
    from api import app
    from prompts import PROMPT_TASK_BREAKDOWN
    from google.genai import types as gtypes

    parent = (req.message or "").strip()
    if not parent:
        raise HTTPException(status_code=400, detail="タスク内容を指定してください")

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = PROMPT_TASK_BREAKDOWN.replace("{parent_task}", parent)
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("task_breakdown", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m, contents=prompt,
            config=gtypes.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text or "{}")
        subtasks = data.get("subtasks", [])
        if not isinstance(subtasks, list):
            subtasks = []
        return {"subtasks": subtasks, "parent": parent}
    except Exception as e:
        logging.error(f"task_breakdown error: {e}")
        raise HTTPException(status_code=500, detail=f"タスク分解に失敗しました: {str(e)}")


@router.post("/task_breakdown/apply", dependencies=[Depends(verify_api_key)])
async def task_breakdown_apply(req: TaskBreakdownApplyRequest):
    """分解結果を Google Tasks に追加する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service:
        raise HTTPException(status_code=503, detail="タスクサービス未設定")

    list_name = req.list_name or "プライベート"
    added = 0
    for st in req.subtasks:
        title = (st.get("title") or "").strip()
        if not title:
            continue
        estimate = st.get("estimate")
        notes = f"⏱ {estimate}" if estimate else ""
        if req.parent_title:
            notes = f"親: {req.parent_title}\n{notes}".strip()
        try:
            await bot.tasks_service.add_task(title, list_name=list_name, notes=notes)
            added += 1
        except TypeError:
            await bot.tasks_service.add_task(title, list_name=list_name)
            added += 1
        except Exception as e:
            logging.error(f"task add error: {e}")
    return {"status": "success", "added": added, "message": f"{added}件をタスクに追加したよ！"}


@router.post("/task_triage", dependencies=[Depends(verify_api_key)])
async def task_triage(req: TaskTriageRequest):
    """指定リストのタスクを AI に整理提案させる。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not getattr(chat_service, "tasks_service", None):
        raise HTTPException(status_code=503, detail="Tasks サービス未接続")

    tasks = await chat_service.tasks_service.get_raw_tasks(req.list_name)
    if not tasks:
        return {"reply": f"「{req.list_name}」リストに未完了タスクがないよ。"}

    task_list_str = "\n".join(
        f"- {t['title']}" + (f" (締切: {t['due'][:10]})" if t.get('due') else "")
        for t in tasks
    )

    gemini_client = getattr(bot, "gemini_client", None) if bot else None
    if not gemini_client:
        return {"reply": f"「{req.list_name}」のタスク一覧:\n{task_list_str}\n\n（AI分析は現在利用できません）"}

    prompt = (
        f"あなたはタスク管理のプロフェッショナルです。\n"
        f"以下の「{req.list_name}」リストのタスクを分析し、整理提案をしてください。\n\n"
        f"タスク一覧:\n{task_list_str}\n\n"
        f"以下の観点で提案してください:\n"
        f"1. 優先度（高/中/低）の分類\n"
        f"2. 完了・削除を推奨するタスク\n"
        f"3. グループ化・統合できるタスク\n"
        f"4. 今日取り組むべきタスクのおすすめ\n\n"
        f"簡潔かつ実用的に日本語で回答してください。"
    )

    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("task_organize", default_pro=False)
        response = await gemini_client.aio.models.generate_content(model=_m, contents=prompt)
        reply = response.text.strip() if response.text else "分析に失敗しました。"
    except Exception as e:
        logging.error(f"Task triage AI error: {e}")
        reply = f"AI分析でエラーが発生しました。\n\nタスク一覧:\n{task_list_str}"

    return {"reply": reply}
