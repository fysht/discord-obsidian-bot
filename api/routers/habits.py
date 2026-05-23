"""習慣（Habits）関連エンドポイント。

GET    /habits           — 今日の一覧（Google Tasks の「習慣」リストと HabitCog データの統合）
POST   /habits/complete  — 完了
POST   /habits/uncomplete — 未完了に戻す
POST   /habits/add       — 新規追加（同名既存があれば weekdays 更新）
POST   /habits/update    — 未実装（互換のため 501 を返す）
POST   /habits/trigger   — トリガー（いつやるか）の設定
POST   /habits/delete    — 削除（Google Tasks 側からも消す）
GET    /habits/history   — 日別の達成率履歴
GET    /habits/gantt     — ガントチャート用の習慣別履歴
"""

import datetime
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/habits", tags=["habits"])


# ===== 互換用ヘルパー =====

def _parse_habit_trigger(notes: str) -> tuple[str, str]:
    """notes 先頭行が "⏰ <trigger>" なら trigger と残りに分割。なければ ('', notes)"""
    if not notes:
        return "", ""
    lines = notes.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("⏰"):
        trigger = first[1:].lstrip(" ：:").strip()
        rest = "\n".join(lines[1:]).lstrip("\n")
        return trigger, rest
    return "", notes


# ===== Requests =====

class HabitCompleteRequest(BaseModel):
    habit_name: str


class HabitAddRequest(BaseModel):
    name: str
    frequency_days: int = 1
    weekdays: Optional[List[int]] = None  # 0=月..6=日, None or 空 → 毎日


class HabitTriggerRequest(BaseModel):
    habit_name: str
    trigger: str = ""


# ===== Endpoints =====

