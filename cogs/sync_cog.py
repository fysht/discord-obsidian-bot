import os
import sys
import logging
from pathlib import Path
import asyncio
from discord.ext import commands, tasks
from dotenv import load_dotenv

# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
sys.stdout.reconfigure(encoding='utf-8')

# --- 基本設定 ---
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))

class SyncCog(commands.Cog):
    """定期的に外部の同期ワーカーを呼び出すCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 親ディレクトリの sync_worker.py を指すパス
        self.worker_path = str(Path(__file__).resolve().parent.parent / "sync_worker.py")
        self.sync_lock = asyncio.Lock()
        self.logger = logging.getLogger(__name__)

    @commands.Cog.listener()
    async def on_ready(self):
        """Botの準備完了時にタスクを開始"""
        self.logger.info("自動同期待機ループを開始します...")
        if not self.auto_sync_loop.is_running():
            self.auto_sync_loop.start()

    def cog_unload(self):
        """Cogのアンロード時にタスクをキャンセル"""
        self.auto_sync_loop.cancel()

    async def force_sync(self):
        """
        保留中のメモを強制的に同期します。
        ロックを使用して、多重実行を防止します。
        """
        if self.sync_lock.locked():
            self.logger.warning("【強制同期】現在、別の同期処理が実行中のため、今回の実行はスキップします。")
            return

        async with self.sync_lock:
            self.logger.info("【強制同期】保留中のメモの同期処理を開始します...")
            
            if not PENDING_MEMOS_FILE.exists() or PENDING_MEMOS_FILE.stat().st_size == 0:
                self.logger.info("【強制同期】保留中のメモはありませんでした。")
                return

            self.logger.info("【強制同期】未同期のメモを検出しました。同期ワーカーを呼び出します...")
            try:
                # タイムアウト付きでサブプロセスを実行 (最大120秒)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, self.worker_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                except asyncio.TimeoutError:
                    proc.kill()
                    self.logger.error("【強制同期】ワーカーがタイムアウトしました。")
                    return

                if proc.returncode == 0:
                    log_output = stdout.decode('utf-8', 'ignore').strip()
                    self.logger.info(f"【強制同期】ワーカーが正常に完了しました。\n--- ワーカーログ ---\n{log_output}\n--------------------")
                else:
                    error_output = stderr.decode('utf-8', 'ignore').strip()
                    self.logger.error(f"【強制同期】ワーカーの実行に失敗しました (終了コード: {proc.returncode})。\n--- ワーカーエラーログ ---\n{error_output}\n--------------------------")
            except Exception as e:
                self.logger.error(f"【強制同期】ワーカーの呼び出し処理自体に失敗しました: {e}", exc_info=True)


    @tasks.loop(minutes=5)
    async def auto_sync_loop(self):
        """5分ごとに保留メモの同期を試みるループ"""
        await self.force_sync()

    @auto_sync_loop.before_loop
    async def before_auto_sync_loop(self):
        """ループ開始前にBotの準備が整うのを待つ"""
        await self.bot.wait_until_ready()
        self.logger.info(f"自動同期ループを開始します（間隔: 5分、監視対象: {PENDING_MEMOS_FILE}）")


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))