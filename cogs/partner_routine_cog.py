import os
import discord
from discord.ext import commands, tasks
import google.generativeai as genai
import logging
import datetime
from datetime import timedelta
import zoneinfo

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class PartnerRoutineCog(commands.Cog):
    """パートナーAIの自発的なアクション（リマインダー、放置検知、夜の振り返り）を担当するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        
        # 独立してGeminiを初期化
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.reminder_check_task.is_running():
            self.reminder_check_task.start()
        if not self.inactivity_check_task.is_running():
            self.inactivity_check_task.start()
        if not self.nightly_reflection_task.is_running():
            self.nightly_reflection_task.start()
        logging.info("PartnerRoutineCog: ルーティンタスクを開始しました。")

    def cog_unload(self):
        self.reminder_check_task.cancel()
        self.inactivity_check_task.cancel()
        self.nightly_reflection_task.cancel()

    # --- タスク1: リマインダー監視 (1分ごと) ---
    @tasks.loop(minutes=1)
    async def reminder_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        now = datetime.datetime.now(JST)
        remaining = []
        changed = False

        for rem in partner_cog.reminders:
            target = datetime.datetime.fromisoformat(rem['time'])
            if now >= target:
                channel = self.bot.get_channel(self.memo_channel_id)
                if channel:
                    user_mention = self.bot.get_user(rem['user_id']).mention if self.bot.get_user(rem['user_id']) else ''
                    await channel.send(f"{user_mention} ⏰ 時間だよ！\n**{rem.get('content')}** ({target.strftime('%H:%M')})")
                changed = True
            else:
                remaining.append(rem)
        
        # 状態の更新と保存依頼
        partner_cog.reminders = remaining
        if changed:
            await partner_cog.save_data_to_drive()

    # --- タスク2: 放置検知 (60分ごと) ---
    @tasks.loop(minutes=60)
    async def inactivity_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        now = datetime.datetime.now(JST)
        # 12時間経過 ＆ 深夜1時〜朝6時の睡眠時間帯は避ける
        if (now - partner_cog.last_interaction) > timedelta(hours=12) and not (1 <= now.hour <= 6):
            channel = self.bot.get_channel(self.memo_channel_id)
            if not channel: return
            
            async for m in channel.history(limit=1):
                if m.author.id == self.bot.user.id: return # 直近がBot発言ならスキップ
            
            prompt = "あなたは私を日々サポートする、20代女性の親しいパートナーです。温かみのあるタメ口で話してください。\n私から12時間以上連絡がありません。心配して軽く声をかけるような短いメッセージをDiscordに送ってください。"
            try:
                response = await self.gemini_model.generate_content_async(prompt)
                await channel.send(response.text.strip())
                # 最終やり取り時間を更新して保存
                partner_cog.last_interaction = now
                await partner_cog.save_data_to_drive()
            except Exception as e:
                logging.error(f"PartnerRoutine: 放置検知エラー: {e}")

    # --- タスク3: 夜の振り返り (毎日22:00) ---
    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def nightly_reflection_task(self):
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel: return
        
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        # 今日のログをPartnerCogの機能を使って取得
        today_log = await partner_cog.fetch_todays_chat_log(channel)
        if not today_log.strip(): return
        
        prompt = f"あなたは私を日々サポートする、20代女性の親しいパートナーです。温かみのあるタメ口で話してください。\n現在22時です。以下の「今日の会話ログ全体」を踏まえて、今日1日を振り返るような労いの言葉や簡単な質問を送信してください。\n【今日のログ】\n{today_log}"
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            await channel.send(response.text.strip())
        except Exception as e:
            logging.error(f"PartnerRoutine: 夜の振り返りエラー: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerRoutineCog(bot))