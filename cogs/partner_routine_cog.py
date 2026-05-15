import os
import asyncio
import logging
import datetime
import random
from datetime import timedelta

from discord.ext import commands, tasks

from config import JST
from utils.async_utils import safe_create_task
from prompts import (
    PROMPT_ROUTINE_INACTIVITY,
    PROMPT_ROUTINE_NIGHTLY,
    PROMPT_WEEKEND_STOCK_REVIEW,
    PROMPT_HABIT_CHECK,
    PROMPT_ROUTINE_MORNING,
    PROMPT_SPONTANEOUS_CHAT,
    PROMPT_ROUTINE_TOMORROW,
    PROMPT_ROUTINE_DAILY_REVIEW,
)
from services.info_service import InfoService


class PartnerRoutineCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.gemini_client = bot.gemini_client
        self.tasks_service = getattr(bot, "tasks_service", None)
        self.calendar_service = getattr(bot, "calendar_service", None)
        self.info_service = getattr(bot, "info_service", InfoService())

        for task in [
            self.inactivity_check_task,
            self.nightly_reflection_task,
            self.weekend_stock_review_task,
            self.habit_check_task,
            self.morning_routine_task,
            self.spontaneous_message_task,
            self.update_manual_task,
            self.tomorrow_plan_task,
            self.obsidian_review_task,
        ]:
            task.start()

    def cog_unload(self):
        for task in [
            self.inactivity_check_task,
            self.nightly_reflection_task,
            self.weekend_stock_review_task,
            self.habit_check_task,
            self.morning_routine_task,
            self.spontaneous_message_task,
            self.update_manual_task,
            self.tomorrow_plan_task,
            self.obsidian_review_task,
        ]:
            task.cancel()

    # ==========================================
    # 毎晩23:45に「取扱説明書」を自動更新
    # ==========================================
    @tasks.loop(time=datetime.time(hour=23, minute=45, tzinfo=JST))
    async def update_manual_task(self):
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        today_log = await partner_cog.fetch_todays_chat_log()
        if not today_log.strip():
            return

        current_manual = await partner_cog._get_user_manual()

        from prompts import PROMPT_UPDATE_MANUAL
        prompt = PROMPT_UPDATE_MANUAL.format(
            current_manual=current_manual, chat_log=today_log
        )

        try:
            logging.info("【UserManual】取扱説明書の自動更新を開始します...")
            from services.gemini_model_resolver import resolve_gemini_model
            _m = await resolve_gemini_model("routines", default_pro=True)
            response = await self.gemini_client.aio.models.generate_content(
                model=_m, contents=prompt
            )
            new_manual = response.text.strip()

            service = partner_cog.drive_service.get_service()
            folder_id = await partner_cog.drive_service.find_file(
                service, partner_cog.drive_folder_id, ".bot"
            )
            if not folder_id:
                folder_id = await partner_cog.drive_service.create_folder(
                    service, partner_cog.drive_folder_id, ".bot"
                )

            file_id = await partner_cog.drive_service.find_file(
                service, folder_id, "UserManual.md"
            )
            if file_id:
                await partner_cog.drive_service.update_text(
                    service, file_id, new_manual
                )
            else:
                await partner_cog.drive_service.upload_text(
                    service, folder_id, "UserManual.md", new_manual
                )

            partner_cog.user_manual_cache = new_manual
            partner_cog.last_manual_fetch = datetime.datetime.now()
            logging.info("【UserManual】取扱説明書の更新と保存が完了しました！")
        except Exception as e:
            logging.error(f"Manual Update Error: {e}")

    # ==========================================
    # 気まぐれメッセージ（毎時20%の確率）
    # ==========================================
    @tasks.loop(hours=1)
    async def spontaneous_message_task(self):
        now = datetime.datetime.now(JST)
        if not (9 <= now.hour <= 22):
            return
        if random.random() > 0.20:
            return

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        if (
            datetime.datetime.now(JST) - partner_cog.last_interaction
        ).total_seconds() < 3600:
            return

        manual = await partner_cog._get_user_manual()
        prompt = PROMPT_SPONTANEOUS_CHAT.format(user_manual=manual)
        await partner_cog.generate_and_send_routine_message(
            "（気まぐれな雑談メッセージを生成してください）", prompt
        )

    # ==========================================
    # 朝のルーティン（7:00）
    # ==========================================
    @tasks.loop(time=datetime.time(hour=7, minute=0, tzinfo=JST))
    async def morning_routine_task(self):
        await asyncio.sleep(random.randint(0, 900))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        schedule_text = "（予定を取得できませんでした）"
        if self.calendar_service:
            today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            schedule_text = await self.calendar_service.list_events_for_date(today_str)

        tasks_text = "（タスク情報を取得できませんでした）"
        if self.tasks_service:
            tasks_text = await self.tasks_service.get_uncompleted_tasks()

        weather, max_t, min_t = "取得失敗", "N/A", "N/A"
        news_list = []
        if self.info_service:
            weather_data = await self.info_service.get_weather()
            weather = weather_data.get("summary", "取得失敗")
            max_t = weather_data.get("max_temp", "N/A")
            min_t = weather_data.get("min_temp", "N/A")
            news_list = await self.info_service.get_news(limit=3)

        news_text = (
            "\n".join(news_list) if news_list else "（ニュースを取得できませんでした）"
        )

        # 過去の今日（1年前 / 3ヶ月前）
        past_journals = ""
        try:
            past_journals = await partner_cog._get_past_daily_journals()
        except Exception as e:
            logging.debug(f"past journals fetch error: {e}")

        past_section = (
            f"\n【過去の今日】\n{past_journals}\n" if past_journals else ""
        )

        context_data = (
            f"【今日の予定】\n{schedule_text}\n\n"
            f"【未完了のタスク（タイムライン作成のベース）】\n{tasks_text}\n\n"
            f"【今日の天気】\n{weather} (最高{max_t}℃ / 最低{min_t}℃)\n\n"
            f"【ニュース】\n{news_text}"
            f"{past_section}"
        )
        await partner_cog.generate_and_send_routine_message(
            context_data, PROMPT_ROUTINE_MORNING
        )

    # ==========================================
    # 無活動チェック（15分ごと）
    # ==========================================
    @tasks.loop(minutes=15)
    async def inactivity_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        now = datetime.datetime.now(JST)
        diff = now - partner_cog.last_interaction

        if diff > timedelta(hours=6) and 9 <= now.hour <= 21:
            context_data = "ユーザーは数時間何も発言していません。"
            await partner_cog.generate_and_send_routine_message(
                context_data, PROMPT_ROUTINE_INACTIVITY
            )
            partner_cog.last_interaction = now

    # ==========================================
    # 夜の振り返り（22:00）
    # ==========================================
    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def nightly_reflection_task(self):
        await asyncio.sleep(random.randint(0, 900))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        today_log = await partner_cog.fetch_todays_chat_log()
        mit_section = ""
        try:
            mit_section = await partner_cog._get_mit_section()
        except Exception as e:
            logging.debug(f"MIT section fetch error: {e}")
        mit_block = f"\n\n【今日のMIT】\n{mit_section}" if mit_section else ""

        # 翌日の天気とカレンダーを取得
        tomorrow_str = (datetime.datetime.now(JST) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_weather = "（取得失敗）"
        tomorrow_schedule = "（取得失敗）"
        try:
            if self.info_service:
                wd = await self.info_service.get_weather()
                tomorrow_daily = next((d for d in (wd.get("daily") or []) if d.get("day") == "明日"), None)
                if tomorrow_daily:
                    tomorrow_weather = f"{tomorrow_daily.get('weather', '不明')} 最高{tomorrow_daily.get('max_temp','?')}℃ 最低{tomorrow_daily.get('min_temp','?')}℃"
                else:
                    tomorrow_weather = wd.get("summary", "取得失敗")
        except Exception as e:
            logging.debug(f"tomorrow weather fetch error: {e}")
        try:
            if self.calendar_service:
                tomorrow_schedule = await self.calendar_service.list_events_for_date(tomorrow_str)
        except Exception as e:
            logging.debug(f"tomorrow calendar fetch error: {e}")

        tomorrow_block = (
            f"\n\n【明日の天気】{tomorrow_weather}"
            f"\n【明日の予定】\n{tomorrow_schedule}"
            f"\n\n⚡ 明日のMIT（最重要タスク）を今夜のうちに3つ考えて、チャットで「明日のMIT: 1. xxx 2. xxx 3. xxx」と教えてほしい。set_mitツールで登録するよ！"
        )

        prompt = (
            f"{PROMPT_ROUTINE_NIGHTLY}{mit_block}\n\n【今日の会話ログ】\n"
            f"{today_log if today_log.strip() else '今日は特に会話がありませんでした。'}"
            f"{tomorrow_block}"
        )

        try:
            if self.gemini_client:
                from services.gemini_model_resolver import resolve_gemini_model
                _m = await resolve_gemini_model("routines", default_pro=True)
                response = await self.gemini_client.aio.models.generate_content(
                    model=_m, contents=prompt
                )
                from api.database import backup_db_to_drive
                from api.notification_service import save_message_and_notify

                reply_text = response.text.strip()
                await save_message_and_notify("assistant", reply_text)
                if partner_cog.drive_service:
                    safe_create_task(
                        backup_db_to_drive(
                            partner_cog.drive_service, partner_cog.drive_folder_id
                        ),
                        name="db-backup-routine",
                    )
        except Exception as e:
            logging.error(f"Nightly Reflection Error: {e}")

    # ==========================================
    # 週末の株レビュー（金曜20:00）
    # ==========================================
    @tasks.loop(time=datetime.time(hour=20, minute=0, tzinfo=JST))
    async def weekend_stock_review_task(self):
        if datetime.datetime.now(JST).weekday() != 4:
            return
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        await partner_cog.generate_and_send_routine_message(
            "今週の株式市場が閉まりました。週末です。", PROMPT_WEEKEND_STOCK_REVIEW
        )

    # ==========================================
    # 習慣チェック（21:00）
    # ==========================================
    @tasks.loop(time=datetime.time(hour=21, minute=0, tzinfo=JST))
    async def habit_check_task(self):
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        habit_cog = self.bot.get_cog("HabitCog")
        if not partner_cog or not habit_cog:
            return

        incomplete_habits = await habit_cog.get_incomplete_habits()

        if incomplete_habits:
            context_data = "【今日の未完了の習慣】\n" + "\n".join(
                [f"- {h}" for h in incomplete_habits]
            )
        else:
            context_data = "今日の習慣はすべて完了しています！"

        await partner_cog.generate_and_send_routine_message(
            context_data, PROMPT_HABIT_CHECK
        )

    # ==========================================
    # 明日の予定（21:00）
    # ==========================================
    @tasks.loop(time=datetime.time(hour=21, minute=0, tzinfo=JST))
    async def tomorrow_plan_task(self):
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        tomorrow = datetime.datetime.now(JST) + timedelta(days=1)
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")

        schedule_text = "（予定を取得できませんでした）"
        if self.calendar_service:
            schedule_text = await self.calendar_service.list_events_for_date(
                tomorrow_str
            )

        weather_text = "（天気情報を取得できませんでした）"
        if self.info_service:
            weather_data = await self.info_service.get_weather()
            weather_text = (
                f"{weather_data.get('summary', '不明')} "
                f"(最高{weather_data.get('max_temp','--')}℃ / "
                f"最低{weather_data.get('min_temp','--')}℃)"
            )

        context_data = f"【明日の予定】\n{schedule_text}\n\n【明日の天気】\n{weather_text}"
        await partner_cog.generate_and_send_routine_message(
            context_data, PROMPT_ROUTINE_TOMORROW
        )

    # ==========================================
    # Obsidianデイリーノートの振り返り（20:00）
    # ==========================================
    @tasks.loop(time=datetime.time(hour=20, minute=0, tzinfo=JST))
    async def obsidian_review_task(self):
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        daily_note = await partner_cog._get_todays_obsidian_note()
        if not daily_note or len(daily_note.strip()) < 50:
            return

        await partner_cog.generate_and_send_routine_message(
            daily_note, PROMPT_ROUTINE_DAILY_REVIEW
        )

    @spontaneous_message_task.before_loop
    @update_manual_task.before_loop
    @morning_routine_task.before_loop
    @inactivity_check_task.before_loop
    @nightly_reflection_task.before_loop
    @weekend_stock_review_task.before_loop
    @habit_check_task.before_loop
    @tomorrow_plan_task.before_loop
    @obsidian_review_task.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerRoutineCog(bot))
