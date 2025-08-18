import os
import sys
import logging
import asyncio
from pathlib import Path
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

# --- 定数定義 ---
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))

class SyncCog(commands.Cog):
    """定期的に外部の同期ワーカーを呼び出すCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Botのルートディレクトリにある 'sync_worker.py' へのパスを構築
        self.worker_path = str(Path(__file__).resolve().parent.parent / "sync_worker.py")
        self.sync_lock = asyncio.Lock() # 同期処理の多重実行を防止するロック

    @commands.Cog.listener()
    async def on_ready(self):
        """Botの準備完了時にタスクを開始"""
        if not self.auto_sync_loop.is_running():
            logger.info("自動同期ループの開始を待機しています...")
            self.auto_sync_loop.start()

    def cog_unload(self):
        """Cogのアンロード時にタスクをキャンセル"""
        self.auto_sync_loop.cancel()

    @tasks.loop(minutes=5) # 5分間隔で実行
    async def auto_sync_loop(self):
        # 処理対象のファイルが存在しないか、中身が空なら何もしない
        if not PENDING_MEMOS_FILE.exists() or PENDING_MEMOS_FILE.stat().st_size == 0:
            return

        # 前回の同期処理が実行中であれば、今回はスキップ
        if self.sync_lock.locked():
            logger.warning("【自動同期】前回の同期処理がまだ完了していないため、今回の実行はスキップします。")
            return

        async with self.sync_lock:
            logger.info("【自動同期】未同期のメモを検出しました。同期ワーカーを呼び出します...")

            try:
                # 外部プロセスとして同期ワーカーを実行
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, self.worker_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode == 0:
                    log_output = stdout.decode('utf-8', 'ignore').strip()
                    logger.info(f"【自動同期】ワーカーが正常に完了しました。\n--- ワーカーログ ---\n{log_output}\n--------------------")
                else:
                    error_output = stderr.decode('utf-8', 'ignore').strip()
                    logger.error(f"【自動同期】ワーカーの実行に失敗しました (終了コード: {proc.returncode})。\n--- ワーカーエラーログ ---\n{error_output}\n--------------------------")

            except Exception as e:
                logger.error(f"【自動同期】ワーカーの呼び出し処理自体に失敗しました: {e}", exc_info=True)

    @auto_sync_loop.before_loop
    async def before_auto_sync_loop(self):
        """ループ開始前にBotの準備が整うのを待つ"""
        await self.bot.wait_until_ready()
        logger.info(f"自動同期ループを開始します（間隔: 5分、監視対象: {PENDING_MEMOS_FILE}）")


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))