@router.get("", dependencies=[Depends(verify_api_key)])
async def get_habits():
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    tasks_service = getattr(bot, "tasks_service", None) if bot else None

    if not tasks_service:
        return {"habits": [], "today_done": [], "streaks": {}}

    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")

    raw_uncompleted = await tasks_service.get_raw_tasks("習慣")
    completed_today_titles = await tasks_service.get_completed_tasks_today("習慣")

    task_meta_by_name = {}
    for t in raw_uncompleted:
        trig_notes, _ = _parse_habit_trigger(t.get("notes", ""))
        task_meta_by_name[t["title"]] = {"task_id": t["id"], "trigger_notes": trig_notes}

    all_names = [t["title"] for t in raw_uncompleted] + completed_today_titles
    if not all_names:
        return {"habits": [], "today_done": [], "streaks": {}}

    def _meta(name):
        return task_meta_by_name.get(name, {"task_id": "", "trigger_notes": ""})

    if not habit_cog:
        habits_list = []
        for i, n in enumerate(all_names):
            m = _meta(n)
            habits_list.append({
                "id": str(i), "name": n, "frequency_days": 1,
                "trigger": m["trigger_notes"], "task_id": m["task_id"],
            })
        today_done = [str(i) for i, n in enumerate(all_names) if n in completed_today_titles]
        return {"habits": habits_list, "today_done": today_done, "streaks": {}}

    data = await habit_cog._load_data()
    changed = False
    for name in all_names:
        existing = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if not existing:
            existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
            new_id = str(max(existing_ids) + 1)
            data["habits"].append({"id": new_id, "name": name, "frequency_days": 1, "trigger": ""})
            changed = True

    for h in data["habits"]:
        if not h.get("trigger"):
            m = _meta(h["name"])
            if m.get("trigger_notes"):
                h["trigger"] = m["trigger_notes"]
                changed = True

    if today_str not in data["logs"]:
        data["logs"][today_str] = []
    for name in completed_today_titles:
        matching = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if matching and matching["id"] not in data["logs"][today_str]:
            data["logs"][today_str].append(matching["id"])
            changed = True

    if changed:
        await habit_cog._save_data(data)

    today_logs = data.get("logs", {}).get(today_str, [])
    today_date = datetime.datetime.now(JST).date()

    def _is_due_today(habit_data: dict, h_id: str) -> bool:
        freq = habit_data.get("frequency_days", 1)
        if freq <= 1:
            return True
        for i in range(1, 90):
            d = (today_date - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            if h_id in data.get("logs", {}).get(d, []):
                return i >= freq
        return True

    habits_list = []
    streaks = {}
    for name in all_names:
        matching = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if matching:
            m = _meta(name)
            freq = matching.get("frequency_days", 1)
            due_today = _is_due_today(matching, matching["id"])
            trigger_val = matching.get("trigger", "") or m.get("trigger_notes", "")
            weekdays = matching.get("weekdays") or []
            if weekdays and today_date.weekday() not in weekdays:
                due_today = False
            habits_list.append({
                "id": matching["id"],
                "name": matching["name"],
                "frequency_days": freq,
                "weekdays": weekdays,
                "trigger": trigger_val,
                "task_id": m["task_id"],
                "due_today": due_today,
            })
            streaks[matching["id"]] = habit_cog._get_habit_stats(data, matching["id"], today_str)

    return {"habits": habits_list, "today_done": today_logs, "streaks": streaks}


@router.post("/complete", dependencies=[Depends(verify_api_key)])
async def complete_habit(req: HabitCompleteRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"status": "error", "message": "HabitCog not available"}
    result_msg = await habit_cog.complete_habit(req.habit_name)
    return {"status": "success", "message": result_msg}


@router.post("/uncomplete", dependencies=[Depends(verify_api_key)])
async def uncomplete_habit(req: HabitCompleteRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"status": "error", "message": "HabitCog not available"}
    result_msg = await habit_cog.uncomplete_habit(req.habit_name)
    return {"status": "success", "message": result_msg}


@router.post("/add", dependencies=[Depends(verify_api_key)])
async def add_habit(req: HabitAddRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        raise HTTPException(status_code=503, detail="HabitCog不在")

    weekdays = sorted({d for d in (req.weekdays or []) if isinstance(d, int) and 0 <= d <= 6})

    data = await habit_cog._load_data()
    existing = next((h for h in data["habits"] if h["name"].lower() == req.name.lower()), None)
    if not existing:
        existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
        new_id = str(max(existing_ids) + 1)
        data["habits"].append({
            "id": new_id,
            "name": req.name,
            "frequency_days": req.frequency_days,
            "weekdays": weekdays,
        })
        await habit_cog._save_data(data)
    else:
        existing["weekdays"] = weekdays
        await habit_cog._save_data(data)

    if hasattr(bot, "tasks_service") and bot.tasks_service:
        await bot.tasks_service.add_task(req.name, list_name="習慣")

    return {"status": "success"}


@router.post("/update", dependencies=[Depends(verify_api_key)])
async def update_habit(req: BaseModel):
    raise HTTPException(status_code=501, detail="この機能は未実装です。")


@router.post("/trigger", dependencies=[Depends(verify_api_key)])
async def set_habit_trigger(req: HabitTriggerRequest):
    """習慣の trigger（いつやるか）を habit_data に永続化する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        raise HTTPException(status_code=503, detail="HabitCog 未起動")

    data = await habit_cog._load_data()
    target = next((h for h in data["habits"] if h["name"] == req.habit_name), None)
    if not target:
        target = next(
            (h for h in data["habits"] if req.habit_name.lower() in h["name"].lower()), None,
        )
    if not target:
        existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
        new_id = str(max(existing_ids) + 1)
        target = {"id": new_id, "name": req.habit_name, "frequency_days": 1, "trigger": ""}
        data["habits"].append(target)

    target["trigger"] = req.trigger.strip()
    await habit_cog._save_data(data)
    return {"status": "success", "trigger": target["trigger"]}


@router.post("/delete", dependencies=[Depends(verify_api_key)])
async def delete_habit_endpoint(req: HabitCompleteRequest):
    """習慣を削除し、Google Tasks の「習慣」リストからも消す。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        raise HTTPException(status_code=503, detail="HabitCog不在")

    msg = await habit_cog.delete_habit(req.habit_name)

    if hasattr(bot, "tasks_service") and bot.tasks_service:
        try:
            raw_tasks = await bot.tasks_service.get_raw_tasks("習慣")
            for t in raw_tasks or []:
                if (t.get("title") or "").strip().lower() == req.habit_name.strip().lower():
                    await bot.tasks_service.delete_task(t.get("id"), list_name="習慣")
        except Exception as e:
            logging.debug(f"habit GTasks delete failed: {e}")

    return {"status": "success", "message": msg}


@router.get("/history", dependencies=[Depends(verify_api_key)])
async def get_habit_history(days: int = 28):
    import datetime as dt
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"history": []}
    days = max(1, min(days, 180))
    data = await habit_cog._load_data()
    today = dt.datetime.now(JST).date()
    total_habits = len(data.get("habits", []))
    history = []
    for i in range(days - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        done = len(data.get("logs", {}).get(d_str, []))
        rate = (done / total_habits) if total_habits > 0 else 0.0
        history.append({"date": d.strftime("%m/%d"), "rate": round(rate, 2), "done": done, "total": total_habits})
    return {"history": history}


@router.get("/gantt", dependencies=[Depends(verify_api_key)])
async def get_habit_gantt(days: int = 90):
    """各習慣ごとの達成履歴をガントチャート用に返す。"""
    import datetime as dt
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"habits": [], "dates": []}
    days = max(7, min(days, 180))
    data = await habit_cog._load_data()
    today = dt.datetime.now(JST).date()
    dates = [(today - dt.timedelta(days=i)) for i in range(days - 1, -1, -1)]
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    logs = data.get("logs", {})
    habits = []
    for h in data.get("habits", []):
        h_id = h["id"]
        cells = [1 if h_id in logs.get(ds, []) else 0 for ds in date_strs]
        habits.append({
            "id": h_id,
            "name": h["name"],
            "cells": cells,
        })
    return {
        "habits": habits,
        "dates": [d.strftime("%m/%d") for d in dates],
        "date_strs": date_strs,
    }
