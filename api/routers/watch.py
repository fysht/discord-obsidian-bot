"""視聴セッション（ダラ見対策）: 自動記録(Webhook) / 宣言タイマー / 見る前チェックイン。

- Tasker/MacroDroid が YouTube の起動・終了を `POST /watch/app_event` に投げて自動記録する。
- 「30分見る」の宣言は `POST /watch/declare`（マネージャーが受けて時間で回収）。
- 「何のために？」の目的入力は `POST /watch/{id}/reason`（空なら茶々を返す）。
"""

import datetime
import logging
import random

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import notification_service
from api.database import (
    watch_create_session, watch_get, watch_get_active, watch_list_active,
    watch_finalize, watch_set_reason, watch_extend, watch_list_by_date,
    watch_today_total_minutes,
)
from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/watch", tags=["watch"])

_APP_LABEL = {"youtube": "YouTube", "net": "ネット", "other": "アプリ"}

# 目的が空のときの軽い茶々（毎回少し変える）。
_NO_REASON_TEASE = [
    "目的なしか〜。ま、ほどほどにね😏",
    "なんとなく、だね？10分で戻っておいで👀",
    "目的が浮かばないなら、それはダラ見のサインかも🫣",
    "OK、でも『気づいたら1時間』には気をつけて⏳",
]


def _label(app: str) -> str:
    return _APP_LABEL.get((app or "youtube").strip(), "アプリ")


def _elapsed_sec(started_at: str) -> int:
    try:
        start = datetime.datetime.fromisoformat(started_at)
        return max(0, int((datetime.datetime.now(JST) - start).total_seconds()))
    except Exception:
        return 0


async def _append_obsidian(session: dict):
    """完了した視聴セッションをデイリーノートの Lifelog に1行記録する。"""
    try:
        from api.routers._obsidian_helpers import append_lifelog_line
        minutes = int((session.get("duration_sec") or 0) // 60)
        label = _label(session.get("app"))
        reason = (session.get("reason") or "").strip()
        line = f"- 📵 {label} {minutes}分" + (f"（目的: {reason}）" if reason else "（目的: 未記入）")
        await append_lifelog_line(session.get("date") or datetime.datetime.now(JST).strftime("%Y-%m-%d"), line)
    except Exception as e:
        logging.debug(f"watch obsidian append failed: {e}")


class AppEventRequest(BaseModel):
    app: str = "youtube"
    event: str  # open / close


@router.post("/app_event", dependencies=[Depends(verify_api_key)])
async def watch_app_event(req: AppEventRequest):
    """Tasker/MacroDroid からのアプリ起動・終了イベント。
    open で active セッションを作り目的チェックインを促し、close で滞在時間を確定する。"""
    app = (req.app or "youtube").strip() or "youtube"
    event = (req.event or "").strip().lower()
    label = _label(app)

    if event in ("open", "start"):
        existing = await watch_get_active(app)
        if existing:
            return {"ok": True, "id": existing["id"], "state": "already_active"}
        sid = await watch_create_session(app=app, source="webhook")
        # ❷ 見る前チェックイン: 開いた瞬間に「何のために？」を促す（摩擦を作る）。
        try:
            await notification_service.send_push(
                title=f"📵 {label}を開いたね",
                body="何のために見てる？目的を一言メモしよ（ダラ見ガード）",
                url=f"/?watchReason={sid}",
            )
        except Exception as e:
            logging.debug(f"watch open push failed: {e}")
        return {"ok": True, "id": sid, "state": "active"}

    if event in ("close", "stop"):
        active = await watch_get_active(app)
        if not active:
            return {"ok": True, "state": "no_active"}
        dur = _elapsed_sec(active.get("started_at"))
        await watch_finalize(active["id"], dur, status="done")
        session = await watch_get(active["id"])
        if session:
            await _append_obsidian(session)
        return {"ok": True, "id": active["id"], "state": "done", "duration_sec": dur}

    raise HTTPException(status_code=422, detail="event は open / close を指定してください")


class DeclareRequest(BaseModel):
    app: str = "youtube"
    minutes: int = 30
    reason: str = ""


@router.post("/declare", dependencies=[Depends(verify_api_key)])
async def watch_declare(req: DeclareRequest):
    """❶ 宣言タイマー: 「{minutes}分見る」を宣言。マネージャーが受けて時間で回収する。"""
    app = (req.app or "youtube").strip() or "youtube"
    minutes = max(1, min(int(req.minutes or 30), 240))
    label = _label(app)
    # 既に進行中の宣言があれば二重に作らない。
    active = await watch_get_active(app)
    if active and (active.get("declared_minutes") or 0) > 0:
        return {"ok": True, "id": active["id"], "state": "already_active"}
    sid = await watch_create_session(
        app=app, source="declare", declared_minutes=minutes, reason=(req.reason or "").strip(),
    )
    # マネージャーが「受けた」感を出すメッセージをチャットへ投下。
    try:
        from api.database import save_message
        reason = (req.reason or "").strip()
        ack = f"OK、{label}を{minutes}分だね⏳ {minutes}分たったら声かけるよ。"
        if reason:
            ack += f"\n目的:「{reason}」忘れずに👍"
        else:
            ack += "\n（目的があるなら一言あると後で振り返りやすいよ）"
        await save_message("assistant", ack)
    except Exception as e:
        logging.debug(f"watch declare ack failed: {e}")
    return {"ok": True, "id": sid, "state": "active", "minutes": minutes}


class ReasonRequest(BaseModel):
    reason: str = ""


@router.post("/{session_id}/reason", dependencies=[Depends(verify_api_key)])
async def watch_reason(session_id: int, req: ReasonRequest):
    """❷ 見る前チェックイン: 目的を保存。空なら軽い茶々メッセージを返す。"""
    session = await watch_get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="セッションが見つかりません")
    reason = (req.reason or "").strip()
    await watch_set_reason(session_id, reason)
    if not reason:
        return {"ok": True, "reason": "", "tease": random.choice(_NO_REASON_TEASE)}
    return {"ok": True, "reason": reason, "tease": ""}


