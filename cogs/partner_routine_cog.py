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
    PROMPT_ROUTINE_NIGHTLY,
    PROMPT_WEEKEND_STOCK_REVIEW,
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
        # スケジュール対象タスクの最終実行日（同日重複防止用）
        self._last_run_dates: dict[str, "datetime.date"] = {}
        # DB バックアップの重複アップロード抑止用（前回バックアップ時の DB mtime）
        self._last_db_backup_mtime: float = 0.0

        for task in [
            self.nightly_reflection_task,
            self.weekend_stock_review_task,
            self.habit_check_task,
            self.morning_routine_task,
            self.update_manual_task,
            self.tomorrow_plan_task,
            self.obsidian_review_task,
            self.db_backup_task,
            self.lunch_meal_check_task,
            self.afternoon_check_task,
            self.dinner_meal_check_task,
            self.evening_mood_check_task,
            self.gratitude_check_task,
            self.learning_check_task,
            self.english_quiz_task,
        ]:
            task.start()

    def cog_unload(self):
        for task in [
            self.nightly_reflection_task,
            self.weekend_stock_review_task,
            self.habit_check_task,
            self.morning_routine_task,
            self.update_manual_task,
            self.tomorrow_plan_task,
            self.obsidian_review_task,
            self.db_backup_task,
            self.lunch_meal_check_task,
            self.afternoon_check_task,
            self.dinner_meal_check_task,
            self.evening_mood_check_task,
            self.gratitude_check_task,
            self.learning_check_task,
            self.english_quiz_task,
        ]:
            task.cancel()

    # ==========================================
    # 毎晩23:45に「取扱説明書」を自動更新
    # ==========================================
    @tasks.loop(minutes=1)
    async def update_manual_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("update_manual", "23:45", "daily", self._last_run_dates.get("update_manual"))
        if not due:
            return
        self._last_run_dates["update_manual"] = today
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
    # 朝のルーティン（7:00）
    # ==========================================
    @tasks.loop(minutes=1)
    async def morning_routine_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("morning_routine", "07:00", "daily", self._last_run_dates.get("morning_routine"))
        if not due:
            return
        self._last_run_dates["morning_routine"] = today
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

        # get_news() は {"title", "link"} の dict リストを返す。
        # 通知カードでは markdown リンクにして「リンクをそのままクリック」できるようにする。
        if news_list:
            _news_lines = []
            for n in news_list:
                if isinstance(n, dict):
                    title = n.get("title", "")
                    link = n.get("link", "")
                    _news_lines.append(f"- [{title}]({link})" if link else f"- {title}")
                else:
                    _news_lines.append(f"- {n}")
            news_text = "\n".join(_news_lines)
        else:
            news_text = ""

        # 過去の今日（1年前 / 3ヶ月前）
        past_journals = ""
        try:
            past_journals = await partner_cog._get_past_daily_journals()
        except Exception as e:
            logging.debug(f"past journals fetch error: {e}")

        # 「複数項目を1メッセージに束ねる」のをやめ、項目ごとに個別の通知カード
        # （既読チェック可能）として送り、プッシュはまとめて1発だけ。
        notices = [
            {"category": "weather", "title": "☀️ 今日の天気",
             "body": f"{weather}\n最高 {max_t}℃ / 最低 {min_t}℃"},
            {"category": "schedule", "title": "📅 今日の予定", "body": schedule_text},
            {"category": "tasks", "title": "✅ 未完了タスク", "body": tasks_text},
            {"category": "news", "title": "📰 今日のニュース", "body": news_text},
        ]
        if past_journals and past_journals.strip():
            notices.append(
                {"category": "past", "title": "🕰 過去の今日", "body": past_journals}
            )

        from api.notification_service import send_notice_batch
        await send_notice_batch(notices, "朝のお知らせ")

        # 朝食のログ質問を投下（回答欄＋履歴チップで1タップ記録）。
        # Push は朝のお知らせカード（上の send_notice_batch）の1発にまとめるため、ここでは出さない。
        try:
            await partner_cog.send_log_question("meal", "朝ごはんは何を食べた？", push=False)
        except Exception as e:
            logging.debug(f"morning meal question error: {e}")

    # ==========================================
    # 昼食ログの質問（13:00）— 昼ごはんを記録
    # ==========================================
    @tasks.loop(minutes=1)
    async def lunch_meal_check_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("lunch_meal", "13:00", "daily", self._last_run_dates.get("lunch_meal"))
        if not due:
            return
        self._last_run_dates["lunch_meal"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        try:
            await partner_cog.send_log_question("meal", "昼ごはんは何を食べた？")
        except Exception as e:
            logging.debug(f"lunch meal question error: {e}")

    # ==========================================
    # 昼の振り返り（14:30）— 午後の調子を1タップで記録
    # ==========================================
    @tasks.loop(minutes=1)
    async def afternoon_check_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("afternoon_check", "14:30", "daily", self._last_run_dates.get("afternoon_check"))
        if not due:
            return
        self._last_run_dates["afternoon_check"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        try:
            await partner_cog.send_log_question("afternoon", "午前はどうだった？午後の調子は？")
        except Exception as e:
            logging.debug(f"afternoon question error: {e}")

    # ==========================================
    # 夕食ログの質問（19:00）— 夕ごはんを記録
    # ==========================================
    @tasks.loop(minutes=1)
    async def dinner_meal_check_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("dinner_meal", "19:00", "daily", self._last_run_dates.get("dinner_meal"))
        if not due:
            return
        self._last_run_dates["dinner_meal"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        try:
            await partner_cog.send_log_question("meal", "晩ごはんは何を食べた？")
        except Exception as e:
            logging.debug(f"dinner meal question error: {e}")

    # ==========================================
    # 夜の気分チェック（21:00）— 1タップで気分をログに残す
    # ==========================================
    @tasks.loop(minutes=1)
    async def evening_mood_check_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("evening_mood", "21:00", "daily", self._last_run_dates.get("evening_mood"))
        if not due:
            return
        self._last_run_dates["evening_mood"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        try:
            await partner_cog.send_log_question("mood", "今日の気分はどうだった？")
        except Exception as e:
            logging.debug(f"evening mood question error: {e}")

    # ==========================================
    # 良かったこと・感謝（20:45）— 今日のポジティブを1つ残す
    # ==========================================
    @tasks.loop(minutes=1)
    async def gratitude_check_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("gratitude_check", "20:45", "daily", self._last_run_dates.get("gratitude_check"))
        if not due:
            return
        self._last_run_dates["gratitude_check"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        try:
            await partner_cog.send_log_question("gratitude", "今日良かったこと・感謝したいことは？")
        except Exception as e:
            logging.debug(f"gratitude question error: {e}")

    # ==========================================
    # 今日の学び・気づき（21:15）— インプットや発見を1行残す
    # ==========================================
    @tasks.loop(minutes=1)
    async def learning_check_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("learning_check", "21:15", "daily", self._last_run_dates.get("learning_check"))
        if not due:
            return
        self._last_run_dates["learning_check"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        try:
            await partner_cog.send_log_question("learning", "今日学んだこと・気づいたことは？")
        except Exception as e:
            logging.debug(f"learning question error: {e}")

    # ==========================================
    # 英語クイズ（火・木・土の19:30）— 週3回、選択肢チップで気軽に学習
    # ==========================================
    @tasks.loop(minutes=1)
    async def english_quiz_task(self):
        from services.schedule_resolver import is_due
        # 週3回（火=1・木=3・土=5）に限定して通知過多を避ける
        if datetime.datetime.now(JST).weekday() not in (1, 3, 5):
            return
        due, today = await is_due("english_quiz", "19:30", "daily", self._last_run_dates.get("english_quiz"))
        if not due:
            return
        self._last_run_dates["english_quiz"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        try:
            await partner_cog.send_english_quiz()
        except Exception as e:
            logging.debug(f"english quiz task error: {e}")

    # ==========================================
    # DB の定期バックアップ（2分ごと・変更時のみ）
    #   チャット・通知カード・質問など全ての DB 書き込みを Drive へ確実に退避する。
    #   起動毎の復元で古い Drive 版がローカルを潰し、メッセージが消える事故への対策。
    #   OOM 等の突然死でも損失は最大2分に収まる。mtime 比較で無駄なアップロードは省く。
    # ==========================================
    @tasks.loop(minutes=2)
    async def db_backup_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog or not getattr(partner_cog, "drive_service", None):
            return
        try:
            from api.database import DB_PATH, backup_db_to_drive
            mtime = DB_PATH.stat().st_mtime if DB_PATH.exists() else 0.0
            if mtime == self._last_db_backup_mtime:
                return  # 前回から変更なし → アップロードしない
            await backup_db_to_drive(partner_cog.drive_service, partner_cog.drive_folder_id)
            self._last_db_backup_mtime = mtime
        except Exception as e:
            logging.debug(f"db_backup_task error: {e}")

    # ==========================================
    # 無活動チェック（15分ごと）
    # ==========================================
    # ==========================================
    # 夜の振り返り（22:00）— DailySummaryCog に統合済みのため通常は早期 return
    # （明日の天気・予定・MIT 質問も DailySummaryCog のメッセージに含まれる）
    # ==========================================
    @tasks.loop(minutes=1)
    async def nightly_reflection_task(self):
        # 統合により無効化。互換性のため関数自体は残す。
        return
        # 以下の旧コードは保守の参考用にコメントアウトしてもよいが、
        # 重複通知を防ぐためここで return している。
        from services.schedule_resolver import is_due  # noqa: E402,F401  # type: ignore[unreachable]
        due, today = await is_due("evening_review", "22:00", "daily", self._last_run_dates.get("evening_review"))
        if not due:
            return
        self._last_run_dates["evening_review"] = today
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
                await save_message_and_notify("assistant", reply_text, proactive=True)
                # ユーザーの次の返信を「夜の振り返り回答」として捕捉するため、
                # 質問レコードを登録しておく（/chat で自動的に答えが紐付き、Obsidian へ保存される）
                try:
                    from api.database import add_daily_question, get_questions_by_date
                    _today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
                    _existing = await get_questions_by_date(_today_str, scope='nightly_reflection')
                    if not any(q.get('status') == 'pending' for q in _existing):
                        await add_daily_question(
                            _today_str,
                            "今日の振り返り（夜のメッセージへの返信）",
                            scope='nightly_reflection',
                        )
                except Exception as _qe:
                    logging.debug(f"register nightly_reflection question failed: {_qe}")
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
    @tasks.loop(minutes=1)
    async def weekend_stock_review_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("weekend_stocks", "20:15", "friday", self._last_run_dates.get("weekend_stocks"))
        if not due:
            return
        self._last_run_dates["weekend_stocks"] = today
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        await partner_cog.generate_and_send_routine_message(
            "今週の株式市場が閉まりました。週末です。", PROMPT_WEEKEND_STOCK_REVIEW
        )

    # ==========================================
    # 習慣チェック（21:00）
    # ==========================================
    @tasks.loop(minutes=1)
    async def habit_check_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("habit_check", "20:30", "daily", self._last_run_dates.get("habit_check"))
        if not due:
            return
        self._last_run_dates["habit_check"] = today
        await asyncio.sleep(random.randint(0, 600))
        partner_cog = self.bot.get_cog("PartnerCog")
        habit_cog = self.bot.get_cog("HabitCog")
        if not partner_cog or not habit_cog:
            return

        incomplete_habits = await habit_cog.get_incomplete_habits()

        # すべて完了している日は通知しない（ノイズを避ける）。
        # 未完了がある日だけ、個別カード（既読チェック可能）として知らせる。
        if not incomplete_habits:
            return

        body = "まだ完了していない習慣だよ。\n" + "\n".join(
            [f"- {h}" for h in incomplete_habits]
        )
        from api.notification_service import send_notice_batch
        await send_notice_batch(
            [{"category": "habit", "title": "🔁 未完了の習慣", "body": body}],
            "習慣チェック",
        )

    # ==========================================
    # 明日の予定（21:00）— 無効化
    #   22:00 のデイリーサマリー（DailySummaryCog._send_tomorrow_cards）が
    #   明日の天気・予定を個別カードで送るようになり、内容が重複するため停止。
    #   互換のため関数自体は残し、即 return する。
    # ==========================================
    @tasks.loop(minutes=1)
    async def tomorrow_plan_task(self):
        return  # 無効化（DailySummaryCog のカードに統合済み）
        from services.schedule_resolver import is_due  # noqa: E402,F401  # type: ignore[unreachable]
        due, today = await is_due("tomorrow_preview", "21:00", "daily", self._last_run_dates.get("tomorrow_preview"))
        if not due:
            return
        self._last_run_dates["tomorrow_preview"] = today
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
                f"{weather_data.get('summary', '不明')}\n"
                f"最高 {weather_data.get('max_temp','--')}℃ / "
                f"最低 {weather_data.get('min_temp','--')}℃"
            )

        # 明日の予定・天気も項目ごとに個別カード化し、プッシュはまとめて1発。
        notices = [
            {"category": "schedule", "title": "📅 明日の予定", "body": schedule_text},
            {"category": "weather", "title": "☀️ 明日の天気", "body": weather_text},
        ]
        from api.notification_service import send_notice_batch
        await send_notice_batch(notices, "明日のお知らせ")

    # ==========================================
    # Obsidianデイリーノートの振り返り（20:00）
    # ==========================================
    @tasks.loop(minutes=1)
    async def obsidian_review_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due("obsidian_review", "20:00", "daily", self._last_run_dates.get("obsidian_review"))
        if not due:
            return
        self._last_run_dates["obsidian_review"] = today
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

    @update_manual_task.before_loop
    @morning_routine_task.before_loop
    @nightly_reflection_task.before_loop
    @weekend_stock_review_task.before_loop
    @habit_check_task.before_loop
    @tomorrow_plan_task.before_loop
    @obsidian_review_task.before_loop
    @db_backup_task.before_loop
    @lunch_meal_check_task.before_loop
    @afternoon_check_task.before_loop
    @dinner_meal_check_task.before_loop
    @evening_mood_check_task.before_loop
    @gratitude_check_task.before_loop
    @learning_check_task.before_loop
    @english_quiz_task.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerRoutineCog(bot))
