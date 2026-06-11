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
    {"key": "auto_market_sentiment",  "label": "市場の地合い",            "time": "06:45", "dow": "weekday", "category": "manager", "description": "平日朝の地合いレポートをお知らせへ（土日は配信なし）。"},
    {"key": "morning_routine",        "label": "朝のルーチン",            "time": "07:00", "dow": "daily",  "category": "manager", "description": "今日の予定・タスク・天気・過去の今日を一括お届け。"},
    {"key": "breakfast_meal",         "label": "朝食ログ",                "time": "08:30", "dow": "daily",  "category": "manager", "description": "朝ごはんを記録する質問を投下（回答は朝食・8:30で記録）。"},
    {"key": "lunch_meal",             "label": "昼食ログ",                "time": "12:45", "dow": "daily",  "category": "manager", "description": "昼ごはんを記録する質問を投下（回答は昼食・12:45で記録）。"},
    {"key": "afternoon_check",        "label": "昼の振り返り",            "time": "14:30", "dow": "daily",  "category": "manager", "description": "午後の調子を1タップで記録。回答は Obsidian の独立セクションに保存。"},
    {"key": "dinner_meal",            "label": "夕食ログ",                "time": "20:00", "dow": "daily",  "category": "manager", "description": "晩ごはんを記録する質問を投下（回答は夕食・20:00で記録）。"},
    {"key": "english_quiz",           "label": "英語クイズ",              "time": "19:30", "dow": "daily",  "category": "manager", "description": "火・木・土に英単語/フレーズのクイズを出題（選択肢チップで気軽に学習）。"},
    {"key": "gratitude_check",        "label": "良かったこと・感謝",      "time": "20:45", "dow": "daily",  "category": "manager", "description": "今日良かったこと・感謝したいことを1つ記録。"},
    {"key": "evening_mood",           "label": "夜の気分チェック",        "time": "21:00", "dow": "daily",  "category": "manager", "description": "今日の気分を1タップで記録。回答は Obsidian の独立セクションに保存。"},
    {"key": "learning_check",         "label": "今日の学び・気づき",      "time": "21:15", "dow": "daily",  "category": "manager", "description": "今日の学び・気づきを記録。回答は Obsidian の独立セクションに保存。"},
    {"key": "auto_alerts_earnings",   "label": "価格アラート＋決算予定",  "time": "07:15", "dow": "daily",  "category": "manager", "description": "保有銘柄の当日決算予定や価格アラートを通知。"},
    {"key": "holdings_noon_review",   "label": "保有銘柄の昼チェック",    "time": "12:00", "dow": "weekday", "category": "manager", "description": "平日12時に保有銘柄の継続/縮小/売却をテクニカル×ファンダで診断しお知らせへ。12:30の売買判断の参考に（決定論的・無料）。"},
    {"key": "decision_review_verify", "label": "売買判断の答え合わせ",    "time": "15:45", "dow": "weekday", "category": "manager", "description": "平日の市場クローズ後に、過去の売買判断を市場平均と比べて答え合わせ（20/60営業日後）。新たに判定が出たときだけ的中率をお知らせへ。"},
    {"key": "auto_breakout_advise",   "label": "高値ブレイク×一括診断",  "time": "16:00", "dow": "weekday", "category": "manager", "description": "平日の大引け後に「じわじわ高値ブレイク」(topix500)で新規候補を抽出し、保有＋候補を一括診断（継続/売却・新規買い・入替・over_trading警告）してお知らせへ。"},
    {"key": "fitbit_morning",         "label": "Fitbit 朝レポート",       "time": "08:00", "dow": "daily",  "category": "manager", "description": "前夜の睡眠データを取得し、朝のメッセージで報告。"},
    {"key": "auto_news_sentiment",    "label": "保有銘柄ニュース朝刊",    "time": "08:30", "dow": "daily",  "category": "manager", "description": "保有銘柄のニュース差分を AI が短いダイジェスト（取り上げるべき銘柄のみ／好材料・悪材料・影響）にまとめてお知らせへ。"},
    {"key": "cost_alert",             "label": "コストアラート",          "time": "09:00", "dow": "daily",  "category": "manager", "description": "Gemini API 利用コストの監視通知。"},
    {"key": "obsidian_review",        "label": "Obsidian 振り返り",       "time": "20:00", "dow": "daily",  "category": "manager", "description": "Obsidian の今日のデイリーノートを読んで、マネージャーが感想を返します。"},
    {"key": "weekend_stocks",         "label": "週末株レビュー",          "time": "20:15", "dow": "friday", "category": "manager", "description": "金曜の市場クローズに合わせて週末の株式振り返り。"},
    {"key": "habit_check",            "label": "習慣チェック",            "time": "20:30", "dow": "daily",  "category": "manager", "description": "今日まだ完了していない習慣をリマインドします。"},
    {"key": "tomorrow_preview",       "label": "明日の予定",              "time": "21:00", "dow": "daily",  "category": "manager", "description": "明日のカレンダー予定と天気を前夜に通知。"},
    {"key": "weekly_review",          "label": "週次レビュー",            "time": "21:30", "dow": "sunday", "category": "manager", "description": "日曜の夜に1週間の振り返りを Drive に保存します。"},
    {"key": "evening_review",         "label": "夜の振り返り",            "time": "22:00", "dow": "daily",  "category": "manager", "description": "今日の会話ログ・MIT・翌日情報を統括して夜にレビュー。質問にチャット上で全て回答すると自動で Obsidian へ確定保存。"},
    {"key": "fitbit_evening",         "label": "Fitbit 夜レポート",       "time": "22:15", "dow": "daily",  "category": "manager", "description": "日中アクティビティを取り込み、夜にレポート。"},
    {"key": "location_save_reminder", "label": "ロケーション保存リマインド", "time": "22:30", "dow": "daily",  "category": "manager", "description": "Google マップのタイムラインJSON保存をチャットで促します（ロケーションログを開くボタン付き）。"},
    {"key": "daily_organize",         "label": "デイリー整理",            "time": "23:55", "dow": "daily",  "category": "manager", "description": "タスク・チャットログ・天気を整理して Obsidian に保存し、おやすみメッセージ送信。『次のアクション』はタスクへ自動登録せず、メッセージ内のボタンで個別に承認できます。"},

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
