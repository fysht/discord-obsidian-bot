import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import datetime

from config import JST
from prompts import PROMPT_FITBIT_MORNING, PROMPT_FITBIT_MORNING_NO_DATA, PROMPT_FITBIT_EVENING
from services.fitbit_service import FitbitService

class FitbitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_service = bot.drive_service
        
        if self.drive_service:
            self.fitbit_service = FitbitService(
                drive_service=self.drive_service,
                client_id=os.getenv("FITBIT_CLIENT_ID"),
                client_secret=os.getenv("FITBIT_CLIENT_SECRET"),
                initial_refresh_token=os.getenv("FITBIT_REFRESH_TOKEN", ""),
                user_id=os.getenv("FITBIT_USER_ID", "-")
            )
            self.is_ready = True
        else:
            self.is_ready = False
            logging.error("FitbitCog: Driveサービスが初期化されていません。")

    def _format_minutes(self, total_minutes: int) -> str:
        if not total_minutes: return "0分"
        hours, mins = divmod(total_minutes, 60)
        if hours > 0: return f"{hours}時間{mins}分"
        return f"{mins}分"

    @tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=JST))
    async def sleep_report(self):
        if not self.is_ready: return
        target_date = datetime.datetime.now(JST).date()
        
        # ★修正: 新しい fitbit_service の get_stats() を使用
        stats = await self.fitbit_service.get_stats(target_date)
        
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        today_log = "（会話ログなし）"
        if channel:
            today_log = await partner_cog.fetch_todays_chat_log(channel)

        if not stats or 'sleep_score' not in stats:
            context_data = f"今日の睡眠データ：まだ同期されていません\n【最近の会話ログ】\n{today_log}"
            instruction = PROMPT_FITBIT_MORNING_NO_DATA
        else:
            sleep_score = stats.get('sleep_score', 0)
            sleep_time = self._format_minutes(stats.get('total_sleep_minutes', 0))
            context_data = f"【昨晩の睡眠データ】\nスコア: {sleep_score}\n合計睡眠時間: {sleep_time}\n【最近の会話ログ】\n{today_log}"
            instruction = PROMPT_FITBIT_MORNING
        
        await partner_cog.generate_and_send_routine_message(context_data, instruction)

    @tasks.loop(time=datetime.time(hour=22, minute=15, tzinfo=JST))
    async def full_health_report(self):
        if not self.is_ready: return
        target_date = datetime.datetime.now(JST).date()
        
        # ★修正: 新しい fitbit_service の get_stats() を使用
        stats = await self.fitbit_service.get_stats(target_date)
        
        if not stats:
            stats = {} # 取得失敗時も空の辞書で処理を続行
            
        # ★修正: 取得した stats 辞書をそのまま update_daily_note_with_stats に渡す
        await self.fitbit_service.update_daily_note_with_stats(target_date, stats)
        
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return
        
        sleep_score = stats.get('sleep_score', 'N/A')
        sleep_time = self._format_minutes(stats.get('total_sleep_minutes', 0)) if stats.get('total_sleep_minutes') else 'N/A'
        steps = stats.get('steps', 'N/A')
        calories = stats.get('calories_out', 'N/A')
        
        sleep_text = f"スコア: {sleep_score}, 睡眠時間: {sleep_time}"
        activity_text = f"歩数: {steps}歩, 消費: {calories}kcal"
        
        context_data = f"【本日の睡眠】\n{sleep_text}\n【本日の活動】\n{activity_text}"
        
        await partner_cog.generate_and_send_routine_message(context_data, PROMPT_FITBIT_EVENING)

    @app_commands.command(name="fitbit_morning", description="今日の睡眠レポートを手動で取得し、パートナーに報告させます。")
    async def get_morning_report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.sleep_report()
        await interaction.followup.send("✅ 朝の睡眠レポート処理を手動で実行しました！")

    @app_commands.command(name="fitbit_evening", description="今日の健康総合レポートを手動で取得し、パートナーに報告させます。")
    async def get_evening_report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.full_health_report()
        await interaction.followup.send("✅ 夜の健康総合レポート処理を手動で実行しました！")

    @sleep_report.before_loop
    @full_health_report.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))