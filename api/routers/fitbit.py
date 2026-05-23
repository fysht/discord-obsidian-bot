"""Fitbit データ参照系エンドポイント（/sleep_trend, /fitbit_all_data）。

ヘルパー (_fitbit_get_or_fetch, _get_fitbit_semaphore, _sleep_trend_cache,
_FITBIT_METRICS) は routes.py に残置（dashboard などからも共有利用）。
"""

import datetime
import logging

from fastapi import APIRouter, Depends

from api.routes import (
    verify_api_key,
    _FITBIT_METRICS, _fitbit_get_or_fetch, _get_fitbit_semaphore, _sleep_trend_cache,
)
from config import JST

router = APIRouter(prefix="", tags=["fitbit"])


@router.get("/sleep_trend", dependencies=[Depends(verify_api_key)])
async def sleep_trend():
    from api import app
    now_dt = datetime.datetime.now(JST)
    cached = _sleep_trend_cache
    if cached["data"] and cached["expires_at"] and now_dt < cached["expires_at"]:
        return cached["data"]

    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"trend": []}
    fitbit_cog = bot.get_cog("FitbitCog")
    if not fitbit_cog or not fitbit_cog.is_ready:
        return {"trend": []}

    async def fetch_day(i):
        date = now_dt.date() - datetime.timedelta(days=i)
        try:
            async with _get_fitbit_semaphore():
                stats = await fitbit_cog.fitbit_service.get_stats(date)
            if stats:
                return {
                    "date": date.strftime("%m/%d"),
                    "score": stats.get("sleep_score"),
                    "duration": stats.get("total_sleep_minutes"),
                }
        except Exception:
            pass
        return {"date": date.strftime("%m/%d"), "score": None, "duration": None}

    results = []
    for i in range(6, -1, -1):
        results.append(await fetch_day(i))

    result = {"trend": results}
    cached["data"] = result
    recent_missing = any(results[i].get("score") is None for i in [-1, -2] if i + len(results) >= 0)
    ttl = datetime.timedelta(minutes=2) if recent_missing else datetime.timedelta(minutes=10)
    cached["expires_at"] = now_dt + ttl
    return result


@router.get("/fitbit_all_data", dependencies=[Depends(verify_api_key)])
async def fitbit_all_data(days: int = 14):
    """過去N日分のFitbitデータを返す（最大30日）。
    過去日はディスクキャッシュから即時応答、当日は 30 分 TTL でキャッシュする。
    全主要メトリクスを返却するためグラフ表示にも使える。"""
    from api import app
    days = max(1, min(days, 30))
    bot = getattr(app.state, "bot", None)
    fitbit_cog = bot.get_cog("FitbitCog") if bot else None
    if not fitbit_cog or not fitbit_cog.is_ready:
        return {"data": []}

    now_dt = datetime.datetime.now(JST)
    results = []
    for i in range(days - 1, -1, -1):
        date = now_dt.date() - datetime.timedelta(days=i)
        record = {}
        try:
            record = await _fitbit_get_or_fetch(fitbit_cog.fitbit_service, date)
        except Exception as e:
            logging.debug(f"fitbit fetch fail {date}: {e}")
        raw_dur = record.get("total_sleep_minutes")
        row = {
            "date": date.strftime("%m/%d"),
            "date_full": date.strftime("%Y-%m-%d"),
            "sleep_duration": fitbit_cog._format_minutes(raw_dur) if raw_dur else None,
        }
        for k in _FITBIT_METRICS:
            row[k] = record.get(k)
        row["calories"] = record.get("calories_out")
        results.append(row)

    return {"data": results}
