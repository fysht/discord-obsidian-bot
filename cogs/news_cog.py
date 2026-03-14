import os
import logging
import datetime
import asyncio

import discord
from discord.ext import commands, tasks

from config import JST
from prompts import PROMPT_ROUTINE_MORNING
from services.info_service import InfoService  # ★修正: services. を追加

class NewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        
        self.calendar_service = getattr(bot, 'calendar_service', None)
        self.tasks_service = getattr(bot, 'tasks_service', None)
        self.gemini_client = bot.gemini_client
        self.info_service = getattr(bot, 'info_service', InfoService())

    @tasks.loop(time=datetime.time(hour=6, minute=30, tzinfo=JST))
    async def morning_routine(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        if not channel: return

        try:
            weather_task = asyncio.create_task(self.info_service.get_weather())
            news_task = asyncio.create_task(self.info_service.get_news(limit=3))

            weather_data = await weather_task
            weather_text = weather_data[0] 
            
            news_list = await news_task
            news_text = "\n".join([f"・{news}" for news in news_list]) if news_list else "ニュースの取得に失敗しました。"

            today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            schedule_text = await self.calendar_service.list_events_for_date(today_str) if self.calendar_service else "カレンダーに接続できません。"
            
            tasks_text = await self.tasks_service.get_uncompleted_tasks() if self.tasks_service else "タスクAPIに接続できません。"

            recent_log = await partner_cog.fetch_todays_chat_log(channel) if channel else ""

            context_data = (
                f"【今日の予定】\n{schedule_text}\n\n"
                f"【現在の未完了タスク】\n{tasks_text}\n\n"
                f"【今日の天気 ({self.location_name})】\n{weather_text}\n\n"
                f"【今日の主要ニュース】\n{news_text}\n\n"
                f"【最近の会話ログ】\n{recent_log}"
            )
            
            await partner_cog.generate_and_send_routine_message(context_data, PROMPT_ROUTINE_MORNING)

        except Exception as e: 
            logging.error(f"Morning Routine Error: {e}")

    @morning_routine.before_loop
    async def before_morning_routine(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.morning_routine.is_running(): self.morning_routine.start()

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))