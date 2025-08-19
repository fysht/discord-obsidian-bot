import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import zoneinfo # 標準ライブラリ (Python 3.9+)
import asyncio
import sys
from pathlib import Path

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUMMARY_TIME = datetime.time(hour=23, minute=59, tzinfo=JST)

class SummaryCog(commands.Cog):
    """毎日定時に外部のサマリー生成ワーカーを呼び出すCog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.worker_path = str(Path(__file__).resolve().parent.parent / "summary_worker.py")
        self.last_summary_date = None # 最後にサマリーを生成した日付を記録
        self.daily_summary.start()

    def cog_unload(self):
        self.daily_summary.cancel()

    async def run_summary_logic(self, target_date: datetime.date, interaction: discord.Interaction | None = None):
        """サマリー生成のメインロジック。日付を指定して実行する"""
        
        # --- サマリー実行前に同期処理を強制実行 ---
        # discord.py 2.0以降では、Cog名はクラス名になる
        sync_cog = self.bot.get_cog('SyncCog')
        if sync_cog and hasattr(sync_cog, 'sync_lock') and not sync_cog.sync_lock.locked():
            print("【サマリー】サマリー生成前に、保留中のメモを同期します...")
            await sync_cog.auto_sync_loop()
            print("【サマリー】同期が完了しました。")
        else:
            print("【サマリー】現在、別の同期処理が実行中のため、10秒待機します...")
            await asyncio.sleep(10)
        
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel:
            if interaction:
                await interaction.followup.send("エラー: 対象のチャンネルが見つかりませんでした。")
            print("【サマリー】エラー: 対象のチャンネルが見つかりませんでした。")
            return
            
        print(f"【サマリー】{target_date} のサマリーを生成するため、外部ワーカーを呼び出します...")
        
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            # 日付を引数としてワーカーに渡す
            proc = await asyncio.create_subprocess_exec(
                sys.executable, self.worker_path, str(target_date),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                result = stdout.decode('utf-8').strip()
                
                if "NO_MEMO_TODAY" in result:
                    message = f"📝 {target_date.strftime('%Y年%m月%d日')} のメモはありませんでした！"
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
                    embed = discord.Embed(
                        title=f" {target_date.strftime('%Y年%m月%d日')} のサマリー",
                        description=result,
                        color=discord.Color.from_rgb(112, 128, 144)
                    )
                    if interaction:
                        await interaction.followup.send(embed=embed)
                    else:
                        await channel.send(embed=embed)
            else:
                error_msg = stderr.decode('utf-8').strip()
                print(f"【サマリー】ワーカーの実行に失敗しました:\n{error_msg}")
                message = "🤖 サマリー生成プロセスの起動に失敗しました。"
                if interaction:
                    await interaction.followup.send(message)
                else:
                    await channel.send(message)

        except Exception as e:
            print(f"【サマリー】ワーカーの呼び出し処理自体に失敗しました: {e}")

    @tasks.loop(time=SUMMARY_TIME)
    async def daily_summary(self):
        today = datetime.datetime.now(JST).date()
        
        # --- 誤作動防止ロジック ---
        if self.last_summary_date == today:
            print(f"【サマリー】本日（{today}）のサマリーは既に実行済みのため、スキップします。")
            return
        
        print(f"【サマリー】定時実行（{SUMMARY_TIME}）タスクを開始します。対象日: {today}")
        self.last_summary_date = today # 実行した日付を記録
        await self.run_summary_logic(target_date=today)

    @daily_summary.before_loop
    async def before_daily_summary(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="test_summary", description="今日のサマリー生成を手動でテスト実行します。")
    async def test_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        target_date = datetime.datetime.now(JST).date()
        await self.run_summary_logic(target_date=target_date, interaction=interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))