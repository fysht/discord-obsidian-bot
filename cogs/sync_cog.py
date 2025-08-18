import discord
from discord.ext import commands, tasks
import json
import asyncio
import sys
from pathlib import Path
from filelock import FileLock, Timeout

PENDING_MEMOS_FILE = "pending_memos.json"
SYNC_INTERVAL_SECONDS = 60 # 実行間隔を60秒に設定

class SyncCog(commands.Cog):
    """定期的に外部の同期ワーカーを呼び出すCog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 外部スクリプトへのパスを堅牢な方法で構築
        # このCogファイル(sync_cog.py)の親ディレクトリ(cogs)の、さらに親(プロジェクトルート)にある
        # 'sync_worker.py' を指すように設定
        self.worker_path = str(Path(__file__).resolve().parent.parent / "sync_worker.py")
        self.auto_sync_loop.start()

    def cog_unload(self):
        """Cogがリロードされる際にループを安全に停止させる"""
        self.auto_sync_loop.cancel()

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def auto_sync_loop(self):
        # 実行前に処理すべきメモがあるか、ファイルロックをかけて安全にチェック
        lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
        try:
            # 5秒間だけロックの取得を試みる
            with lock.acquire(timeout=5):
                pending_memos_path = Path(PENDING_MEMOS_FILE)
                # ファイルの存在と中身を安全にチェック
                if not pending_memos_path.exists():
                    return # ファイルがなければ何もしない
                
                with open(pending_memos_path, "r", encoding='utf-8') as f:
                    memos = json.load(f)
                    if not memos:
                        return # 中身が空なら何もしない
                        
        except (Timeout, FileNotFoundError, json.JSONDecodeError):
            # ロック取得失敗、ファイルなし、JSONエラーの場合は次のループまで待つ
            return

        print("【自動同期】未同期メモを検出。外部の同期ワーカーを呼び出します...")

        try:
            # 堅牢なパス指定でワーカーを呼び出す
            proc = await asyncio.create_subprocess_exec(
                sys.executable, self.worker_path, # ここで堅牢なパスを使用
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)

            # ワーカーの実行結果を監視・表示
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                print("【自動同期】外部ワーカーが正常に処理を完了しました。")
                if stdout:
                    print(f"   [ワーカーからの出力]:\n{stdout.decode('utf-8', errors='ignore')}")
            else:
                print("【自動同期】外部ワーカーの実行中にエラーが発生しました。")
                if stderr:
                    print(f"   [ワーカーからのエラー]:\n{stderr.decode('utf-8', errors='ignore')}")

        except Exception as e:
            print(f"【自動同期】外部ワーカーの呼び出しに失敗しました: {e}")

    @auto_sync_loop.before_loop
    async def before_auto_sync_loop(self):
        """ループが開始される前に、Botの準備が完了するのを待つ"""
        await self.bot.wait_until_ready()
        print(f"自動同期ループを開始します。（間隔: {SYNC_INTERVAL_SECONDS}秒）")

async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))