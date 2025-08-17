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
# タイムゾーンをJSTに設定
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
# サマリーを生成する時刻を23:59 JSTに設定
SUMMARY_TIME = datetime.time(hour=23, minute=59, tzinfo=JST)

class SummaryCog(commands.Cog):
    """毎日定時に外部のサマリー生成ワーカーを呼び出すCog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        # Botのルートディレクトリにある 'summary_worker.py' へのパスを構築
        self.worker_path = str(Path(__file__).resolve().parent.parent / "summary_worker.py")
        self.daily_summary.start()

    def cog_unload(self):
        self.daily_summary.cancel()

    async def run_summary_logic(self, interaction: discord.Interaction | None = None):
        """サマリー生成のメインロジック。スケジュール実行と手動実行の両方から呼ばれる"""
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel:
            if interaction:
                await interaction.followup.send("エラー: 対象のチャンネルが見つかりませんでした。")
            print("【サマリー】エラー: 対象のチャンネルが見つかりませんでした。")
            return
            
        print("【サマリー】外部のサマリー生成ワーカーを呼び出します...")
        
        try:
            # 外部プロセスで文字化けが起きないようにエンコーディングを指定
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            proc = await asyncio.create_subprocess_exec(
                sys.executable, self.worker_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

            # ワーカーからの結果報告を待つ
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # ワーカーからの報告（標準出力）を受け取る
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
                    # 成功した場合、固定形式の埋め込みメッセージを作成して投稿
                    today = datetime.datetime.now(JST).date()
                    embed = discord.Embed(
                        title=f" {today.strftime('%Y年%m月%d日')} のサマリー",
                        description=result,
                        color=discord.Color.from_rgb(112, 128, 144) # SlateGray
                    )
                    if interaction:
                        await interaction.followup.send(embed=embed)
                    else:
                        await channel.send(embed=embed)
            else:
                # ワーカー自体の起動に失敗した場合
                error_msg = stderr.decode('utf-8').strip()
                print(f"【サマリー】ワーカーの実行に失敗しました:\n{error_msg}")
                message = "🤖 サマリー生成プロセスの起動に失敗しました。"
                if interaction:
                    await interaction.followup.send(message)
                else:
                    await channel.send(message)

        except Exception as e:
            print(f"【サマリー】ワーカーの呼び出し処理自体に失敗しました: {e}")

    # スケジュール実行タスク
    @tasks.loop(time=SUMMARY_TIME)
    async def daily_summary(self):
        print(f"【サマリー】定時実行（{SUMMARY_TIME}）タスクを開始します。")
        await self.run_summary_logic()

    @daily_summary.before_loop
    async def before_daily_summary(self):
        await self.bot.wait_until_ready()

    # 手動実行用のスラッシュコマンド
    @app_commands.command(name="test_summary", description="今日のサマリー生成を手動でテスト実行します。")
    async def test_summary(self, interaction: discord.Interaction):
        # deferで「考え中...」と表示させ、タイムアウトを防ぐ
        await interaction.response.defer(ephemeral=False)
        await self.run_summary_logic(interaction=interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))