import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import sys
from pathlib import Path
import logging

# --- リファクタリング: 定数のクリーンなインポート ---
from config import JST

class SummaryCog(commands.Cog):
    """サマリー生成Cog (定時実行タスクは無効化されています)"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.summary_channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.worker_path = str(Path(__file__).resolve().parent.parent / "summary_worker.py")

    async def run_summary_logic(self, period: str, target_date: datetime.date, interaction: discord.Interaction | None = None):
        """サマリー生成の手動実行用ロジック"""
        
        sync_cog = self.bot.get_cog('SyncCog')
        if sync_cog:
            logging.info(f"【{period.capitalize()}サマリー】生成前に同期を実行します...")
            await sync_cog.force_sync()
        
        channel = self.bot.get_channel(self.summary_channel_id)
        if not channel and not interaction:
            logging.error(f"【{period.capitalize()}サマリー】指定されたチャンネル(ID: {self.summary_channel_id})が見つかりません。")
            return

        message = ""
        if interaction:
            await interaction.response.defer(ephemeral=False)
            message = await interaction.followup.send(f"⏳ **{period.capitalize()} サマリー**の生成を開始します...")
        elif channel:
            message = await channel.send(f"⏳ **{period.capitalize()} サマリー**の生成を開始します...")

        try:
            date_str = target_date.strftime("%Y-%m-%d")
            
            proc = await asyncio.create_subprocess_exec(
                sys.executable, self.worker_path, period, date_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                summary_text = stdout.decode('utf-8').strip()
                if summary_text == "NO_MEMO_TODAY":
                     msg = "📝 今日はまだメモがないみたいです。"
                     if interaction: await interaction.followup.send(msg)
                     else: await channel.send(msg)
                     return

                # 2000文字を超える場合はファイルとして送信する処理
                if len(summary_text) > 2000:
                    file_path = f"{period}_summary.md"
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(summary_text)
                    if interaction:
                         await interaction.followup.send(f"✅ **{period.capitalize()} サマリー**が完成しました！", file=discord.File(file_path))
                    else:
                         await channel.send(f"✅ **{period.capitalize()} サマリー**が完成しました！", file=discord.File(file_path))
                    os.remove(file_path)
                    return
                else:
                    embed = discord.Embed(
                        title=f"📅 {period.capitalize()} Summary ({date_str})",
                        description=summary_text,
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