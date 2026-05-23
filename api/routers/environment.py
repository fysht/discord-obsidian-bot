"""環境情報（天気・ロケーションログ手動同期）。"""

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST
from services.info_service import InfoService

router = APIRouter(prefix="", tags=["env"])


class LocationSyncRequest(BaseModel):
    date: str = ""
    date_from: str = ""
    date_to: str = ""


@router.post("/location_log/sync", dependencies=[Depends(verify_api_key)])
async def location_log_sync(req: LocationSyncRequest):
    """指定日付（または日付範囲）のロケーションログを Google Drive の Timeline JSON から同期する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    cog = bot.get_cog("LocationLogCog")
    if not cog:
        raise HTTPException(status_code=503, detail="LocationLogCogが利用できません。")

    date_from = (req.date_from or "").strip()
    date_to = (req.date_to or "").strip()
    single_date = (req.date or "").strip()

    if date_from and date_to:
        try:
            start = datetime.datetime.strptime(date_from, "%Y-%m-%d")
            end = datetime.datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="日付形式が不正です (YYYY-MM-DD)")
        if (end - start).days > 14:
            raise HTTPException(status_code=400, detail="最大14日間まで同期できます")
        if end < start:
            start, end = end, start

        results = []
        current = start
        while current <= end:
            d_str = current.strftime("%Y-%m-%d")
            try:
                r = await cog.perform_manual_sync(d_str)
                results.append(f"{d_str}: {r}")
            except Exception as e:
                results.append(f"{d_str}: エラー - {e}")
            current += datetime.timedelta(days=1)
        return {"status": "success", "message": "\n".join(results)}
    else:
        target_date = single_date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
        result = await cog.perform_manual_sync(target_date)
        return {"status": "success", "message": result}


@router.get("/weather", dependencies=[Depends(verify_api_key)])
async def get_weather_data(location: str = ""):
    """指定場所の天気データを返す。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    info_svc = getattr(bot, "info_service", None) if bot else None
    if not info_svc:
        info_svc = InfoService()
    loc = location.strip() or None
    return await info_svc.get_weather(location=loc)


@router.get("/weather/locations")
async def get_weather_locations():
    """利用可能な天気の場所一覧を返す。"""
    from services.info_service import YAHOO_WEATHER_REGIONS
    return {"regions": YAHOO_WEATHER_REGIONS}
