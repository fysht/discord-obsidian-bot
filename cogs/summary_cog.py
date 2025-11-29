import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import zoneinfo
import asyncio
import sys
from pathlib import Path
import logging

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
DAILY_SUMMARY_TIME = datetime.time(hour=22, minute=30, tzinfo=JST)
WEEKLY_SUMMARY_TIME = datetime.time(hour=23, minute=0, tzinfo=JST) # 日曜 23:00
MONTHLY_SUMMARY_TIME = datetime.time(hour=23, minute=30, tzinfo=JST) # 月末 23:30

class SummaryCog(commands.Cog):
    """毎日定時に外部のサマリー生成ワーカーを呼び出すCog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 修正: 投稿先を JOURNAL_CHANNEL_ID に変更
        self.summary_channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.worker_path = str(Path(__file__).resolve().parent.parent / "summary_worker.py")
        self.last_summary_date = None

    @commands.Cog.listener()
    async def on_ready(self):
        """Botの準備完了後にタスクを開始する"""
        if not self.daily_summary.is_running():
            self.daily_summary.start()
            logging.info("日次サマリー生成タスクを開始しました。")
        if not self.weekly_summary.is_running():
            self.weekly_summary.start()
            logging.info("週次サマリー生成タスクを開始しました。")
        if not self.monthly_summary.is_running():
            self.monthly_summary.start()
            logging.info("月次サマリー生成タスクを開始しました。")


    def cog_unload(self):
        self.daily_summary.cancel()
        self.weekly_summary.cancel()
        self.monthly_summary.cancel()

    async def run_summary_logic(self, period: str, target_date: datetime.date, interaction: discord.Interaction | None = None):
        """サマリー生成のメインロジック。期間と日付を指定して実行する"""
        
        sync_cog = self.bot.get_cog('SyncCog')
        if sync_cog:
            logging.info(f"【{period.capitalize()}サマリー】サマリー生成前に、保留中のメモを強制同期します...")
            await sync_cog.force_sync()
            logging.info(f"【{period.capitalize()}サマリー】同期処理の完了を待機しました。")
        else:
            logging.warning(f"【{period.capitalize()}サマリー】SyncCogが見つからなかったため、同期をスキップします。")
        
        # 修正: self.summary_channel_id を使用してチャンネルを取得
        channel = self.bot.get_channel(self.summary_channel_id)
        if not channel:
            if interaction:
                await interaction.followup.send("エラー: 対象のチャンネルが見つかりませんでした。")
            logging.error(f"【{period.capitalize()}サマリー】エラー: 対象のチャンネル（ID: {self.summary_channel_id}）が見つかりませんでした。")
            return
            
        logging.info(f"【{period.capitalize()}サマリー】{target_date} のサマリーを生成するため、外部ワーカーを呼び出します...")
        
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            proc = await asyncio.create_subprocess_exec(
                sys.executable, self.worker_path, period, str(target_date),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                result = stdout.decode('utf-8').strip()
                if not result:
                     message = f"📝 {target_date.strftime('%Y年%m月%d日')} のメモはありませんでした！(AIの応答が空)"
                elif "NO_MEMO" in result:
                    message = f"📝 {period.capitalize()}サマリーの対象となる期間のメモはありませんでした！"
                elif result.startswith("ERROR:"):
                    logging.error(f"【{period.capitalize()}サマリー】ワーカーでエラー発生: {result}")
                    message = f"🤖 AIによるサマリー生成中にエラーが発生しました。\n`{result}`"
                else:
                    embed = discord.Embed(
                        title=f"📝 {target_date.strftime('%Y年%m月%d日')}の{period.capitalize()}サマリー",
                        description=result,
                        color=discord.Color.from_rgb(112, 128, 144)
                    )
                    if interaction:
                        await interaction.followup.send(embed=embed)
                    else:
                        await channel.send(embed=embed)
                    return # 成功時はここで終了
            else:
                error_msg = stderr.decode('utf-8').strip()
                logging.error(f"【{period.capitalize()}サマリー】ワーカーの実行に失敗しました:\n{error_msg}")
                message = "🤖 サマリー生成プロセスの起動に失敗しました。"
            
            # エラーメッセージの送信
            if interaction:
                await interaction.followup.send(message)
            else:
                await channel.send(message)

        except Exception as e:
            logging.error(f"【{period.capitalize()}サマリー】ワーカーの呼び出し処理自体に失敗しました: {e}", exc_info=True)

    @tasks.loop(time=DAILY_SUMMARY_TIME)
    async def daily_summary(self):
        today = datetime.datetime.now(JST).date()
        
        if self.last_summary_date == today:
            logging.info(f"【サマリー】本日（{today}）のサマリーは既に実行済みのため、スキップします。")
            return
        
        logging.info(f"【サマリー】定時実行（{DAILY_SUMMARY_TIME}）タスクを開始します。対象日: {today}")
        self.last_summary_date = today
        await self.run_summary_logic(period="daily", target_date=today)

    @tasks.loop(time=WEEKLY_SUMMARY_TIME)
    async def weekly_summary(self):
        today = datetime.datetime.now(JST).date()
        if today.weekday() == 6: # 6は日曜日
            logging.info(f"【週次サマリー】定時実行タスクを開始します。対象週: {today}")
            await self.run_summary_logic(period="weekly", target_date=today)

    @tasks.loop(time=MONTHLY_SUMMARY_TIME)
    async def monthly_summary(self):
        today = datetime.datetime.now(JST).date()
        # 月の最終日かどうかをチェック
        next_day = today + datetime.timedelta(days=1)
        if next_day.month != today.month:
            logging.info(f"【月次サマリー】定時実行タスクを開始します。対象月: {today.strftime('%Y-%m')}")
            await self.run_summary_logic(period="monthly", target_date=today)


    @app_commands.command(name="test_summary", description="今日のサマリー生成を手動でテスト実行します。")
    async def test_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        target_date = datetime.datetime.now(JST).date()
        await self.run_summary_logic(period="daily", target_date=target_date, interaction=interaction)

    @app_commands.command(name="test_weekly_summary", description="週次サマリー生成を手動でテスト実行します。")
    async def test_weekly_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        target_date = datetime.datetime.now(JST).date()
        await self.run_summary_logic(period="weekly", target_date=target_date, interaction=interaction)

    @app_commands.command(name="test_monthly_summary", description="月次サマリー生成を手動でテスト実行します。")
    async def test_monthly_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        target_date = datetime.datetime.now(JST).date()
        await self.run_summary_logic(period="monthly", target_date=target_date, interaction=interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))