# cogs/summary_cog.py

import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import datetime
import asyncio
import sys

JST = datetime.timezone(datetime.timedelta(hours=9))
SUMMARY_TIME = datetime.time(hour=22, minute=0, tzinfo=JST)

class SummaryCog(commands.Cog):
    """毎日定時に外部のサマリー生成ワーカーを呼び出すCog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.daily_summary.start()

    def cog_unload(self):
        self.daily_summary.cancel()

    async def run_summary_logic(self, interaction: discord.Interaction | None = None):
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel:
            if interaction:
                await interaction.followup.send("対象のチャンネルが見つかりませんでした。")
            return
            
        print("【サマリー】外部のサマリー生成ワーカーを呼び出します...")
        
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            proc = await asyncio.create_subprocess_exec(
                sys.executable, 'summary_worker.py',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                result = stdout.decode('utf-8').strip()
                
                if "NO_MEMO_TODAY" in result:
                    message = "📝 今日のメモはありませんでした！"
                    if interaction:
                        await interaction.followup.send(message)
                    else:
                        await channel.send(message)
                elif result.startswith("ERROR:"):
                    print(f"【サマリー】ワーカーでエラー発生: {result}")
                    message = "🤖 AIによるサマリーの生成中にエラーが発生しました。"
                    if interaction:
                        await interaction.followup.send(message)
                    else:
                        await channel.send(message)
                else:
                    today = datetime.datetime.now(JST).date()
                    embed = discord.Embed(
                        title=f"📝 {today.strftime('%Y年%m月%d日')} のふりかえり",
                        description=result,
                        color=discord.Color.blue()
                    )
                    if interaction:
                        await interaction.followup.send(embed=embed)
                    else:
                        await channel.send(embed=embed)
            else:
                error_msg = stderr.decode('utf-8').strip()
                print(f"【サマリー】ワーカーの実行に失敗しました: {error_msg}")
                message = "🤖 サマリー生成プロセスの起動に失敗しました。"
                if interaction:
                    await interaction.followup.send(message)
                else:
                    await channel.send(message)

        except Exception as e:
            print(f"【サマリー】ワーカーの呼び出しに失敗しました: {e}")

    @tasks.loop(time=SUMMARY_TIME)
    async def daily_summary(self):
        await self.run_summary_logic()

    @daily_summary.before_loop
    async def before_daily_summary(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="test_summary", description="今日のサマリー生成を手動でテスト実行します。")
    async def test_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.run_summary_logic(interaction=interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))