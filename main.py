import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord.ext import commands
from dotenv import load_dotenv
# obsidian_handler をインポート
try:
    from obsidian_handler import add_memo_async
except ImportError:
    logging.error("obsidian_handler.pyが見つかりません。起動時メモ処理が無効になります。")
    # ダミー関数またはNoneを設定
    add_memo_async = None
import dropbox

# --- 1. 設定読み込み ---
# ログレベルをINFOに設定 (デバッグ時はDEBUGに変更可)
# フォーマットにファイル名や行番号を追加するとデバッグに便利
log_format = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
# discordライブラリ自体のログレベルを設定 (任意)
# logging.getLogger('discord').setLevel(logging.WARNING)

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
# メモチャンネルIDは MemoCog 内で取得・使用する
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0")) # 起動時処理用に保持

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
        intents.message_content = True # メッセージ内容へのアクセス
        intents.members = True       # メンバー情報の取得 (任意)
        intents.reactions = True     # リアクションイベント
        # intents.guilds = True       # サーバー情報の取得 (デフォルトで有効)
        super().__init__(command_prefix="!", intents=intents) # プレフィックスコマンドは現在未使用
        # self.google_search = google_search_function # Google検索は未使用

    async def setup_hook(self):
        """Cogをロードする"""
        logging.info("Cogの読み込みを開始します...")
        cogs_dir = Path(__file__).parent / 'cogs'

        successful_loads = 0
        failed_loads = []

        for filename in os.listdir(cogs_dir):
            # __pycache__ ディレクトリを無視
            if filename == "__pycache__":
                continue
            # .pyファイルで、__で始まらないものを対象
            if filename.endswith('.py') and not filename.startswith('__'):
                cog_name = f'cogs.{filename[:-3]}'
                if filename == 'reception_cog.py':
                    logging.info(f" -> {cog_name} は手動リアクションのためスキップします。")
                    continue
                try:
                    await self.load_extension(cog_name)
                    logging.info(f" -> {cog_name} を読み込みました。")
                    successful_loads += 1
                except commands.ExtensionNotFound:
                     logging.error(f" -> {cog_name} が見つかりません。")
                     failed_loads.append(cog_name)
                except commands.ExtensionAlreadyLoaded:
                     logging.warning(f" -> {cog_name} は既に読み込まれています。")
                     # 既に読み込まれている場合は成功カウントに含める
                     successful_loads +=1
                except commands.NoEntryPointError:
                     logging.error(f" -> {cog_name} に setup 関数がありません。")
                     failed_loads.append(cog_name)
                except commands.ExtensionFailed as e:
                     # Cogの初期化中 (init) または setup 関数内でエラーが発生した場合
                     logging.error(f" -> {cog_name} の読み込みに失敗しました: {e}", exc_info=True)
                     failed_loads.append(f"{cog_name} ({type(e.original).__name__})")
                except Exception as e:
                     # その他の予期せぬエラー
                     logging.error(f" -> {cog_name} の読み込み中に予期せぬエラーが発生しました: {e}", exc_info=True)
                     failed_loads.append(f"{cog_name} (Unexpected Error)")


        logging.info(f"Cog読み込み完了: {successful_loads}個成功")
        if failed_loads:
             logging.error(f"Cog読み込み失敗: {len(failed_loads)}個 - {', '.join(failed_loads)}")


        # スラッシュコマンドの同期 (エラーハンドリング追加)
        try:
            await self.tree.sync()
            logging.info(f"グローバルに {len(self.tree.get_commands())} 個のスラッシュコマンドを同期しました。")
        except discord.HTTPException as e:
             logging.error(f"スラッシュコマンドの同期に失敗しました: {e}")
        except Exception as e:
             logging.error(f"スラッシュコマンド同期中に予期せぬエラー: {e}", exc_info=True)


        # 永続Viewの登録 (ListManagementViewなど)
        # MemoCogがロードされていれば実行
        memo_cog = self.get_cog("MemoCog")
        if memo_cog:
             # MemoCog内でViewインスタンスを作成して追加する方が依存関係が明確
             # self.add_view(ListManagementView(memo_cog))
             logging.info("Persistent views from MemoCog should be registered.")
        else:
             logging.warning("MemoCog not loaded, cannot register persistent views.")


    async def on_ready(self):
        """Botの準備が完了したときの処理"""
        logging.info(f"{self.user} としてログインしました (ID: {self.user.id})")
        logging.info(f"discord.py version: {discord.__version__}")

        # オフライン中のメモを処理 (add_memo_async が利用可能な場合)
        if add_memo_async:
             # Dropbox接続情報が利用可能な場合のみ実行
             if all([DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET]):
                 await self.process_offline_memos()
             else:
                  logging.warning("Dropbox認証情報がないため、オフラインメモ処理をスキップします。")
        else:
             logging.warning("obsidian_handlerが見つからないため、オフラインメモ処理をスキップします。")

        logging.info("--- Bot is ready and listening for events ---")

    async def process_offline_memos(self):
        """オフライン中の未取得メモがないか確認し、処理する"""
        logging.info("オフライン中の未取得メモがないか確認します...")
        after_message_id = None
        dbx = None # Dropboxクライアント

        try:
            # Dropboxクライアントを初期化
            dbx = dropbox.Dropbox(
                oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
                app_key=DROPBOX_APP_KEY,
                app_secret=DROPBOX_APP_SECRET
            )
            # 最終処理IDファイルをダウンロード
            _, res = dbx.files_download(LAST_PROCESSED_ID_FILE_PATH)
            last_id_str = res.content.decode('utf-8').strip()
            if last_id_str:
                after_message_id = int(last_id_str)
                logging.info(f"Dropboxから最終処理ID: {last_id_str} を読み込みました。")
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.DownloadError) and e.error.get_path().is_not_found():
                logging.info("最終処理IDファイルが見つかりません。チャンネル履歴の最初から取得します (件数に注意)。")
                after_message_id = None # 最初から取得
            else:
                logging.error(f"Dropboxからの最終処理IDファイルの読み込みに失敗: {e}")
                return # Dropboxエラー時は処理中断
        except ValueError:
             logging.error(f"最終処理IDファイルの内容が無効です: {last_id_str}")
             return # IDが無効な場合は中断
        except Exception as e:
            logging.error(f"最終処理IDの読み込み中に予期せぬエラー: {e}")
            return # その他のエラー時も中断

        # メモチャンネルを取得
        channel = self.get_channel(MEMO_CHANNEL_ID)
        if not channel:
            logging.error(f"MEMO_CHANNEL_ID: {MEMO_CHANNEL_ID} のチャンネルが見つかりません。")
            return

        try:
            history = []
            limit = 1000 # 一度に取得する件数上限 (API制限考慮)
            logging.info(f"Fetching message history from channel {channel.name} after ID: {after_message_id}")

            # afterパラメータを使ってメッセージ履歴を取得
            # discord.py v2.x では after は datetime オブジェクトか Snowflake を受け取る
            after_obj = discord.Object(id=after_message_id) if after_message_id else None

            async for message in channel.history(limit=limit, after=after_obj, oldest_first=True):
                 history.append(message)

            if history:
                logging.info(f"{len(history)}件の未取得メモが見つかりました。保存します...")
                latest_processed_id = None
                for message in history: # 既に oldest_first=True で取得済み
                    if not message.author.bot:
                        # add_memo_async はファイルのロックと書き込みを行う
                        await add_memo_async(
                            content=message.content,
                            author=f"{message.author} ({message.author.id})",
                            created_at=message.created_at.isoformat(), # UTCで取得される
                            message_id=message.id
                        )
                        latest_processed_id = message.id # 最後に処理したIDを更新
                    # ログ出力は add_memo_async 内で行われる

                logging.info("未取得メモの保存が完了しました。")

                # 最後に処理したメッセージIDをDropboxに保存
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("プログラムが手動で終了されました。")