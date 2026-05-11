"""Gemini API のトークン使用量から円換算コストを算出するサービス。

設計方針:
- 単価は USD/1M tokens。`MODEL_PRICING` テーブルで管理し、未知モデルは `_default` を使う。
- 円換算は `app_settings` の `usd_jpy_rate`（既定 150）を使用。
- 閾値・自動格下げ・頻度抑制の判定は本サービスのトップレベル関数として提供し、各 Cog から呼ぶ。
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from config import JST
from api.database import (
    get_api_usage_by_day,
    get_api_usage_by_model,
    get_app_setting,
    set_app_setting,
)

# USD per 1,000,000 tokens
# 値は概算。Google が公表する正確な単価はモデル世代ごとに変わるため `app_settings` でも上書き可。
MODEL_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-pro":   {"input": 1.25,  "output": 10.00},
    "gemini-2.5-flash": {"input": 0.30,  "output": 2.50},
    "gemini-2.5-flash-preview-04-17": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10,  "output": 0.40},
    "gemini-1.5-pro":   {"input": 1.25,  "output": 5.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    # 未知モデルへのフォールバック（pro 相当の悲観値）
    "_default":         {"input": 1.25,  "output": 10.00},
}

# 設定キー
SETTING_USD_JPY = "usd_jpy_rate"
SETTING_MONTHLY_THRESHOLD_JPY = "monthly_threshold_jpy"
SETTING_AUTO_DOWNGRADE = "auto_downgrade_to_flash"
SETTING_INFRA_COST_JPY = "infra_cost_jpy_per_month"
SETTING_LAST_ALERT_DATE = "cost_alert_last_date"

DEFAULT_USD_JPY = 150.0
DEFAULT_THRESHOLD_JPY = 3000.0
DEFAULT_INFRA_JPY = 0.0


def _pricing_for(model: str) -> dict:
    return MODEL_PRICING.get(model) or MODEL_PRICING["_default"]


def usd_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    p = _pricing_for(model)
    return (in_tokens / 1_000_000.0) * p["input"] + (out_tokens / 1_000_000.0) * p["output"]


async def get_usd_jpy_rate() -> float:
    raw = await get_app_setting(SETTING_USD_JPY, str(DEFAULT_USD_JPY))
    try:
        v = float(raw)
        return v if v > 0 else DEFAULT_USD_JPY
    except (TypeError, ValueError):
        return DEFAULT_USD_JPY


async def get_monthly_threshold_jpy() -> float:
    raw = await get_app_setting(SETTING_MONTHLY_THRESHOLD_JPY, str(DEFAULT_THRESHOLD_JPY))
    try:
        v = float(raw)
        return v if v > 0 else DEFAULT_THRESHOLD_JPY
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_JPY


async def get_auto_downgrade() -> bool:
    raw = await get_app_setting(SETTING_AUTO_DOWNGRADE, "0")
    return raw in ("1", "true", "True", "yes")


async def get_infra_cost_jpy() -> float:
    raw = await get_app_setting(SETTING_INFRA_COST_JPY, str(DEFAULT_INFRA_JPY))
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_INFRA_JPY


async def summary(start_date: str, end_date: str) -> dict:
    """指定期間の API コスト集計を返す（円換算込み）。"""
    rate = await get_usd_jpy_rate()
    by_day_rows = await get_api_usage_by_day(start_date, end_date)
    by_model_rows = await get_api_usage_by_model(start_date, end_date)

    # 日付ごと（モデル横断で合算）
    by_day_map: dict[str, dict] = {}
    for r in by_day_rows:
        d = r["date"]
        if d not in by_day_map:
            by_day_map[d] = {"date": d, "in_tokens": 0, "out_tokens": 0, "request_count": 0, "usd": 0.0, "jpy": 0.0}
        usd = usd_cost(r["model"], r["in_tokens"], r["out_tokens"])
        by_day_map[d]["in_tokens"] += r["in_tokens"]
        by_day_map[d]["out_tokens"] += r["out_tokens"]
        by_day_map[d]["request_count"] += r["request_count"]
        by_day_map[d]["usd"] += usd
        by_day_map[d]["jpy"] += usd * rate
    by_day = sorted(by_day_map.values(), key=lambda x: x["date"])

    # モデルごと
    by_model = []
    total_usd = 0.0
    total_in = 0
    total_out = 0
    total_req = 0
    for r in by_model_rows:
        usd = usd_cost(r["model"], r["in_tokens"], r["out_tokens"])
        total_usd += usd
        total_in += r["in_tokens"]
        total_out += r["out_tokens"]
        total_req += r["request_count"]
        by_model.append({
            "model": r["model"],
            "in_tokens": r["in_tokens"],
            "out_tokens": r["out_tokens"],
            "request_count": r["request_count"],
            "usd": round(usd, 4),
            "jpy": round(usd * rate, 1),
        })
    by_model.sort(key=lambda x: x["jpy"], reverse=True)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "usd_jpy_rate": rate,
        "by_day": [
            {**d, "usd": round(d["usd"], 4), "jpy": round(d["jpy"], 1)}
            for d in by_day
        ],
        "by_model": by_model,
        "total_in_tokens": total_in,
        "total_out_tokens": total_out,
        "total_request_count": total_req,
        "total_usd": round(total_usd, 4),
        "total_jpy": round(total_usd * rate, 1),
    }


async def current_month_jpy() -> float:
    """今月のAPI概算コスト（円）。閾値判定で使う。"""
    now = datetime.datetime.now(JST)
    start = now.replace(day=1).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    s = await summary(start, end)
    return float(s.get("total_jpy", 0))


async def should_downgrade_pro_to_flash() -> bool:
    """今月のコストが閾値の 70% を超え、ユーザーが auto_downgrade を有効化していれば True。
    PartnerCog や ChatService がモデル選択時に呼び出す。"""
    if not await get_auto_downgrade():
        return False
    try:
        used = await current_month_jpy()
        threshold = await get_monthly_threshold_jpy()
        return used >= threshold * 0.7
    except Exception as e:
        logging.debug(f"should_downgrade check failed: {e}")
        return False


async def should_throttle_heavy_tasks() -> bool:
    """重い処理（朝のMIT生成や週次レビュー）を当日スキップすべきか。
    今月のコストが既に閾値を超過していれば True を返す。"""
    try:
        used = await current_month_jpy()
        threshold = await get_monthly_threshold_jpy()
        return used >= threshold
    except Exception:
        return False


def downgrade_model_if_needed(requested_model: str, downgrade: bool) -> str:
    """downgrade=True かつ pro 系を要求している場合は flash に置き換える。"""
    if not downgrade or not requested_model:
        return requested_model
    if "pro" in requested_model and "flash" not in requested_model:
        return "gemini-2.5-flash"
    return requested_model


async def get_settings() -> dict:
    return {
        "usd_jpy_rate": await get_usd_jpy_rate(),
        "monthly_threshold_jpy": await get_monthly_threshold_jpy(),
        "auto_downgrade_to_flash": await get_auto_downgrade(),
        "infra_cost_jpy_per_month": await get_infra_cost_jpy(),
    }


async def update_settings(payload: dict) -> dict:
    """渡された設定を永続化。未知のキーは無視。"""
    if "usd_jpy_rate" in payload:
        try:
            v = float(payload["usd_jpy_rate"])
            if v > 0:
                await set_app_setting(SETTING_USD_JPY, str(v))
        except (TypeError, ValueError):
            pass
    if "monthly_threshold_jpy" in payload:
        try:
            v = float(payload["monthly_threshold_jpy"])
            if v >= 0:
                await set_app_setting(SETTING_MONTHLY_THRESHOLD_JPY, str(v))
        except (TypeError, ValueError):
            pass
    if "auto_downgrade_to_flash" in payload:
        await set_app_setting(
            SETTING_AUTO_DOWNGRADE,
            "1" if payload["auto_downgrade_to_flash"] else "0",
        )
    if "infra_cost_jpy_per_month" in payload:
        try:
            v = float(payload["infra_cost_jpy_per_month"])
            if v >= 0:
                await set_app_setting(SETTING_INFRA_COST_JPY, str(v))
        except (TypeError, ValueError):
            pass
    return await get_settings()


async def get_last_alert_date() -> str:
    return await get_app_setting(SETTING_LAST_ALERT_DATE, "")


async def set_last_alert_date(date_str: str) -> None:
    await set_app_setting(SETTING_LAST_ALERT_DATE, date_str)
