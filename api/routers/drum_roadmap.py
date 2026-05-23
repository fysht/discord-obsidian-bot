"""ドラム上達ロードマップ（静的JSON + マイルストーン別YouTubeリンク）。"""

import datetime
import json
import logging
import os
from pathlib import Path as _Path
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/drum_roadmap", tags=["drum"])

DRUM_ROADMAP_FILE_PATH = _Path(__file__).parent.parent.parent / "data" / "drum_roadmap.json"
DRUM_ROADMAP_LINKS_FILE = "drum_roadmap_links.json"


def _extract_youtube_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    from urllib.parse import urlparse, parse_qs
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return parsed.path.lstrip("/").split("/")[0] or None
    if "youtube.com" in host or "m.youtube.com" in host:
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/shorts/")[1].split("/")[0] or None
        qs = parse_qs(parsed.query or "")
        return qs.get("v", [None])[0]
    return None


def _load_drum_roadmap_static() -> dict:
    try:
        with open(DRUM_ROADMAP_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"drum_roadmap.json 読込失敗: {e}")
        return {"instrument": "drum", "phases": []}


async def _load_drum_roadmap_links() -> dict:
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return {}
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return {}
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return {}
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, DRUM_ROADMAP_LINKS_FILE)
    if not f_id:
        return {}
    try:
        raw = await drive.read_text_file(service, f_id)
        return json.loads(raw) or {}
    except Exception as e:
        logging.warning(f"drum_roadmap_links.json 読込失敗: {e}")
        return {}


async def _save_drum_roadmap_links(data: dict) -> None:
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, DRUM_ROADMAP_LINKS_FILE)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    if f_id:
        await drive.update_text(service, f_id, content)
    else:
        await drive.upload_text(service, b_folder, DRUM_ROADMAP_LINKS_FILE, content)


class DrumRoadmapLinkAddRequest(BaseModel):
    milestone_id: str
    url: str


class DrumRoadmapLinkDeleteRequest(BaseModel):
    milestone_id: str
    video_id: str


@router.get("/links", dependencies=[Depends(verify_api_key)])
async def get_drum_roadmap_links():
    links_map = await _load_drum_roadmap_links()
    return {"ok": True, "links": links_map}


@router.get("", dependencies=[Depends(verify_api_key)])
async def get_drum_roadmap():
    roadmap = _load_drum_roadmap_static()
    links_map = await _load_drum_roadmap_links()
    phases = []
    for ph in roadmap.get("phases", []):
        milestones = []
        for m in ph.get("milestones", []):
            mid = m.get("id")
            milestones.append({
                "id": mid,
                "label": m.get("label", ""),
                "criteria": m.get("criteria", ""),
                "est_hours": m.get("est_hours"),
                "videos": list(links_map.get(mid, [])),
            })
        phases.append({
            "id": ph.get("id"),
            "label": ph.get("label"),
            "description": ph.get("description", ""),
            "milestones": milestones,
        })
    return {"ok": True, "instrument": roadmap.get("instrument", "drum"), "phases": phases}


@router.post("/links/add", dependencies=[Depends(verify_api_key)])
async def add_drum_roadmap_link(req: DrumRoadmapLinkAddRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"ok": False, "error": "Bot 未初期化"}
    video_id = _extract_youtube_video_id(req.url)
    if not video_id:
        return {"ok": False, "error": "YouTube URL を解釈できませんでした"}
    from services.webclip_service import WebClipService
    title = ""
    author = ""
    try:
        wc = WebClipService(getattr(bot, "drive_service", None), getattr(bot, "gemini_client", None))
        info = await wc.get_youtube_info(req.url)
        if info:
            title = info.get("title") or ""
            author = info.get("author_name") or ""
    except Exception as e:
        logging.warning(f"YouTube oEmbed 取得失敗: {e}")
    roadmap = _load_drum_roadmap_static()
    valid_ids = {m["id"] for ph in roadmap.get("phases", []) for m in ph.get("milestones", [])}
    if req.milestone_id not in valid_ids:
        return {"ok": False, "error": "未知の milestone_id です"}

    links = await _load_drum_roadmap_links()
    arr = links.setdefault(req.milestone_id, [])
    arr = [x for x in arr if x.get("video_id") != video_id]
    arr.append({
        "video_id": video_id,
        "url": req.url,
        "title": title or req.url,
        "author": author,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "added_at": datetime.datetime.now(JST).isoformat(),
    })
    links[req.milestone_id] = arr
    await _save_drum_roadmap_links(links)
    return {"ok": True, "videos": arr}


@router.post("/links/delete", dependencies=[Depends(verify_api_key)])
async def delete_drum_roadmap_link(req: DrumRoadmapLinkDeleteRequest):
    links = await _load_drum_roadmap_links()
    arr = links.get(req.milestone_id) or []
    after = [x for x in arr if x.get("video_id") != req.video_id]
    if len(after) == len(arr):
        return {"ok": False, "error": "該当する動画が見つかりません"}
    links[req.milestone_id] = after
    await _save_drum_roadmap_links(links)
    return {"ok": True}
