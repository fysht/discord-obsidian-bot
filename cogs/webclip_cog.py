import os
import discord
from discord import app_commands # スラッシュコマンドを使用する場合
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import datetime
import zoneinfo

# readabilityベースのパーサーをインポート
from web_parser import parse_url_with_readability
# --- Google Docs Handler Import (エラーハンドリング付き) ---
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("WebClipCog: Google Docs連携が有効です。")
except ImportError:
    logging.warning("WebClipCog: google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False
    # ダミー関数を定義
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass # 何もしない
# --- ここまで ---

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
    from datetime import timezone, timedelta
    JST = timezone(timedelta(hours=+9), "JST")

# Botが付与する処理開始トリガー
BOT_PROCESS_TRIGGER_REACTION = '📥'
# 処理ステータス用
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
GOOGLE_DOCS_ERROR_EMOJI = '🇬' # Google Docs連携エラー用

class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、ObsidianとGoogle Docsに保存するCog (リアクショントリガー)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # .envファイルから設定を読み込む
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        self.dbx = None
        self.is_ready = False # 初期化成功フラグ

        # 必須環境変数のチェック
        missing_vars = []
        if not self.web_clip_channel_id: missing_vars.append("WEB_CLIP_CHANNEL_ID")
        if not self.dropbox_app_key: missing_vars.append("DROPBOX_APP_KEY")
        if not self.dropbox_app_secret: missing_vars.append("DROPBOX_APP_SECRET")
        if not self.dropbox_refresh_token: missing_vars.append("DROPBOX_REFRESH_TOKEN")

        if missing_vars:
            logging.error(f"WebClipCog: 必要な環境変数 ({', '.join(missing_vars)}) が不足しています。Cogは動作しません。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret,
                timeout=60
            )
            self.dbx.users_get_current_account() # 接続テスト
            logging.info("WebClipCog: Dropbox client initialized successfully.")
            self.is_ready = True # Dropbox初期化成功で準備完了
        except Exception as e:
            logging.error(f"WebClipCog: Failed to initialize Dropbox client: {e}", exc_info=True)
            # is_ready は False のまま

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Botが付与したトリガーリアクションを検知して処理を開始"""
        # 必要なチェック
        if payload.channel_id != self.web_clip_channel_id: return
        if payload.user_id != self.bot.user.id: return
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        if not self.is_ready: # Cogが初期化されているか
            logging.error("WebClipCog: Cog is not ready. Cannot process clip.")
            return

        # 対象メッセージを取得
        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"Failed to fetch message {payload.message_id} for webclip processing.")
            return

        # メッセージ内容からURLを抽出
        content = message.content.strip()
        url_match = URL_REGEX.search(content)
        if not url_match:
            logging.warning(f"Webclip trigger on message {message.id} which does not contain a valid URL.")
            await message.add_reaction('❓')
            try: await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException: pass
            return
        url = url_match.group(0)

        # 既に処理中・処理済みでないか確認
        if any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI) and r.me for r in message.reactions):
            logging.info(f"Message {message.id} (URL: {url}) is already processed or in progress. Skipping.")
            try: await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException: pass
            return

        logging.info(f"Received webclip trigger for URL: {url} (Message ID: {message.id})")

        # Botが付与したトリガーリアクションを削除
        try: await message.remove_reaction(payload.emoji, self.bot.user)
        except discord.HTTPException: pass

        # ウェブクリップ処理を実行
        await self._perform_clip(url=url, message=message)


    async def _perform_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック (Google Docs保存追加)"""
        if not self.is_ready: # 再度チェック
            logging.error("Cannot perform web clip: WebClipCog is not ready.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        # 処理開始リアクション
        try: await message.add_reaction(PROCESS_START_EMOJI)
        except discord.HTTPException: pass

        title = "Untitled"
        content_md = '(Content could not be extracted)'
        obsidian_save_success = False
        gdoc_save_success = False
        error_reactions = set() # エラーリアクション保持用

        try:
            logging.info(f"Starting web clip process for {url}")
            loop = asyncio.get_running_loop()
            # web_parser を非同期実行
            title_result, content_md_result = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )
            logging.info(f"Readability finished for {url}. Title: '{title_result}', Content length: {len(content_md_result) if content_md_result else 0}")

            title = title_result if title_result and title_result != "No Title Found" else url
            content_md = content_md_result or content_md # Noneならデフォルトのまま

            # --- Obsidianへの保存 ---
            try:
                # ファイル名に使えない文字を除去・置換、長さ制限
                safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:100]
                if not safe_title: safe_title = "Untitled"

                now = datetime.datetime.now(JST)
                timestamp = now.strftime('%Y%m%d%H%M%S')
                daily_note_date = now.strftime('%Y-%m-%d')

                webclip_file_name = f"{timestamp}-{safe_title}.md"
                webclip_file_name_for_link = webclip_file_name.replace('.md', '')

                # 保存するMarkdownの内容
                webclip_note_content = (
                    f"# {title}\n\n"
                    f"- **Source:** <{url}>\n"
                    f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"[[{daily_note_date}]]\n\n"
                    f"---\n\n"
                    f"{content_md}"
                )

                webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"

                logging.info(f"Uploading web clip file to Dropbox: {webclip_file_path}")
                await asyncio.to_thread(
                    self.dbx.files_upload,
                    webclip_note_content.encode('utf-8'),
                    webclip_file_path,
                    mode=WriteMode('add')
                )
                logging.info(f"Webclip successfully saved to Obsidian: {webclip_file_path}")

                # --- デイリーノートへのリンク追加 ---
                daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                daily_note_content = ""
                try:
                    _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                    daily_note_content = res.content.decode('utf-8')
                except ApiError as e_dn:
                    if isinstance(e_dn.error, DownloadError) and e_dn.error.is_path() and e_dn.error.get_path().is_not_found():
                        daily_note_content = f"# {daily_note_date}\n" # 新規作成
                        logging.info(f"Daily note {daily_note_path} not found. Creating new.")
                    else: raise

                link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{title}]]"
                webclips_heading = "## WebClips" # utils.obsidian_utils がない場合の簡易追記ロジック

                # --- 簡易的な追記ロジック (update_section がない場合) ---
                lines = daily_note_content.split('\n')
                new_daily_content = ""
                try:
                    heading_index = -1
                    for i, line in enumerate(lines):
                        if line.strip() == webclips_heading:
                            heading_index = i
                            break
                    if heading_index == -1: raise ValueError

                    insert_index = heading_index + 1
                    while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                        insert_index += 1
                    if insert_index > heading_index + 1 and lines[insert_index - 1].strip() != "":
                        lines.insert(insert_index, "")
                        insert_index += 1
                    lines.insert(insert_index, link_to_add)
                    new_daily_content = "\n".join(lines)
                except ValueError:
                    new_daily_content = daily_note_content.strip() + f"\n\n{webclips_heading}\n{link_to_add}\n"
                # --- 簡易的な追記ロジックここまで ---

                await asyncio.to_thread(
                    self.dbx.files_upload,
                    new_daily_content.encode('utf-8'),
                    daily_note_path,
                    mode=WriteMode('overwrite')
                )
                logging.info(f"Daily note updated successfully: {daily_note_path}")
                obsidian_save_success = True

            except ApiError as e_obs:
                logging.error(f"Error saving to Obsidian (Dropbox API): {e_obs}", exc_info=True)
                error_reactions.add(PROCESS_ERROR_EMOJI) # 汎用エラー
            except Exception as e_obs_other:
                logging.error(f"Unexpected error saving to Obsidian: {e_obs_other}", exc_info=True)
                error_reactions.add(PROCESS_ERROR_EMOJI)

            # --- Google Docsへの保存 ---
            if google_docs_enabled:
                # Obsidian保存成功時のみ実行するか、常に試行するかは選択可能
                # ここでは常に試行する
                try:
                    # Google Docsに送信する内容 (Markdownではなくプレーンテキストが良い場合もある)
                    # ここではMarkdown本文をそのまま送る
                    gdoc_text_to_append = content_md
                    await append_text_to_doc_async(
                        text_to_append=gdoc_text_to_append,
                        source_type="WebClip",
                        url=url,
                        title=title
                    )
                    gdoc_save_success = True
                    logging.info(f"Webclip content successfully sent to Google Docs: {url}")
                except Exception as e_gdoc:
                    logging.error(f"Failed to send webclip content to Google Docs: {e_gdoc}", exc_info=True)
                    error_reactions.add(GOOGLE_DOCS_ERROR_EMOJI)
                    gdoc_save_success = False

            # --- 最終的なリアクション ---
            if obsidian_save_success: # Obsidian保存成功を基準
                if not error_reactions: # 他にエラーがなければ成功
                    await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    logging.info(f"Web clip process completed successfully for {url}")
                else:
                    # Obsidianは成功したが他でエラー
                    await message.add_reaction(PROCESS_COMPLETE_EMOJI) # Obsidian成功は示す
                    for reaction in error_reactions:
                        try: await message.add_reaction(reaction)
                        except discord.HTTPException: pass
                    logging.warning(f"Web clip process for {url} completed with errors: {error_reactions}")
            else:
                # Obsidian保存失敗
                final_reactions = error_reactions if error_reactions else {PROCESS_ERROR_EMOJI}
                for reaction in final_reactions:
                    try: await message.add_reaction(reaction)
                    except discord.HTTPException: pass
                logging.error(f"Web clip process failed for {url} (Obsidian save failed). Errors: {error_reactions}")


        except Exception as e: # _perform_clip 全体の予期せぬエラー
            logging.error(f"[Web Clip Error] Unexpected error during web clip process for ({url}): {e}", exc_info=True)
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
            # 処理中リアクションを削除
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass

    # --- 元のスラッシュコマンド ( InteractionMessage の扱いに注意が必要 ) ---
    @app_commands.command(name="clip", description="[手動] URLをObsidianとGoogle Docsにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip_command(self, interaction: discord.Interaction, url: str):
        if not self.is_ready:
            await interaction.response.send_message("❌ クリップ機能が初期化されていません。", ephemeral=True)
            return
        if not url.startswith(('http://', 'https://')):
             await interaction.response.send_message("❌ 無効なURL形式です。", ephemeral=True)
             return

        await interaction.response.defer(ephemeral=False, thinking=True) # thinking=Trueに変更
        message_proxy = await interaction.original_response()

        # _perform_clip は Message オブジェクトを期待するため、InteractionMessage ではリアクション操作が不安定になる可能性
        class TempMessage: # ダミークラス
             def __init__(self, proxy):
                 self.id = proxy.id; self.reactions = []; self.channel = proxy.channel; self.jump_url = proxy.jump_url; self._proxy = proxy
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji) # 試みる
                 except: pass # 失敗しても無視
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user) # 試みる
                 except: pass # 失敗しても無視

        await self._perform_clip(url=url, message=TempMessage(message_proxy))
        # 完了メッセージはリアクションで示すため、ここでは不要 (編集するとリアクションが見えなくなる)
        # await interaction.edit_original_response(content=f"クリップ処理を実行しました: {url}")


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if int(os.getenv("WEB_CLIP_CHANNEL_ID", 0)) == 0:
        logging.error("WebClipCog: WEB_CLIP_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    # インスタンス作成時に初期化成否をチェック
    cog_instance = WebClipCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("WebClipCog loaded successfully.")
    else:
        logging.error("WebClipCog failed to initialize properly and was not loaded.")
        del cog_instance # 初期化失敗時はインスタンスを削除