class EndRequest(BaseModel):
    status: str = "done"          # done / abandoned
    extend_minutes: int = 0       # >0 なら終了せず延長する


@router.post("/{session_id}/end", dependencies=[Depends(verify_api_key)])
async def watch_end(session_id: int, req: EndRequest):
    """宣言/視聴セッションの回収。延長指定があれば終了せずタイマーを延ばす。"""
    session = await watch_get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="セッションが見つかりません")
    if int(req.extend_minutes or 0) > 0:
        await watch_extend(session_id, int(req.extend_minutes))
        return {"ok": True, "state": "extended", "added": int(req.extend_minutes)}
    if session.get("status") != "active":
        return {"ok": True, "state": session.get("status")}
    dur = _elapsed_sec(session.get("started_at"))
    status = "abandoned" if (req.status or "done").strip() == "abandoned" else "done"
    await watch_finalize(session_id, dur, status=status)
    finalized = await watch_get(session_id)
    if finalized:
        await _append_obsidian(finalized)
    return {"ok": True, "state": status, "duration_sec": dur}


@router.get("/active", dependencies=[Depends(verify_api_key)])
async def watch_active():
    """進行中セッション一覧（経過分・宣言分を付与）。"""
    rows = await watch_list_active()
    for r in rows:
        elapsed_min = _elapsed_sec(r.get("started_at")) // 60
        r["elapsed_minutes"] = elapsed_min
        dm = int(r.get("declared_minutes") or 0)
        r["remaining_minutes"] = max(0, dm - elapsed_min) if dm > 0 else None
        r["app_label"] = _label(r.get("app"))
    return {"items": rows}


@router.get("/today", dependencies=[Depends(verify_api_key)])
async def watch_today(date: str = ""):
    """当日（既定: 今日）のセッション一覧と合計分。"""
    if not date:
        date = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    rows = await watch_list_by_date(date)
    return {
        "date": date,
        "items": rows,
        "total_minutes": await watch_today_total_minutes(date),
    }
