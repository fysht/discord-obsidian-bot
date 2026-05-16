"""マネージャー連絡スケジュールの設定値解決ヘルパー。

各 cog の `@tasks.loop(time=...)` を `@tasks.loop(minutes=1)` に置き換え、
内部で `is_due()` を呼んで「今この瞬間に実行すべきか」を判定するためのユーティリティ。

設定値は app_settings テーブルに以下のキーで保存:
    schedule.<task_key>.enabled  -> "1" / "0"
    schedule.<task_key>.time     -> "HH:MM" 形式
    schedule.<task_key>.dow      -> "daily" / "monday" .. "sunday"

設定が無ければ default_time / default_dow にフォールバック、enabled は default で "1"。
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from config import JST


# ユーザーが設定画面で編集可能なスケジュール一覧
# (task_key, label, default_time, default_dow, description)
# 並び順は時系列（朝 → 夜）
SCHEDULE_CATALOG: list[tuple[str, str, str, str, str]] = [
    ("morning_mit",      "朝のMIT",          "06:30", "daily",  "今日のMIT候補3件を朝に提案します。あなたの返信で確定。"),
    ("morning_routine",  "朝のルーチン",      "07:00", "daily",  "今日の予定・タスク・天気・ニュース・過去の今日を一括お届け。"),
    ("obsidian_review",  "Obsidian 振り返り", "20:00", "daily",  "Obsidian の今日のデイリーノートを読んで、マネージャーが感想を返します。"),
    ("weekend_stocks",   "週末株レビュー",    "20:00", "friday", "金曜の市場クローズに合わせて週末の株式振り返り。"),
    ("tomorrow_preview", "明日の予定",        "21:00", "daily",  "明日のカレンダー予定と天気を前夜に通知。"),
    ("habit_check",      "習慣チェック",      "21:00", "daily",  "今日まだ完了していない習慣をリマインドします。"),
    ("weekly_review",    "週次レビュー",      "21:00", "sunday", "日曜の夜に1週間の振り返りを Drive に保存します。"),
    ("evening_review",   "夜の振り返り",      "22:00", "daily",  "今日の会話ログ・MIT・翌日情報を統括して夜にレビュー。"),
    ("update_manual",    "取扱説明書更新",    "23:45", "daily",  "あなたの会話ログから「ユーザー取扱説明書」を自動更新。"),
    ("daily_organize",   "デイリー整理",      "23:55", "daily",  "タスク・チャットログ・天気を整理して Obsidian に保存。"),
    ("practice_reminder","ドラム練習リマインド","19:00", "daily",  "夜にドラム練習をリマインド。当日まだ未完了なら通知。"),
]

_DOW_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


async def is_due(
    task_key: str,
    default_time: str,
    default_dow: str,
    last_run_date: Optional[datetime.date],
) -> tuple[bool, datetime.date]:
    """設定値を読み、現在この分に実行すべきかを判定する。

    Returns:
        (should_run, today_date)
        - should_run: True なら呼び出し側がタスクを実行する
        - today_date: 呼び出し側が `last_run_date` に保持する値（True/False どちらでも返す）
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
        return False, last_run_date  # 同日重複防止

    time_str = await get_app_setting(f"schedule.{task_key}.time", default_time)
    dow = await get_app_setting(f"schedule.{task_key}.dow", default_dow)

    try:
        h, m = map(int, time_str.split(":", 1))
    except Exception:
        h, m = map(int, default_time.split(":", 1))

    now = datetime.datetime.now(JST)

    # 曜日チェック
    if dow != "daily":
        target_weekday = _DOW_MAP.get(dow)
        if target_weekday is None:
            target_weekday = _DOW_MAP.get(default_dow)
        if target_weekday is not None and now.weekday() != target_weekday:
            return False, last_run_date

    # 時刻チェック: 同じ時刻 (hour & minute) に達したらヒット
    if now.hour != h or now.minute != m:
        return False, last_run_date

    return True, today


def get_catalog() -> list[dict]:
    """SCHEDULE_CATALOG をシリアライズしやすい dict 形式で返す（API用）。"""
    return [
        {
            "key": k,
            "label": label,
            "default_time": dt,
            "default_dow": dow,
            "description": desc,
        }
        for k, label, dt, dow, desc in SCHEDULE_CATALOG
    ]
