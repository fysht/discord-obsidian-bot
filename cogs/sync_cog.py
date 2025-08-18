import os
import json
import asyncio
import sys
from pathlib import Path
from discord.ext import commands, tasks
from filelock import FileLock, Timeout

# 環境変数ベースに統一
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "pending_memos.json"))

# デフォルトは60秒。必要なら環境変数で上書き（例: 30）
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "60"))

class SyncCog(commands.Cog):
    """定期的に外部の同期ワーカーを呼び出すCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # ルート直下の sync_worker.py への絶対パス
        self.worker_path = str(Path(__file__).resolve().parent.parent / "sync_worker.py")
        self.auto_sync_loop.start()

    def cog_unload(self):
        self.auto_sync_loop.cancel()

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def auto_sync_loop(self):
        # 同期対象の有無をロック付きで確認
        lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
        try:
            with lock.acquire(timeout=5):
                if not PENDING_MEMOS_FILE.exists():
                    return
                with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                    memos = json.load(f)
                    if not memos:
                        return
        except (Timeout, FileNotFoundError, json.JSONDecodeError):
            return

        print(f"【自動同期】未同期メモを検出（{PENDING_MEMOS_FILE}）。外部ワーカーを呼び出します...")

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, self.worker_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy()  # RCLONE_CONFIG などを継承
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                print("【自動同期】外部ワーカーが正常終了。")
                if stdout:
                    print(f"[worker stdout]\n{stdout.decode('utf-8', errors='ignore')}")
            else:
                print("【自動同期】外部ワーカーでエラー。")
                if stderr:
                    print(f"[worker stderr]\n{stderr.decode('utf-8', errors='ignore')}")
        except Exception as e:
            print(f"【自動同期】ワーカー呼び出し失敗: {e}")

    @auto_sync_loop.before_loop
    async def before_auto_sync_loop(self):
        await self.bot.wait_until_ready()
        print(f"自動同期ループを開始（間隔: {SYNC_INTERVAL_SECONDS}秒、監視: {PENDING_MEMOS_FILE}）")

async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))