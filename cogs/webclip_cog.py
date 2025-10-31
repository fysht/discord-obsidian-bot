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

# --- utils.obsidian_utils のインポート (フォールバック付き) ---
try:
    from utils.obsidian_utils import update_section
    logging.info("WebClipCog: utils/obsidian_utils.py を読み込みました。")
except ImportError:
    logging.warning("WebClipCog: utils/obsidian_utils.py が見つかりません。簡易的な追記ロジックを使用します。")
    # 簡易ダミー関数 (フォールバック)
    def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
        lines = current_content.split('\n')
        new_content_lines = list(lines)
        try:
            heading_index = -1
            for i, line in enumerate(new_content_lines):
                 # 見出しレベルを問わず、テキスト部分が一致するか確認
                if line.strip().lstrip('#').strip().lower() == section_header.lstrip('#').strip().lower():
                    heading_index = i
                    break
            if heading_index == -1: raise ValueError("Header not found")
            
            insert_index = heading_index + 1
            while insert_index < len(new_content_lines) and not new_content_lines[insert_index].strip().startswith('## '):
                insert_index += 1
            if insert_index > heading_index + 1 and new_content_lines[insert_index - 1].strip() != "":
                new_content_lines.insert(insert_index, "")
                insert_index += 1
            new_content_lines.insert(insert_index, text_to_add)
            return "\n".join(new_content_lines) 
        except ValueError:
            logging.info(f"Section '{section_header}' not found in daily note, appending.")
            return current_content.strip() + f"\n\n{section_header}\n{text_to_add}\n"
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
        
        self.is_ready = True # 環境変数があれば準備完了とみなす
        logging.info("WebClipCog: 環境変数をロードしました。")


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Botが付与したトリガーリアクション(📥)を検知して処理を開始"""
        # 必要なチェック
        if payload.channel_id != self.web_clip_channel_id: return
        if payload.user_id != self.bot.user.id: return 
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        
        if not self.is_ready: # Cogが環境変数などで準備OKか
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
        """Webクリップのコアロジック (参考コードベースのDropbox処理 + Google Docs)"""
        
        if not self.is_ready:
            logging.error("Cannot perform web clip: WebClipCog is not ready (missing env vars).")
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
            
            # 1. Webパーサーの実行
            title_result, content_md_result = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )
            logging.info(f"Readability finished for {url}. Title: '{title_result}', Content length: {len(content_md_result) if content_md_result else 0}")

            title = title_result if title_result and title_result != "No Title Found" else url
            content_md = content_md_result or content_md

            # 2. ファイル名とノート内容の準備
            # ★ 修正: 参考コード (ファイル28) に合わせ、禁止文字を "" (空文字) に置換
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:100] 
            if not safe_title: safe_title = "Untitled"

            now = datetime.datetime.now(JST)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            daily_note_date = now.strftime('%Y-%m-%d')

            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_name_for_link = webclip_file_name.replace('.md', '')

            # ★ 修正: 参考コード (ファイル28) のフォーマットに合わせる
            webclip_note_content = (
                f"# {title}\n\n"
                f"- **Source:** <{url}>\n\n"
                f"---\n\n" # 参考コードには Clipped: がないため削除
                f"[[{daily_note_date}]]\n\n"
                f"{content_md}"
            )

            # 3. Obsidianへの保存 (★ 修正: 参考コード (ファイル28) に基づき `with` ブロックを使用)
            try:
                logging.info("Initializing Dropbox client for webclip (using 'with' statement)...")
                # dbx クライアントを `with` で初期化
                with dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret,
                    timeout=60 # タイムアウト設定
                ) as dbx:
                    
                    webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                    
                    logging.info(f"Uploading web clip file to Dropbox: {webclip_file_path}")
                    # `with` ブロック内では `dbx` は同期的に動作するため `loop.run_in_executor` を使用
                    await loop.run_in_executor(
                        None,
                        dbx.files_upload,
                        webclip_note_content.encode('utf-8'),
                        webclip_file_path,
                        mode=WriteMode('add')
                    )
                    logging.info(f"Webclip successfully saved to Obsidian: {webclip_file_path}")

                    # --- デイリーノートへのリンク追加 ---
                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                    daily_note_content = ""
                    try:
                        _, res = dbx.files_download(daily_note_path) # 同期
                        daily_note_content = res.content.decode('utf-8')
                        logging.info(f"Daily note {daily_note_path} downloaded.")
                    except ApiError as e_dn:
                        if isinstance(e_dn.error, DownloadError) and e_dn.error.is_path() and e_dn.error.get_path().is_not_found():
                            daily_note_content = "" # ★ 修正: 参考コード (ファイル28) に合わせ、新規作成時は空
                            logging.info(f"Daily note {daily_note_path} not found. Creating new.")
                        else: raise

                    # ★ 修正: 参考コード (ファイル28) に合わせ、リンク表示名を指定しない
                    link_to_add = f"- [[WebClips/{webclip_file_name_for_link}]]" 
                    webclips_heading = "## WebClips"
                    
                    # ★ 修正: utils.obsidian_utils.py の update_section を使用
                    # (※参考コードのロジックは不完全なため、より堅牢なこちらを採用します)
                    new_daily_content = update_section(daily_note_content, link_to_add, webclips_heading)

                    await loop.run_in_executor(
                        None,
                        dbx.files_upload,
                        new_daily_content.encode('utf-8'),
                        daily_note_path,
                        mode=WriteMode('overwrite')
                    )
                    logging.info(f"Daily note updated successfully: {daily_note_path}")
                    obsidian_save_success = True

            except ApiError as e_obs:
                logging.error(f"Error saving to Obsidian (Dropbox API): {e_obs}", exc_info=True)
                error_reactions.add(PROCESS_ERROR_EMOJI)
            except Exception as e_obs_other:
                logging.error(f"Unexpected error saving to Obsidian: {e_obs_other}", exc_info=True)
                error_reactions.add(PROCESS_ERROR_EMOJI)

            # 4. Google Docsへの保存 (新機能)
            if google_docs_enabled:
                try:
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

            # 5. 最終的なリアクション
            if obsidian_save_success:
                if not error_reactions:
                    await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    logging.info(f"Web clip process completed successfully for {url}")
                else:
                    await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    for reaction in error_reactions:
                        try: await message.add_reaction(reaction)
                        except discord.HTTPException: pass
                    logging.warning(f"Web clip process for {url} completed with errors: {error_reactions}")
            else:
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

    # --- スラッシュコマンド (手動実行用) ---
    @app_commands.command(name="clip", description="[手動] URLをObsidianとGoogle Docsにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip_command(self, interaction: discord.Interaction, url: str):
        if not self.is_ready:
            await interaction.response.send_message("❌ クリップ機能が初期化されていません。", ephemeral=True)
            return
        if not url.startswith(('http://', 'https://')):
             await interaction.response.send_message("❌ 無効なURL形式です。", ephemeral=True)
             return

        await interaction.response.defer(ephemeral=False, thinking=True) 
        message_proxy = await interaction.original_response()

        # _perform_clip は Message オブジェクトを期待するため、ダミークラスを使用
        class TempMessage:
             def __init__(self, proxy):
                 self.id = proxy.id; self.reactions = []; self.channel = proxy.channel; self.jump_url = proxy.jump_url; self._proxy = proxy
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji) 
                 except: pass 
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user) 
                 except: pass

        await self._perform_clip(url=url, message=TempMessage(message_proxy))


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if int(os.getenv("WEB_CLIP_CHANNEL_ID", 0)) == 0:
        logging.error("WebClipCog: WEB_CLIP_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    
    cog_instance = WebClipCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("WebClipCog loaded successfully.")
    else:
        logging.error("WebClipCog failed to initialize properly and was not loaded.")
        del cog_instance