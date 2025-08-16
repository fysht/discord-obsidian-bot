# cogs/sync_cog.py

import discord
from discord.ext import commands, tasks
import json
import asyncio # 非同期で外部プログラムを呼び出すために必要
import sys # Pythonの実行ファイルパスを取得するために必要

PENDING_MEMOS_FILE = "pending_memos.json"
SYNC_INTERVAL_SECONDS = 10

class SyncCog(commands.Cog):
    """定期的に外部の同期ワーカーを呼び出すCog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.auto_sync_loop.start()

    def cog_unload(self):
        self.auto_sync_loop.cancel()

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def auto_sync_loop(self):
        # 保留中のメモがあるかだけをチェック
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding='utf-8') as f:
                if not json.load(f):
                    return # メモがなければ何もしない
        except (FileNotFoundError, json.JSONDecodeError):
            return # ファイルがなければ何もしない

        print("【自動同期】未同期メモを検出。外部の同期ワーカーを呼び出します...")

        try:
            # 外部のsync_worker.pyを、Botとは完全に別のプロセスとして実行する
            # sys.executableは現在実行中のPythonのパス (例: C:\Python313\python.exe)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, 'sync_worker.py',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)

            # 外部プログラムの実行結果（出力）を取得
            stdout, stderr = await proc.communicate()

            # 結果をコンソールに表示
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
        await self.bot.wait_until_ready()
        print("自動同期ループを開始します。")

async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))