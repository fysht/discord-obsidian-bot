import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord.ext import commands
from dotenv import load_dotenv
import re
try:
    from obsidian_handler import add_memo_async
except ImportError:
    logging.error("obsidian_handler.pyが見つかりません。起動時メモ処理が無効になります。")
    add_memo_async = None
import dropbox

# --- 1. 設定読み込み ---
log_format = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0")) 

# --- Dropbox関連設定 ---
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
LAST_PROCESSED_ID_FILE_PATH = f"{DROPBOX_VAULT_PATH}/.bot/last_processed_id.txt"


# --- 2. Bot本体のクラス定義 ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True 
        intents.members = True       
        intents.reactions = True     
        super().__init__(command_prefix="!", intents=intents) 

    async def setup_hook(self):
        """Cogをロードする"""
        logging.info("Cogの読み込みを開始します...")
        cogs_dir = Path(__file__).parent / 'cogs'

        successful_loads = 0
        failed_loads = []

        for filename in os.listdir(cogs_dir):
            if filename == "__pycache__":
                continue
            
            # --- ★ 修正: local_worker.py で実行するCogをスキップ ---
            # (youtube_cog.py がローカル実行専用)
            # --- ★ 修正: reception_cog.py もスキップ対象に追加 ---
            if filename == 'youtube_cog.py' or filename == 'reception_cog.py':
                logging.info(f" -> cogs/{filename} はローカルワーカーが担当、または自動転送ロジックと重複するためスキップします。")
                continue
            # --- 修正ここまで ---

            if filename.endswith('.py') and not filename.startswith('__'):
                cog_name = f'cogs.{filename[:-3]}'
                
                try:
                    await self.load_extension(cog_name)
                    logging.info(f" -> {cog_name} を読み込みました。")
                    successful_loads += 1
                except commands.ExtensionNotFound:
                     logging.error(f" -> {cog_name} が見つかりません。")
                     failed_loads.append(cog_name)
                except commands.ExtensionAlreadyLoaded:
                     logging.warning(f" -> {cog_name} は既に読み込まれています。")
                     successful_loads +=1
                except commands.NoEntryPointError:
                     logging.error(f" -> {cog_name} に setup 関数がありません。")
                     failed_loads.append(cog_name)
                except commands.ExtensionFailed as e:
                     logging.error(f" -> {cog_name} の読み込みに失敗しました: {e}", exc_info=True)
                     failed_loads.append(f"{cog_name} ({type(e.original).__name__})")
                except Exception as e:
                     logging.error(f" -> {cog_name} の読み込み中に予期せぬエラーが発生しました: {e}", exc_info=True)
                     failed_loads.append(f"{cog_name} (Unexpected Error)")


        logging.info(f"Cog読み込み完了: {successful_loads}個成功")
        if failed_loads:
             logging.error(f"Cog読み込み失敗: {len(failed_loads)}個 - {', '.join(failed_loads)}")

        try:
            await self.tree.sync()
            logging.info(f"グローバルに {len(self.tree.get_commands())} 個のスラッシュコマンドを同期しました。")
        except discord.HTTPException as e:
             logging.error(f"スラッシュコマンドの同期に失敗しました: {e}")
        except Exception as e:
             logging.error(f"スラッシュコマンド同期中に予期せぬエラー: {e}", exc_info=True)

        memo_cog = self.get_cog("MemoCog")
        if memo_cog:
             logging.info("Persistent views from MemoCog should be registered.")
        else:
             logging.warning("MemoCog not loaded, cannot register persistent views.")


    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (ID: {self.user.id})")
        logging.info(f"discord.py version: {discord.__version__}")

        if add_memo_async:
             if all([DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET]):
                 await self.process_offline_memos()
             else:
                  logging.warning("Dropbox認証情報がないため、オフラインメモ処理をスキップします。")
        else:
             logging.warning("obsidian_handlerが見つからないため、オフラインメモ処理をスキップします。")

        logging.info("--- Bot is ready and listening for events ---")

    async def process_offline_memos(self):
        logging.info("オフライン中の未取得メモがないか確認します...")
        after_message_id = None
        dbx = None 

        try:
            dbx = dropbox.Dropbox(
                oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
                app_key=DROPBOX_APP_KEY,
                app_secret=DROPBOX_APP_SECRET
            )
            _, res = dbx.files_download(LAST_PROCESSED_ID_FILE_PATH)
            last_id_str = res.content.decode('utf-8').strip()
            if last_id_str:
                after_message_id = int(last_id_str)
                logging.info(f"Dropboxから最終処理ID: {last_id_str} を読み込みました。")
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.DownloadError) and e.error.get_path().is_not_found():
                logging.info("最終処理IDファイルが見つかりません。チャンネル履歴の最初から取得します (件数に注意)。")
                after_message_id = None
            else:
                logging.error(f"Dropboxからの最終処理IDファイルの読み込みに失敗: {e}")
                return 
        except ValueError:
             logging.error(f"最終処理IDファイルの内容が無効です: {last_id_str}")
             return
        except Exception as e:
            logging.error(f"最終処理IDの読み込み中に予期せぬエラー: {e}")
            return

        channel = self.get_channel(MEMO_CHANNEL_ID)
        if not channel:
            logging.error(f"MEMO_CHANNEL_ID: {MEMO_CHANNEL_ID} のチャンネルが見つかりません。")
            return

        try:
            history = []
            limit = 1000
            logging.info(f"Fetching message history from channel {channel.name} after ID: {after_message_id}")
            after_obj = discord.Object(id=after_message_id) if after_message_id else None

            async for message in channel.history(limit=limit, after=after_obj, oldest_first=True):
                 history.append(message)

            if history:
                logging.info(f"{len(history)}件の未取得メモが見つかりました。保存します...")
                latest_processed_id = None
                for message in history: 
                    if not message.author.bot:
                        # ★ 修正: add_memo_async に必要な引数を渡す (添付ファイル memo_cog.py に合わせる)
                        # (ただし、この関数はテキストメモのみを対象とするべき)
                        if not URL_REGEX.search(message.content.strip()): # URLが含まれないもののみ
                            await add_memo_async(
                                content=message.content,
                                author=f"{message.author} ({message.author.id})",
                                created_at=message.created_at.isoformat(),
                                message_id=message.id,
                                context="Discord Memo Channel (Offline Sync)", # コンテキスト情報を追加
                                category="Memo" # カテゴリ情報を追加
                            )
                        latest_processed_id = message.id
                logging.info("未取得メモの保存が完了しました。")
                # ★ 修正ここまで

                if latest_processed_id and dbx:
                    try:
                        await asyncio.to_thread(
                            dbx.files_upload,
                            str(latest_processed_id).encode('utf-8'),
                            LAST_PROCESSED_ID_FILE_PATH,
                            mode=dropbox.files.WriteMode('overwrite')
                        )
                        logging.info(f"最終処理IDをDropboxに保存しました: {latest_processed_id}")
                    except Exception as e_upload:
                        logging.error(f"最終処理IDのDropboxへの保存に失敗: {e_upload}")
            else:
                logging.info("処理対象の新しいメモはありませんでした。")

        except discord.Forbidden:
             logging.error(f"チャンネル {channel.name} の履歴読み取り権限がありません。")
        except discord.HTTPException as e:
             logging.error(f"履歴の取得中にHTTPエラーが発生しました: {e}")
        except Exception as e:
            logging.error(f"履歴の取得または処理中に予期せぬエラーが発生しました: {e}", exc_info=True)

# --- 3. 起動処理 ---
async def main():
    if not TOKEN:
         logging.critical("DISCORD_BOT_TOKENが設定されていません。ボットを起動できません。")
         return

    bot = MyBot()
    try:
        await bot.start(TOKEN)
    except discord.LoginFailure:
         logging.critical("Discordトークンが無効です。ボットを起動できません。")
    except Exception as e:
         logging.critical(f"ボットの起動中に致命的なエラーが発生しました: {e}", exc_info=True)

# ★ 修正: process_offline_memos で URL_REGEX を使うため、ここで定義
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("プログラムが手動で終了されました。")