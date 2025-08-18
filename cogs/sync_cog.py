import os
import logging
from pathlib import Path
import discord
from discord.ext import commands, tasks
import subprocess
import json
import shutil

logger = logging.getLogger(__name__)

# 環境変数から設定を取得
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/var/data/vault"))
DROPBOX_REMOTE = os.getenv("DROPBOX_REMOTE", "dropbox")

class SyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # __init__ ではループを開始せず、on_ready で開始する

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.auto_sync_loop.is_running():
            self.auto_sync_loop.start()
            logger.info(f"自動同期ループを開始（間隔: 60秒、監視: {PENDING_MEMOS_FILE}）")

    @tasks.loop(seconds=60)
    async def auto_sync_loop(self):
        if PENDING_MEMOS_FILE.exists():
            try:
                with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                    memos = json.load(f)
            except Exception as e:
                logger.error(f"[AUTO-SYNC] failed to load pending memos: {e}")
                return

            if memos:
                logger.info("【自動同期】未同期メモを検出...")
                await self.bot.loop.run_in_executor(None, self._process_and_sync, memos)

    def _process_and_sync(self, memos):
        logger.info(f"[PROCESS] processing {len(memos)} memos...")

        # Memo 保存先: KnowledgeBase/DailyNotes/
        daily_notes_path = VAULT_PATH / "KnowledgeBase" / "DailyNotes"
        daily_notes_path.mkdir(parents=True, exist_ok=True)

        for memo in memos:
            try:
                date = memo["created_at"][:10]  # YYYY-MM-DD
                file_path = daily_notes_path / f"{date}.md"
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(f"- {memo['created_at']} ({memo['author']}): {memo['content']}\n")
            except Exception as e:
                logger.error(f"[PROCESS] failed to write memo: {e}")

        # rclone 同期処理
        rclone_path = shutil.which("rclone")
        logger.info(f"[SYNC] rclone: {rclone_path}")
        logger.info(f"[SYNC] PATH={os.getenv('PATH')}")
        logger.info(f"[SYNC] RCLONE_CONFIG={os.getenv('RCLONE_CONFIG')}")

        try:
            cmd_up = [
                "rclone", "copy", str(VAULT_PATH), f"{DROPBOX_REMOTE}:/vault",
                "--update", "--create-empty-src-dirs", "--verbose"
            ]
            cmd_down = [
                "rclone", "copy", f"{DROPBOX_REMOTE}:/vault", str(VAULT_PATH),
                "--update", "--create-empty-src-dirs", "--verbose"
            ]
            res1 = subprocess.run(cmd_up, check=True, capture_output=True)
            res2 = subprocess.run(cmd_down, check=True, capture_output=True)
            logger.info(f"[SYNC] Dropbox sync successful.\nstdout:\n{res1.stdout.decode()}\n{res2.stdout.decode()}")
        except subprocess.CalledProcessError as e:
            logger.error(f"[SYNC] rclone sync failed: {e.stderr.decode()}")

        # 処理済みメモをクリア
        try:
            with open(PENDING_MEMOS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            logger.info("[PROCESS] cleared pending memos.")
        except Exception as e:
            logger.error(f"[PROCESS] failed to clear pending memos: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))