"""マネージャー連絡スケジュールの設定値解決ヘルパー。

時刻と曜日は SCHEDULE_CATALOG に固定。ユーザーが設定画面で変更できるのは
ON/OFF のみ。`schedule.<task_key>.enabled` を app_settings から読む。
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from config import JST


# 設定画面に出すスケジュール一覧。
# category: "manager" = マネージャーがユーザーに連絡するタスク（重複しない時刻に固定）
#           "auto"    = 自動同期。ユーザーには連絡しない（ログのみ）
# fixed_time / fixed_dow は固定値。設定画面で変更不可。
SCHEDULE_CATALOG: list[dict] = [
    # ===== マネージャーがユーザーに連絡するタスク（時刻重複なし）=====
    {"key": "morning_mit",            "label": "朝のMIT",                 "time": "06:30", "dow": "daily",  "category": "manager", "description": "今日のMIT候補3件を朝に提案します。あなたの返信で確定。"},
    {"key": "auto_market_sentiment",  "label": "市場の地合い",            "time": "06:45", "dow": "daily",  "category": "manager", "description": "保有銘柄基準の朝の地合いレポートを通知ログへ。"},
    {"key": "morning_routine",        "label": "朝のルーチン",            "time": "07:00", "dow": "daily",  "category": "manager", "description": "今日の予定・タスク・天気・ニュース・過去の今日を一括お届け。"},
    {"key": "auto_alerts_earnings",   "label": "価格アラート＋決算予定",  "time": "07:15", "dow": "daily",  "category": "manager", "description": "保有銘柄の当日決算予定や価格アラートを通知。"},
    {"key": "fitbit_morning",         "label": "Fitbit 朝レポート",       "time": "08:00", "dow": "daily",  "category": "manager", "description": "前夜の睡眠データを取得し、朝のメッセージで報告。"},
    {"key": "auto_news_sentiment",    "label": "保有銘柄ニュース朝刊",    "time": "08:30", "dow": "daily",  "category": "manager", "description": "保有銘柄のニュースを通知ログに送信（差分のみ）。"},
    {"key": "cost_alert",             "label": "コストアラート",          "time": "09:00", "dow": "daily",  "category": "manager", "description": "Gemini API 利用コストの監視通知。"},
    {"key": "obsidian_review",        "label": "Obsidian 振り返り",       "time": "20:00", "dow": "daily",  "category": "manager", "description": "Obsidian の今日のデイリーノートを読んで、マネージャーが感想を返します。"},
    {"key": "weekend_stocks",         "label": "週末株レビュー",          "time": "20:15", "dow": "friday", "category": "manager", "description": "金曜の市場クローズに合わせて週末の株式振り返り。"},
    {"key": "habit_check",            "label": "習慣チェック",            "time": "20:30", "dow": "daily",  "category": "manager", "description": "今日まだ完了していない習慣をリマインドします。"},
    {"key": "tomorrow_preview",       "label": "明日の予定",              "time": "21:00", "dow": "daily",  "category": "manager", "description": "明日のカレンダー予定と天気を前夜に通知。"},
    {"key": "weekly_review",          "label": "週次レビュー",            "time": "21:30", "dow": "sunday", "category": "manager", "description": "日曜の夜に1週間の振り返りを Drive に保存します。"},
    {"key": "evening_review",         "label": "夜の振り返り",            "time": "22:00", "dow": "daily",  "category": "manager", "description": "今日の会話ログ・MIT・翌日情報を統括して夜にレビュー。"},
    {"key": "fitbit_evening",         "label": "Fitbit 夜レポート",       "time": "22:15", "dow": "daily",  "category": "manager", "description": "日中アクティビティを取り込み、夜にレポート。"},
    {"key": "location_save_reminder", "label": "ロケーション保存リマインド", "time": "22:30", "dow": "daily",  "category": "manager", "description": "Google マップのタイムラインJSON保存をマネージャーがチャットで促します。"},
    {"key": "daily_organize",         "label": "デイリー整理",            "time": "23:55", "dow": "daily",  "category": "manager", "description": "タスク・チャットログ・天気を整理して Obsidian に保存し、おやすみメッセージ送信。"},

    # ===== 自動同期（ユーザーには通知せず内部処理のみ）=====
    {"key": "fitbit_night",           "label": "Fitbit キャッシュ事前取得", "time": "23:00", "dow": "daily",  "category": "auto",    "description": "翌日のグラフ表示用にキャッシュ。通知なし。"},
    {"key": "update_manual",          "label": "取扱説明書自動更新",       "time": "23:45", "dow": "daily",  "category": "auto",    "description": "会話ログからユーザー取扱説明書を裏側で更新。通知なし。"},
]

_DOW_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _catalog_lookup(task_key: str) -> Optional[dict]:
    for row in SCHEDULE_CATALOG:
        if row["key"] == task_key:
            return row
    return None


async def is_due(
    task_key: str,
    default_time: str,
    default_dow: str,
    last_run_date: Optional[datetime.date],
) -> tuple[bool, datetime.date]:
    """カタログ固定値と enabled フラグから「今この分に実行すべきか」を判定する。

    時刻・曜日はカタログを正とし、DB の値は無視する（設定UIから編集不可）。
    後方互換のため引数 default_time / default_dow は残しているが、カタログ
    が無い場合のフォールバックとしてのみ使う。
    """
    today = datetime.datetime.now(JST).date()
    try:
        from api.database import get_app_setting
    except Exception as e:
        logging.debug(f"schedule_resolver: db import failed: {e}")
        return False, last_run_date or today

    enabled = await get_app_setting(f"schedule.{task_key}.enabled", "1")
    if enabled != "1":
        return False, last_run_date

    if last_run_date == today:
        return False, last_run_date

    row = _catalog_lookup(task_key)
    time_str = row["time"] if row else default_time
    dow = row["dow"] if row else default_dow

    try:
        h, m = map(int, time_str.split(":", 1))
    except Exception:
        h, m = map(int, default_time.split(":", 1))

    now = datetime.datetime.now(JST)

    if dow != "daily":
        target_weekday = _DOW_MAP.get(dow)
        if target_weekday is not None and now.weekday() != target_weekday:
            return False, last_run_date

    if now.hour != h or now.minute != m:
        return False, last_run_date

    return True, today


async def is_enabled(task_key: str) -> bool:
    """ON/OFF 状態のみを問い合わせる（@tasks.loop(time=...) 系から使う）。"""
    try:
        from api.database import get_app_setting
    except Exception:
        return True
    val = await get_app_setting(f"schedule.{task_key}.enabled", "1")
    return val == "1"


def get_catalog() -> list[dict]:
    return list(SCHEDULE_CATALOG)
