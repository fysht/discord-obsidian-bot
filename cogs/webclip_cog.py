import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import datetime
import zoneinfo

from web_parser import parse_url_with_readability
from utils.obsidian_utils import update_section
# Google Docs Handlerをインポート
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
except ImportError:
    logging.warning("google_docs_handler.pyが見つからないため、WebClipのGoogle Docs連携は無効です。")
    google_docs_enabled = False

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
JST = zoneinfo.ZoneInfo("Asia/Tokyo") # JSTを定義

class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、ObsidianとGoogle Docsに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("WebClipCog: Dropboxの認証情報が.envファイルに設定されていません。")

    async def perform_clip_async(self, url: str, message: discord.Message | discord.InteractionMessage):
        """Webクリップのコアロジック (非同期版)"""
        obsidian_save_success = False
        clipped_content = ""
        clipped_title = "Untitled"
        start_time = datetime.datetime.now() # 処理開始時間
        try:
            # InteractionMessageの場合は interaction を取得
            interaction = getattr(message, 'interaction', None)

            # メッセージへのリアクションまたはThinking状態
            if isinstance(message, discord.Message):
                await message.add_reaction("⏳")
            elif interaction and not interaction.response.is_done():
                 # スラッシュコマンドからの場合、deferされているはずなので何もしない
                 # もし send_message から呼ばれた場合は thinking=True で defer する
                 # ここでは message が original_response である前提
                 pass

            loop = asyncio.get_running_loop()
            title, content_md = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )
            clipped_title = title if title else "Untitled"
            clipped_content = content_md if content_md else "(コンテンツの取得に失敗しました)"
            logging.info(f"Readability completed for {url}. Title: {clipped_title[:50]}...")

            safe_title = re.sub(r'[\\/*?:"<>|]', "_", clipped_title) # 不適切文字をアンダースコアに置換
            if not safe_title:
                safe_title = "Untitled"
            # タイトルが長すぎる場合も考慮 (ファイル名上限対策)
            safe_title = safe_title[:100]


            now = datetime.datetime.now(JST) # datetime を使用
            timestamp = now.strftime('%Y%m%d%H%M%S')
            daily_note_date = now.strftime('%Y-%m-%d')

            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_name_for_link = webclip_file_name.replace('.md', '')

            webclip_note_content = (
                f"# {clipped_title}\n\n"
                f"- **Source:** <{url}>\n"
                f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{clipped_content}"
            )

            # --- Obsidianへの保存 ---
            dbx = dropbox.Dropbox( # with文を使わずインスタンス化
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret,
                timeout=300 # タイムアウトを延長 (任意)
            )
            webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"

            try:
                # files_upload を非同期実行
                await asyncio.to_thread(
                    dbx.files_upload,
                    webclip_note_content.encode('utf-8'),
                    webclip_file_path,
                    mode=WriteMode('add') # dropbox.files を使わない
                )
                logging.info(f"クリップ成功 (Obsidian): {webclip_file_path}")

                daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                daily_note_content = ""
                try:
                    # files_download を非同期実行
                    metadata, res = await asyncio.to_thread(dbx.files_download, daily_note_path)
                    daily_note_content = res.content.decode('utf-8')
                except ApiError as e:
                    if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): # dropbox.files を使わない
                        logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                        daily_note_content = f"# {daily_note_date}\n\n" # 新規作成時の基本内容
                    else:
                        logging.error(f"デイリーノートのダウンロードエラー: {e}")
                        raise # 保存処理を中断させる

                link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{clipped_title}]]" # タイトルもリンクに追加
                webclips_heading = "## WebClips"

                new_daily_content = update_section(
                    daily_note_content, link_to_add, webclips_heading
                )

                # files_upload を非同期実行
                await asyncio.to_thread(
                    dbx.files_upload,
                    new_daily_content.encode('utf-8'),
                    daily_note_path,
                    mode=WriteMode('overwrite') # dropbox.files を使わない
                )
                logging.info(f"デイリーノート更新成功 (Obsidian): {daily_note_path}")
                obsidian_save_success = True

            except ApiError as e:
                 logging.error(f"Obsidianへの保存中にDropbox APIエラー: {e}", exc_info=True)
                 # エラーリアクションを追加
                 if isinstance(message, discord.Message): await message.add_reaction("⚠️")
                 # Google Docs への送信は試みない
                 obsidian_save_success = False # 失敗フラグ

            # --- Google Docsへの追記 ---
            if obsidian_save_success and google_docs_enabled:
                 try:
                    await append_text_to_doc_async(
                        text_to_append=clipped_content,
                        source_type="Web Clip",
                        url=url,
                        title=clipped_title
                    )
                    logging.info(f"クリップ成功 (Google Docs): {url}")
                 except Exception as e_gdoc:
                     logging.error(f"Failed to send web clip to Google Docs: {e_gdoc}", exc_info=True)
                     # Google Docs失敗のリアクション (任意)
                     if isinstance(message, discord.Message): await message.add_reaction("🇬️") # Googleアイコンの代わり

            # 成功リアクション (Obsidian保存成功が基準)
            if obsidian_save_success:
                 if isinstance(message, discord.Message): await message.add_reaction("✅")

        except Exception as e:
            logging.error(f"Webクリップ処理中に予期せぬエラー: {e}", exc_info=True)
            if isinstance(message, discord.Message): await message.add_reaction("❌")
            # スラッシュコマンドの場合、エラーメッセージを編集で表示
            if interaction:
                 error_msg = f"❌ クリップ処理中にエラーが発生しました: `{e}`"
                 try:
                     # interaction.edit_original_response は message に対して行う
                     await message.edit(content=error_msg)
                 except discord.HTTPException:
                     # 編集に失敗した場合 (e.g., メッセージ削除済み) はログのみ
                     logging.warning("Failed to edit interaction message for error.")
                 except Exception as edit_e:
                     logging.error(f"Error editing interaction message: {edit_e}")


            # エラー時もGoogle Docsにエラー情報を記録する (任意)
            if google_docs_enabled:
                try:
                    error_text = f"Webクリップ処理中にエラーが発生しました。\nURL: {url}\nError: {e}"
                    await append_text_to_doc_async(error_text, "Web Clip Error", url, clipped_title)
                except Exception: pass
        finally:
            end_time = datetime.datetime.now()
            duration = (end_time - start_time).total_seconds()
            logging.info(f"Web clip process finished for {url}. Duration: {duration:.2f} seconds.")
            if isinstance(message, discord.Message):
                try: # リアクション削除のエラーハンドリング
                    await message.remove_reaction("⏳", self.bot.user)
                except discord.HTTPException:
                    pass # 削除に失敗しても処理は続ける


    @app_commands.command(name="clip", description="URLをObsidianとGoogle Docsにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        # ephemeral=False にして、処理中メッセージが見えるように変更
        # thinking=True で defer し、応答メッセージは perform_clip_async 内で編集する
        await interaction.response.defer(ephemeral=False, thinking=True)
        # original_response() は InteractionMessage を返す
        message = await interaction.original_response()
        # perform_clip_async を呼び出す
        await self.perform_clip_async(url=url, message=message)
        # 完了メッセージを編集で表示 (成功・失敗はリアクションで判断)
        try:
            # 成功・失敗リアクションが付与されるので、メッセージはシンプルに
            await interaction.edit_original_response(content=f"`{url}` のクリップ処理が完了しました。")
        except discord.HTTPException as e:
            logging.warning(f"Failed to edit original response after clip command: {e}")
        except Exception as e_edit:
             logging.error(f"Error editing original response after clip command: {e_edit}")


async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))