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

class SummaryCog(commands.Cog):
    """サマリー生成Cog (定時実行タスクは無効化されています)"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.summary_channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.worker_path = str(Path(__file__).resolve().parent.parent / "summary_worker.py")

    # 定時実行タスク (daily_summary, weekly_summary, monthly_summary) は削除されました。

    async def run_summary_logic(self, period: str, target_date: datetime.date, interaction: discord.Interaction | None = None):
        """サマリー生成の手動実行用ロジック"""
        
        sync_cog = self.bot.get_cog('SyncCog')
        if sync_cog:
            logging.info(f"【{period.capitalize()}サマリー】生成前に同期を実行します...")
            await sync_cog.force_sync()
        
        channel = self.bot.get_channel(self.summary_channel_id)
        if not channel and not interaction:
            logging.error(f"【{period.capitalize()}サマリー】出力先チャンネルが見つかりません。")
            return
            
        logging.info(f"【{period.capitalize()}サマリー】ワーカー呼び出し: {target_date}")
        
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
                     message = f"📝 {target_date} のメモはありませんでした。"
                elif "NO_MEMO" in result:
                    message = f"📝 対象期間のメモはありませんでした。"
                elif result.startswith("ERROR:"):
                    message = f"🤖 エラー: {result}"
                else:
                    embed = discord.Embed(
                        title=f"📝 {target_date} {period.capitalize()} Summary",
                        description=result,
                        color=discord.Color.light_grey()
                    )
                    if interaction: await interaction.followup.send(embed=embed)
                    else: await channel.send(embed=embed)
                    return
            else:
                message = "🤖 サマリー生成プロセスの起動に失敗しました。"
                logging.error(f"Worker Error: {stderr.decode('utf-8')}")
            
            if interaction: await interaction.followup.send(message)
            else: await channel.send(message)

        except Exception as e:
            logging.error(f"Summary run error: {e}", exc_info=True)

    # --- Manual Test Commands (残しておきますが、不要であれば削除可能です) ---

    @app_commands.command(name="test_summary", description="サマリー生成を手動でテスト実行します。")
    async def test_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        target_date = datetime.datetime.now(JST).date()
        await self.run_summary_logic(period="daily", target_date=target_date, interaction=interaction)

    @app_commands.command(name="test_weekly_summary", description="週次サマリー生成を手動でテスト実行します。")
    async def test_weekly_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        target_date = datetime.datetime.now(JST).date()
        await self.run_summary_logic(period="weekly", target_date=target_date, interaction=interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))