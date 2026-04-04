import os
import discord
from discord.ext import commands, tasks
from google.genai import types
import logging
import datetime
from datetime import timedelta
import random

from config import JST
from prompts import PROMPT_ROUTINE_INACTIVITY, PROMPT_ROUTINE_NIGHTLY, PROMPT_WEEKEND_STOCK_REVIEW, PROMPT_HABIT_CHECK, PROMPT_ROUTINE_MORNING, PROMPT_SPONTANEOUS_CHAT
from services.info_service import InfoService

class PartnerRoutineCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.gemini_client = bot.gemini_client
        
        self.tasks_service = getattr(bot, 'tasks_service', None)
        # ★ 追加: カレンダーサービスを取得できるように設定
        self.calendar_service = getattr(bot, 'calendar_service', None)
        self.info_service = getattr(bot, 'info_service', InfoService())

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.inactivity_check_task.is_running(): self.inactivity_check_task.start()
        if not self.nightly_reflection_task.is_running(): self.nightly_reflection_task.start()
        if not self.weekend_stock_review_task.is_running(): self.weekend_stock_review_task.start()
        if not self.habit_check_task.is_running(): self.habit_check_task.start()
        if not self.morning_routine_task.is_running(): self.morning_routine_task.start()
        
        if not self.spontaneous_message_task.is_running(): self.spontaneous_message_task.start()
        if not self.update_manual_task.is_running(): self.update_manual_task.start()

    def cog_unload(self):
        self.inactivity_check_task.cancel()
        self.nightly_reflection_task.cancel()
        self.weekend_stock_review_task.cancel()
        self.habit_check_task.cancel()
        self.morning_routine_task.cancel()
        self.spontaneous_message_task.cancel()
        self.update_manual_task.cancel()

    # ==========================================
    # 毎晩23:45に「取扱説明書」を自動更新
    # ==========================================
    @tasks.loop(time=datetime.time(hour=23, minute=45, tzinfo=JST))
    async def update_manual_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        channel = self.bot.get_channel(self.memo_channel_id)
        if not partner_cog or not channel: return

        # 今日の会話ログを取得
        today_log = await partner_cog.fetch_todays_chat_log(channel)
        if not today_log.strip(): return # 会話がなければスキップ

        # 現在の取扱説明書を取得
        current_manual = await partner_cog._get_user_manual()
        
        from prompts import PROMPT_UPDATE_MANUAL
        prompt = PROMPT_UPDATE_MANUAL.format(current_manual=current_manual, chat_log=today_log)
        
        try:
            logging.info("【UserManual】取扱説明書の自動更新を開始します...")
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )
            new_manual = response.text.strip()
            
            # Google Driveの .bot フォルダ（隠しフォルダ）に保存
            service = partner_cog.drive_service.get_service()
            folder_id = await partner_cog.drive_service.find_file(service, partner_cog.drive_folder_id, ".bot")
            if not folder_id:
                folder_id = await partner_cog.drive_service.create_folder(service, partner_cog.drive_folder_id, ".bot")
            
            file_id = await partner_cog.drive_service.find_file(service, folder_id, "UserManual.md")
            if file_id:
                await partner_cog.drive_service.update_text(service, file_id, new_manual)
            else:
                await partner_cog.drive_service.upload_text(service, folder_id, "UserManual.md", new_manual)
            
            # パートナーの脳内キャッシュを最新化
            partner_cog.user_manual_cache = new_manual
            partner_cog.last_manual_fetch = datetime.datetime.now()
            logging.info("【UserManual】取扱説明書の更新と保存が完了しました！")
            
        except Exception as e:
            logging.error(f"Manual Update Error: {e}")

    # ==========================================
    # 記憶を使った「完全な気まぐれ発言」
    # ==========================================
    @tasks.loop(hours=1)
    async def spontaneous_message_task(self):
        # 9時から22時の間のみ（深夜・早朝は避ける）
        now = datetime.datetime.now(JST)
        if not (9 <= now.hour <= 22): return
        
        # 7%の確率で発動（1日に1回あるかないか程度）
        if random.random() > 0.07: return

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        # もし直近1時間以内に会話していたら、ウザくならないようにスキップ
        if hasattr(partner_cog, 'last_interaction'):
            if (datetime.datetime.now(JST) - partner_cog.last_interaction).total_seconds() < 3600:
                return
        
        # コア・メモリーを取得してプロンプトを作成
        manual = await partner_cog._get_user_manual()
        prompt = PROMPT_SPONTANEOUS_CHAT.format(user_manual=manual)
        
        # 定期メッセージ関数を使って送信
        await partner_cog.generate_and_send_routine_message("（気まぐれな雑談メッセージを生成してください）", prompt)

    @tasks.loop(time=datetime.time(hour=7, minute=0, tzinfo=JST))
    async def morning_routine_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        # ★ 追加: 今日のカレンダーの予定を取得
        schedule_text = "（予定を取得できませんでした）"
        if self.calendar_service:
            today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            schedule_text = await self.calendar_service.list_events_for_date(today_str)

        tasks_text = "（タスク情報を取得できませんでした）"
        if self.tasks_service:
            tasks_text = await self.tasks_service.get_uncompleted_tasks()

        weather, max_t, min_t = "取得失敗", "N/A", "N/A"
        news_list = []
        if self.info_service:
            weather, max_t, min_t = await self.info_service.get_weather()
            news_list = await self.info_service.get_news(limit=3)

        news_text = "\n".join(news_list) if news_list else "（ニュースを取得できませんでした）"

        # ★ 変更: context_data に【今日の予定】を結合
        context_data = f"【今日の予定】\n{schedule_text}\n\n【未完了のタスク】\n{tasks_text}\n\n【今日の天気】\n{weather}\n\n【ニュース】\n{news_text}"
        await partner_cog.generate_and_send_routine_message(context_data, PROMPT_ROUTINE_MORNING)

    @tasks.loop(minutes=15)
    async def inactivity_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog or not hasattr(partner_cog, 'last_interaction'): return

        now = datetime.datetime.now(JST)
        diff = now - partner_cog.last_interaction
        
        if diff > timedelta(hours=6) and 9 <= now.hour <= 21:
            context_data = "ユーザーは数時間何も発言していません。"
            await partner_cog.generate_and_send_routine_message(context_data, PROMPT_ROUTINE_INACTIVITY)
            partner_cog.last_interaction = now

    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def nightly_reflection_task(self):
        channel = self.bot.get_channel(self.memo_channel_id)
        partner_cog = self.bot.get_cog("PartnerCog")
        if not channel or not partner_cog: return

        today_log = await partner_cog.fetch_todays_chat_log(channel)
        
        prompt = f"{PROMPT_ROUTINE_NIGHTLY}\n\n【今日の会話ログ】\n{today_log if today_log.strip() else '今日は特に会話がありませんでした。'}"
        
        try:
            if self.gemini_client:
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt
                )
                await channel.send(response.text.strip())
        except Exception as e: 
            logging.error(f"Nightly Reflection Error: {e}")

    @tasks.loop(time=datetime.time(hour=20, minute=0, tzinfo=JST))
    async def weekend_stock_review_task(self):
        if datetime.datetime.now(JST).weekday() != 4:
            return

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        context_data = "今週の株式市場が閉まりました。週末です。"
        await partner_cog.generate_and_send_routine_message(context_data, PROMPT_WEEKEND_STOCK_REVIEW)

    @tasks.loop(time=datetime.time(hour=21, minute=0, tzinfo=JST))
    async def habit_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        habit_cog = self.bot.get_cog("HabitCog")
        if not partner_cog or not habit_cog: return

        incomplete_habits = await habit_cog.get_incomplete_habits()
        
        if incomplete_habits:
            context_data = "【今日の未完了の習慣】\n" + "\n".join([f"- {h}" for h in incomplete_habits])
        else:
            context_data = "今日の習慣はすべて完了しています！"
            
        await partner_cog.generate_and_send_routine_message(context_data, PROMPT_HABIT_CHECK)

    @spontaneous_message_task.before_loop
    @update_manual_task.before_loop
    async def before_new_tasks(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerRoutineCog(bot))