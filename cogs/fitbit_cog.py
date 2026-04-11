import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import datetime
import asyncio
import random

from config import JST
from prompts import (
    PROMPT_FITBIT_MORNING,
    PROMPT_FITBIT_MORNING_NO_DATA,
    PROMPT_FITBIT_EVENING,
)
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
                user_id=os.getenv("FITBIT_USER_ID", "-"),
            )
            self.is_ready = True
        else:
            self.is_ready = False
            logging.error("FitbitCog: Driveサービスが初期化されていません。")

        self.sleep_report_loop.start()
        self.full_health_report_loop.start()

    def cog_unload(self):
        self.sleep_report_loop.cancel()
        self.full_health_report_loop.cancel()

    def _format_minutes(self, total_minutes: int) -> str:
        if not total_minutes:
            return "0分"
        hours, mins = divmod(total_minutes, 60)
        if hours > 0:
            return f"{hours}時間{mins}分"
        return f"{mins}分"

    async def send_sleep_report(self, date_str: str = None):
        if not self.is_ready:
            return

        if date_str:
            try:
                target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                target_date = datetime.datetime.now(JST).date()
        else:
            target_date = datetime.datetime.now(JST).date()

        stats = await self.fitbit_service.get_stats(target_date)

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        today_log = "（会話ログなし）"
        if channel:
            today_log = await partner_cog.fetch_todays_chat_log(channel)

        date_display = (
            f"{target_date.strftime('%Y年%m月%d日')}の" if date_str else "今日の"
        )

        if not stats or "sleep_score" not in stats:
            context_data = f"{date_display}睡眠データ：まだ同期されていないか、データがありません。\n【最近の会話ログ】\n{today_log}"
            instruction = PROMPT_FITBIT_MORNING_NO_DATA
        else:
            sleep_score = stats.get("sleep_score", 0)
            sleep_time = self._format_minutes(stats.get("total_sleep_minutes", 0))
            context_data = f"【{date_display}睡眠データ】\nスコア: {sleep_score}\n合計睡眠時間: {sleep_time}\n【最近の会話ログ】\n{today_log}"
            instruction = PROMPT_FITBIT_MORNING

        await partner_cog.generate_and_send_routine_message(context_data, instruction)

    async def send_full_health_report(self, date_str: str = None):
        if not self.is_ready:
            return

        if date_str:
            try:
                target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                target_date = datetime.datetime.now(JST).date()
        else:
            target_date = datetime.datetime.now(JST).date()

        stats = await self.fitbit_service.get_stats(target_date)

        if not stats:
            stats = {}

        await self.fitbit_service.update_daily_note_with_stats(target_date, stats)

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        sleep_score = stats.get("sleep_score", "N/A")
        sleep_time = (
            self._format_minutes(stats.get("total_sleep_minutes", 0))
            if stats.get("total_sleep_minutes")
            else "N/A"
        )
        steps = stats.get("steps", "N/A")
        calories = stats.get("calories_out", "N/A")

        sleep_text = f"スコア: {sleep_score}, 睡眠時間: {sleep_time}"
        activity_text = f"歩数: {steps}歩, 消費: {calories}kcal"

        date_display = (
            f"{target_date.strftime('%Y年%m月%d日')}の" if date_str else "本日の"
        )
        context_data = f"【{date_display}睡眠】\n{sleep_text}\n【{date_display}活動】\n{activity_text}"

        await partner_cog.generate_and_send_routine_message(
            context_data, PROMPT_FITBIT_EVENING
        )

    # ==========================================
    # ★新規追加: 裏側で一括同期して報告する処理
    # ==========================================
    async def perform_batch_sync_and_notify(
        self, days: int, channel: discord.TextChannel
    ):
        if not self.is_ready:
            return
        if days < 1:
            days = 1
        if days > 30:
            days = 30  # 最大30日に制限

        today = datetime.datetime.now(JST).date()
        success_dates = []
        error_dates = []

        # 過去から順番にデータを取りに行く
        for i in range(days, 0, -1):
            target_date = today - datetime.timedelta(days=i)
            date_str = target_date.strftime("%Y-%m-%d")

            try:
                stats = await self.fitbit_service.get_stats(target_date)
                if not stats:
                    stats = {}
                success = await self.fitbit_service.update_daily_note_with_stats(
                    target_date, stats
                )

                if success:
                    success_dates.append(date_str)
                else:
                    error_dates.append(date_str)

                # API制限を回避するため1秒休む
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Fitbit Batch Sync Error for {date_str}: {e}")
                error_dates.append(date_str)

        # 取得が終わったら、パートナーAIに報告用メッセージを作らせる
        partner_cog = self.bot.get_cog("PartnerCog")
        if partner_cog and channel:
            result_msg = f"要求された日数: 過去{days}日分\n"
            if success_dates:
                result_msg += f"成功した期間: {success_dates[0]} 〜 {success_dates[-1]} ({len(success_dates)}日分)\n"
            if error_dates:
                result_msg += f"失敗した日付: {', '.join(error_dates)}\n"

            context_data = f"【過去データの一括同期 完了レポート】\n{result_msg}"
            instruction = f"Fitbitの過去データ（{days}日分）の一括同期作業が裏側で完了したことを、ユーザーにLINEのように明るく報告してください。「終わったよ！」という報告と、何日分成功したかを簡潔に伝えてください。質問などは不要です。"

            await partner_cog.generate_and_send_routine_message(
                context_data, instruction
            )

    # --- 自動タスク（毎日の定期実行） ---
    @tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=JST))
    async def sleep_report_loop(self):
        # 人間らしさのため0〜10分のランダム遅延
        await asyncio.sleep(random.randint(0, 600))
        await self.send_sleep_report()

    @tasks.loop(time=datetime.time(hour=22, minute=15, tzinfo=JST))
    async def full_health_report_loop(self):
        # 人間らしさのため0〜10分のランダム遅延
        await asyncio.sleep(random.randint(0, 600))
        await self.send_full_health_report()

    # --- 手動コマンド（念のための個別取得用） ---
    @app_commands.command(
        name="fitbit_morning",
        description="今日の睡眠レポートを手動で取得し、パートナーに報告させます。",
    )
    async def get_morning_report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.send_sleep_report()
        await interaction.followup.send("✅ 朝の睡眠レポート処理を手動で実行しました！")

    @app_commands.command(
        name="fitbit_evening",
        description="今日の健康総合レポートを手動で取得し、パートナーに報告させます。",
    )
    async def get_evening_report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.send_full_health_report()
        await interaction.followup.send(
            "✅ 夜の健康総合レポート処理を手動で実行しました！"
        )

    @sleep_report_loop.before_loop
    @full_health_report_loop.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))
