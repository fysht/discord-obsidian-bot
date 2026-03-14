import os
import discord
from discord.ext import commands, tasks
from google.genai import types
import logging
import datetime
from datetime import timedelta

from config import JST
from prompts import PROMPT_ROUTINE_INACTIVITY, PROMPT_ROUTINE_NIGHTLY, PROMPT_WEEKEND_STOCK_REVIEW, PROMPT_HABIT_CHECK

class PartnerRoutineCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.inactivity_check_task.is_running(): self.inactivity_check_task.start()
        if not self.nightly_reflection_task.is_running(): self.nightly_reflection_task.start()
        if not self.weekend_stock_review_task.is_running(): self.weekend_stock_review_task.start()
        if not self.habit_check_task.is_running(): self.habit_check_task.start()

    def cog_unload(self):
        self.inactivity_check_task.cancel()
        self.nightly_reflection_task.cancel()
        self.weekend_stock_review_task.cancel()
        self.habit_check_task.cancel()

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
                    model="gemini-2.5-pro", contents=prompt
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

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerRoutineCog(bot))