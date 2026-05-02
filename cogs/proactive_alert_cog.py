"""プロアクティブ・リマインダー Cog。

15分ごとに走り、以下3種類の先回り通知を判定する:
  1. カレンダー予定の30分前ブリーフィング
  2. タスク締切の24h前 / 1h前
  3. 移動を伴う予定の出発時刻アラート

通知済みフラグは DB の proactive_alerts_sent テーブルで重複防止。
発火時は AI 生成メッセージを `save_message_and_notify` 経由で保存し、
PWA UI への表示と Web Push 通知を同時に行う。
"""

import logging
import datetime
import re

from discord.ext import commands, tasks

from config import JST
from prompts import (
    PROMPT_PROACTIVE_BRIEFING,
    PROMPT_TASK_DEADLINE_ALERT,
    PROMPT_DEPARTURE_ALERT,
    PROMPT_HABIT_TRIGGER,
)
from api.database import mark_alert_sent, cleanup_alert_keys


class ProactiveAlertCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.calendar_service = getattr(bot, "calendar_service", None)
        self.tasks_service = getattr(bot, "tasks_service", None)
        self.gemini_client = bot.gemini_client

        self.proactive_check_task.start()
        self.cleanup_task.start()

    def cog_unload(self):
        self.proactive_check_task.cancel()
        self.cleanup_task.cancel()

    @tasks.loop(minutes=15)
    async def proactive_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        now = datetime.datetime.now(JST)
        # 静かな時間帯（深夜）はスキップ
        if not (7 <= now.hour <= 22):
            return

        try:
            await self._check_calendar_briefing(partner_cog, now)
        except Exception as e:
            logging.error(f"proactive calendar briefing error: {e}", exc_info=True)

        try:
            await self._check_task_deadlines(partner_cog, now)
        except Exception as e:
            logging.error(f"proactive task deadline error: {e}", exc_info=True)

        try:
            await self._check_departure(partner_cog, now)
        except Exception as e:
            logging.error(f"proactive departure error: {e}", exc_info=True)

        try:
            await self._check_habit_triggers(partner_cog, now)
        except Exception as e:
            logging.error(f"proactive habit trigger error: {e}", exc_info=True)

    @tasks.loop(hours=12)
    async def cleanup_task(self):
        await cleanup_alert_keys(older_than_hours=48)

    async def _check_calendar_briefing(self, partner_cog, now: datetime.datetime):
        """30分後に始まる予定があれば、関連メモを添えてブリーフィング。"""
        if not self.calendar_service:
            return

        # 30分先までの予定を取得（next 35 分まで余裕を持つ）
        upcoming = await self.calendar_service.get_upcoming_events(minutes=35)
        if not upcoming:
            return

        for event in upcoming:
            event_id = event.get("id")
            summary = event.get("summary", "(タイトルなし)")
            start = event.get("start", {})
            start_time = start.get("dateTime")
            if not event_id or not start_time:
                continue

            try:
                start_dt = datetime.datetime.fromisoformat(start_time)
            except Exception:
                continue
            minutes_until = (start_dt - now).total_seconds() / 60.0
            # 25〜35分の窓に該当する予定だけ通知（15分間隔ループの取りこぼし防止）
            if not (25 <= minutes_until <= 35):
                continue

            key = f"briefing:{event_id}:{start_time}"
            if not await mark_alert_sent(key):
                continue

            # 関連メモを検索
            related_notes = ""
            try:
                related_notes = await partner_cog._search_drive_notes(summary)
            except Exception:
                related_notes = ""

            ctx = (
                f"【30分後の予定】{summary}\n"
                f"開始: {start_dt.strftime('%H:%M')}\n"
                f"場所: {event.get('location', '指定なし')}\n"
                f"説明: {event.get('description', 'なし')[:200]}\n\n"
                f"【関連する過去のメモ】\n{related_notes or '（特になし）'}"
            )
            await partner_cog.generate_and_send_routine_message(ctx, PROMPT_PROACTIVE_BRIEFING)

    async def _check_task_deadlines(self, partner_cog, now: datetime.datetime):
        """due が設定されたタスクの 24h前 / 1h前 アラート。"""
        if not self.tasks_service:
            return

        for list_name in ("仕事", "プライベート"):
            tasks_list = await self.tasks_service.get_raw_tasks(list_name)
            if not tasks_list:
                continue

            for t in tasks_list:
                due = t.get("due", "")
                if not due:
                    continue
                try:
                    due_dt = datetime.datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(JST)
                except Exception:
                    continue

                hours_until = (due_dt - now).total_seconds() / 3600.0
                trigger = None
                if 23.0 <= hours_until <= 24.5:
                    trigger = "24h"
                elif 0.75 <= hours_until <= 1.25:
                    trigger = "1h"
                if not trigger:
                    continue

                key = f"deadline:{t['id']}:{trigger}"
                if not await mark_alert_sent(key):
                    continue

                ctx = (
                    f"【締切が迫っているタスク】\n"
                    f"タイトル: {t['title']}\n"
                    f"リスト: {list_name}\n"
                    f"締切: {due_dt.strftime('%Y-%m-%d %H:%M')}\n"
                    f"残り: 約{trigger}前"
                )
                await partner_cog.generate_and_send_routine_message(ctx, PROMPT_TASK_DEADLINE_ALERT)

    async def _check_departure(self, partner_cog, now: datetime.datetime):
        """場所付き予定の出発時刻アラート。location があれば 45分前に発火。"""
        if not self.calendar_service:
            return

        upcoming = await self.calendar_service.get_upcoming_events(minutes=50)
        if not upcoming:
            return

        for event in upcoming:
            event_id = event.get("id")
            location = event.get("location", "")
            start = event.get("start", {})
            start_time = start.get("dateTime")
            if not event_id or not start_time or not location:
                continue
            try:
                start_dt = datetime.datetime.fromisoformat(start_time)
            except Exception:
                continue

            minutes_until = (start_dt - now).total_seconds() / 60.0
            if not (40 <= minutes_until <= 50):
                continue

            key = f"departure:{event_id}:{start_time}"
            if not await mark_alert_sent(key):
                continue

            ctx = (
                f"【移動を伴う予定】\n"
                f"予定: {event.get('summary', '(タイトルなし)')}\n"
                f"開始: {start_dt.strftime('%H:%M')}\n"
                f"場所: {location}\n"
                f"出発の目安: そろそろ"
            )
            await partner_cog.generate_and_send_routine_message(ctx, PROMPT_DEPARTURE_ALERT)

    @staticmethod
    def _parse_trigger_time(trigger: str) -> tuple[int, int] | None:
        """トリガー文字列から (hour, minute) を抽出。時刻でなければ None を返す。"""
        t = trigger.strip()
        # "午後3時30分", "午前7時" など
        m = re.search(r"午後\s*(\d{1,2})時(?:\s*(\d{1,2})分)?", t)
        if m:
            h = int(m.group(1)) + 12
            mi = int(m.group(2)) if m.group(2) else 0
            return (h % 24, mi)
        m = re.search(r"午前\s*(\d{1,2})時(?:\s*(\d{1,2})分)?", t)
        if m:
            return (int(m.group(1)) % 24, int(m.group(2)) if m.group(2) else 0)
        # "7時30分", "8時"
        m = re.search(r"(\d{1,2})時(?:\s*(\d{1,2})分)?", t)
        if m:
            return (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)
        # "7:30", "07:30"
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None

    async def _check_habit_triggers(self, partner_cog, now: datetime.datetime):
        """習慣トリガーの時刻と一致する習慣がある場合、マネージャーから声をかける。"""
        habit_cog = self.bot.get_cog("HabitCog")
        if not habit_cog:
            return

        data = await habit_cog._load_data()
        today_str = now.strftime("%Y-%m-%d")
        today_logs = data.get("logs", {}).get(today_str, [])

        # トリガーはGoogle Tasksのnotesから取得（routes.pyの_parse_habit_trigger相当）
        tasks_service = getattr(self.bot, "tasks_service", None)
        trigger_map: dict[str, str] = {}
        if tasks_service:
            try:
                raw = await tasks_service.get_raw_tasks("習慣")
                for t in raw:
                    notes = t.get("notes", "")
                    lines = notes.splitlines()
                    first = lines[0].strip() if lines else ""
                    if first.startswith("⏰"):
                        trig = first[1:].lstrip(" ：:").strip()
                        trigger_map[t["title"]] = trig
            except Exception as e:
                logging.error(f"habit trigger: tasks fetch error: {e}")

        for habit in data.get("habits", []):
            h_id = habit["id"]
            h_name = habit["name"]
            freq = habit.get("frequency_days", 1)
            trigger = trigger_map.get(h_name, "")
            if not trigger:
                continue

            # 既に今日完了済みならスキップ
            if h_id in today_logs:
                continue

            # 非毎日習慣: 前回完了から freq 日以上経過していなければスキップ
            if freq > 1:
                last_done = None
                for i in range(1, 60):
                    d = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                    if h_id in data.get("logs", {}).get(d, []):
                        last_done = now.date() - datetime.timedelta(days=i)
                        break
                if last_done is not None:
                    days_since = (now.date() - last_done).days
                    if days_since < freq:
                        continue

            # 時刻ベーストリガーのみ発火（文脈トリガーは現時点ではスキップ）
            parsed = self._parse_trigger_time(trigger)
            if parsed is None:
                continue

            trig_h, trig_m = parsed
            trig_minutes = trig_h * 60 + trig_m
            now_minutes = now.hour * 60 + now.minute
            # ±12分の窓に収まっているか確認（15分ループで確実に拾う）
            if not (-12 <= now_minutes - trig_minutes <= 12):
                continue

            key = f"habit_trigger:{h_id}:{today_str}:{trig_h:02d}{trig_m:02d}"
            if not await mark_alert_sent(key):
                continue

            streak_info = habit_cog._get_habit_stats(data, h_id, today_str)
            freq_str = "毎日" if freq == 1 else (f"週1回" if freq == 7 else f"{freq}日に1回")
            ctx = (
                f"【習慣トリガー発火】\n"
                f"習慣名: {h_name}\n"
                f"設定トリガー: {trigger}\n"
                f"頻度: {freq_str}\n"
                f"記録: {streak_info}"
            )
            await partner_cog.generate_and_send_routine_message(ctx, PROMPT_HABIT_TRIGGER)

    @proactive_check_task.before_loop
    @cleanup_task.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ProactiveAlertCog(bot))
