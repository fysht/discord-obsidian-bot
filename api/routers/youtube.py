"""YouTube 登録チャンネルの新着動画（一覧 / あとで見る退避 / 状態更新 / 手動リフレッシュ）。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="/youtube", tags=["youtube"])


@router.get("/videos", dependencies=[Depends(verify_api_key)])
async def youtube_videos(state: str = "new", limit: int = 50):
    """登録チャンネルの新着動画一覧。state は new / later / watched / hidden / all。"""
    from api.database import youtube_list_videos, youtube_count_new
    state = (state or "new").strip().lower()
    if state not in ("new", "later", "saved", "watched", "hidden", "all"):
        state = "new"
    rows = await youtube_list_videos(state=state, limit=limit)
    return {
        "state": state,
        "items": rows,
        "new_count": await youtube_count_new(),
    }


@router.post("/{video_id}/later", dependencies=[Depends(verify_api_key)])
async def youtube_later(video_id: str):
    """「あとで見る」：stocked_links に目的付きで退避し、一覧からは later 状態にする。
    今ダラ見せず週末などにまとめて消化するための導線（ダラ見対策の中核）。"""
    from api.database import (
        youtube_get_video, youtube_update_video_state, add_stocked_link,
    )
    v = await youtube_get_video(video_id)
    if not v:
        raise HTTPException(status_code=404, detail="動画が見つかりません")
    title = (v.get("title") or "YouTube動画").strip()
    url = v.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    try:
        # type='youtube' で保存し、統合カードの「あとで見る」タブに表示されるようにする。
        await add_stocked_link(url, "youtube", title)
    except Exception as e:
        logging.error(f"youtube_later add_stocked_link error: {e}")
        raise HTTPException(status_code=500, detail="退避に失敗しました")
    await youtube_update_video_state(video_id, "later")
    return {"ok": True, "state": "later"}


class YouTubeSummaryRequest(BaseModel):
    refresh: bool = False  # True なら既存キャッシュを無視して再生成


@router.post("/{video_id}/summary", dependencies=[Depends(verify_api_key)])
async def youtube_summary(video_id: str, req: YouTubeSummaryRequest = YouTubeSummaryRequest()):
    """新着動画の要約をオンデマンド生成する（押した動画だけ・結果はキャッシュ）。
    字幕→Gemini Flash で要約。字幕が無ければ概要欄で代用。コスト最小化のため、
    一度生成した要約はDBに保存し、次回以降は再生成しない。"""
    from api import app
    from api.database import youtube_get_video, youtube_set_video_summary
    from services.youtube_service import summarize_video

    # youtube_videos に登録された動画（新着・あとで見る）はキャッシュ＆メタを使う。
    # 手動追加のYouTubeリンクなど未登録でも、video_id から要約だけは生成する。
    v = await youtube_get_video(video_id) or {}

    cached = (v.get("summary") or "").strip()
    if cached and not req.refresh:
        return {"ok": True, "summary": cached, "cached": True}

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        # 低コストモデル（Flash系）を既定にする。大量要約でもコストを抑える狙い。
        model = await _rgm("youtube_summary", default_pro=False)
    except Exception:
        model = "gemini-2.5-flash"

    res = await summarize_video(
        bot.gemini_client, model, video_id,
        title=v.get("title") or "",
        url=v.get("url") or f"https://www.youtube.com/watch?v={video_id}",
        description=v.get("description") or "",
    )
    if not res.get("ok"):
        # 失敗理由をそのまま返して原因を切り分けられるようにする
        raise HTTPException(status_code=502, detail=res.get("error") or "要約に失敗しました")

    summary = res["summary"]
    # 登録済み動画のみキャッシュ（未登録リンクは行が無いのでスキップ）
    if v:
        await youtube_set_video_summary(video_id, summary)
    return {"ok": True, "summary": summary, "cached": False, "source": res.get("source")}


class YouTubeStateRequest(BaseModel):
    state: str


@router.post("/{video_id}/state", dependencies=[Depends(verify_api_key)])
async def youtube_set_state(video_id: str, req: YouTubeStateRequest):
    """動画の状態を更新する（watched / hidden / new / later）。"""
    from api.database import youtube_update_video_state
    state = (req.state or "").strip().lower()
    if state not in ("new", "later", "saved", "watched", "hidden"):
        raise HTTPException(status_code=422, detail="不正な state です")
    ok = await youtube_update_video_state(video_id, state)
    if not ok:
        raise HTTPException(status_code=404, detail="動画が見つかりません")
    return {"ok": True, "state": state}


class YouTubeBulkRequest(BaseModel):
    ids: list[str]
    action: str  # later / hidden / watched


@router.post("/bulk", dependencies=[Depends(verify_api_key)])
async def youtube_bulk(req: YouTubeBulkRequest):
    """選択した動画をまとめて処理する。later=あとで見る退避 / hidden=非表示 / watched=見た。
    一度 hidden/later/watched にした動画は id（=動画ID）主キーの INSERT OR IGNORE で
    再ポーリングしても状態が保たれるため、再び新着に出てくることはない。"""
    from api.database import youtube_update_video_state
    action = (req.action or "").strip().lower()
    if action not in ("later", "saved", "hidden"):
        raise HTTPException(status_code=422, detail="不正な action です")
    ids = [str(i) for i in (req.ids or []) if str(i).strip()]
    count = 0
    for vid in ids:
        try:
            if await youtube_update_video_state(vid, action):
                count += 1
        except Exception as e:
            logging.debug(f"youtube_bulk error ({vid}): {e}")
    return {"ok": True, "count": count, "action": action}


@router.get("/channels", dependencies=[Depends(verify_api_key)])
async def youtube_channels():
    """登録チャンネル一覧（ミュート設定UI用）。enabled=1 が取り込み対象。"""
    from api.database import youtube_list_channels
    return {"channels": await youtube_list_channels()}


class ChannelToggleRequest(BaseModel):
    enabled: bool


@router.post("/channels/{channel_id}/toggle", dependencies=[Depends(verify_api_key)])
async def youtube_channel_toggle(channel_id: str, req: ChannelToggleRequest):
    """チャンネルの新着取り込み ON/OFF（ミュート）を切り替える。"""
    from api.database import youtube_set_channel_enabled
    ok = await youtube_set_channel_enabled(channel_id, req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="チャンネルが見つかりません")
    return {"ok": True, "channel_id": channel_id, "enabled": req.enabled}


@router.post("/refresh", dependencies=[Depends(verify_api_key)])
async def youtube_refresh():
    """登録チャンネルを再取得し、全チャンネルの新着を即ポーリングする（初回・手動更新用）。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    cog = bot.get_cog("YouTubeWatchCog") if bot else None
    if not cog:
        raise HTTPException(status_code=503, detail="YouTube連携が無効です")
    subs = await cog._refresh_subscriptions()
    new_videos = await cog._poll_new_videos()
    return {"ok": True, "subscriptions": subs, "new_videos": new_videos}
