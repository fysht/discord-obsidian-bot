"""その他の設定エンドポイント（schedules / gem_urls）。"""

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="/settings", tags=["settings"])


# ===== マネージャー連絡スケジュール / 自動同期 =====

def _schedule_setting_key(task_key: str, field: str) -> str:
    return f"schedule.{task_key}.{field}"


_DOW_LABEL = {
    "daily": "毎日", "monday": "月", "tuesday": "火", "wednesday": "水",
    "thursday": "木", "friday": "金", "saturday": "土", "sunday": "日",
}


class SettingsSchedulesRequest(BaseModel):
    values: dict  # {task_key: {"enabled": bool}}


@router.get("/schedules", dependencies=[Depends(verify_api_key)])
async def settings_schedules_get():
    """カタログを 2 グループ（manager / auto）に分けて返す。時刻は固定で読取専用。"""
    from api.database import get_app_setting
    from services.schedule_resolver import SCHEDULE_CATALOG
    manager = []
    auto = []
    for row in SCHEDULE_CATALOG:
        enabled = await get_app_setting(_schedule_setting_key(row["key"], "enabled"), "1")
        entry = {
            "key": row["key"],
            "label": row["label"],
            "description": row["description"],
            "time": row["time"],
            "dow": row["dow"],
            "dow_label": _DOW_LABEL.get(row["dow"], row["dow"]),
            "category": row["category"],
            "enabled": enabled == "1",
        }
        if row["category"] == "manager":
            manager.append(entry)
        else:
            auto.append(entry)
    return {"ok": True, "manager": manager, "auto": auto}


@router.post("/schedules", dependencies=[Depends(verify_api_key)])
async def settings_schedules_post(req: SettingsSchedulesRequest):
    """ON/OFF のみ受け付ける。時刻・曜日はカタログ固定で変更不可。"""
    from api.database import set_app_setting
    from services.schedule_resolver import SCHEDULE_CATALOG
    valid_keys = {row["key"] for row in SCHEDULE_CATALOG}
    saved = 0
    for k, v in (req.values or {}).items():
        if k not in valid_keys or not isinstance(v, dict):
            continue
        if "enabled" in v:
            await set_app_setting(_schedule_setting_key(k, "enabled"), "1" if v["enabled"] else "0")
            saved += 1
    return {"ok": True, "saved": saved}


# ===== Gemini Gem URL =====

GEM_URL_CATALOG = [
    ("investment_screener", "スクリーナー質的分析"),
]


class SettingsGemUrlsRequest(BaseModel):
    values: dict  # {key: url, ...}


@router.get("/gem_urls", dependencies=[Depends(verify_api_key)])
async def settings_gem_urls_get():
    from api.database import get_app_setting
    items = []
    for key, label in GEM_URL_CATALOG:
        url = await get_app_setting(f"gem_url.{key}", "")
        items.append({"key": key, "label": label, "url": url})
    return {"ok": True, "items": items}


@router.post("/gem_urls", dependencies=[Depends(verify_api_key)])
async def settings_gem_urls_post(req: SettingsGemUrlsRequest):
    from api.database import set_app_setting
    valid_keys = {k for k, _ in GEM_URL_CATALOG}
    saved = 0
    for k, v in (req.values or {}).items():
        if k not in valid_keys:
            continue
        url = (v or "").strip()
        if url and not re.match(r"^https?://", url, flags=re.IGNORECASE):
            continue
        await set_app_setting(f"gem_url.{k}", url)
        saved += 1
    return {"ok": True, "saved": saved}
