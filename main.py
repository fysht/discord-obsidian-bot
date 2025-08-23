import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord.ext import commands
from dotenv import load_dotenv
from obsidian_handler import add_memo_async
import dropbox

# --- 1. 設定読み込み ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))

# --- Dropbox関連設定 ---
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
# 状態ファイルのパスをDropbox内に変更
LAST_PROCESSED_ID_FILE_PATH = f"{DROPBOX_VAULT_PATH}/.bot/last_processed_id.txt"


# --- 2. Bot本体のクラス定義 ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """Cogをロードし、オフライン中のメモを処理する"""
        logging.info("Cogの読み込みを開始します...")
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    logging.info(f" -> {filename} を読み込みました。")
                except Exception as e:
                    logging.error(f" -> {filename} の読み込みに失敗しました: {e}", exc_info=True)

        await self.tree.sync()
        logging.info(f"{len(self.tree.get_commands())}個のスラッシュコマンドを同期しました。")

    async def on_ready(self):
        """Botの準備が完了したときの処理"""
        logging.info(f"{self.user} としてログインしました (ID: {self.user.id})")
        await self.process_offline_memos()

    async def process_offline_memos(self):
        """オフライン中の未取得メモがないか確認し、処理する"""
        logging.info("オフライン中の未取得メモがないか確認します...")
        after_message = None
        last_id_str = None

        try:
            with dropbox.Dropbox(
                oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
                app_key=DROPBOX_APP_KEY,
                app_secret=DROPBOX_APP_SECRET
            ) as dbx:
                _, res = dbx.files_download(LAST_PROCESSED_ID_FILE_PATH)
                last_id_str = res.content.decode('utf-8').strip()
                if last_id_str:
                    after_message = discord.Object(id=int(last_id_str))
                    logging.info(f"Dropboxから最終処理ID: {last_id_str} を読み込みました。")
        except dropbox.exceptions.ApiError as e:
            # ファイルが存在しない場合は初回起動なので何もしない
            if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.info("最終処理IDファイルが見つかりません。すべての履歴から取得します。")
            else:
                logging.error(f"Dropboxからの最終処理IDファイルの読み込みに失敗: {e}")
        except Exception as e:
            logging.error(f"最終処理IDの解析に失敗: {e}")

        channel = self.get_channel(MEMO_CHANNEL_ID)
        if not channel:
            logging.error(f"MEMO_CHANNEL_ID: {MEMO_CHANNEL_ID} のチャンネルが見つかりません。")
            return

        try:
            history = [m async for m in channel.history(limit=None, after=after_message)]
            if history:
                logging.info(f"{len(history)}件の未取得メモが見つかりました。保存します...")
                for message in sorted(history, key=lambda m: m.created_at):
                    if not message.author.bot:
                        await add_memo_async(
                            content=message.content,
                            author=f"{message.author} ({message.author.id})",
                            created_at=message.created_at.isoformat(),
                            message_id=message.id
                        )
                logging.info("未取得メモの保存が完了しました。")
            else:
                logging.info("処理対象の新しいメモはありませんでした。")
        except Exception as e:
            logging.error(f"履歴の取得または処理中にエラーが発生しました: {e}", exc_info=True)


# --- 3. 起動処理 ---
def main():
    bot = MyBot()
    # bot.start()ではなく、bot.run()を使用する方が安定
    bot.run(TOKEN)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("プログラムが強制終了されました。")