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

# --- Google Docs連携 (youtube_cog.py からコピー) ---
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("WebClipCog: Google Docs連携が有効です。")
except ImportError:
    logging.warning("WebClipCog: google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass
# --- ここまで ---

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# --- リアクション定数 (youtube_cog.py からコピー) ---
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
SAVE_ERROR_EMOJI = '💾'
GOOGLE_DOCS_ERROR_EMOJI = '🇬'
# --- ここまで ---


class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、Obsidianに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # .envファイルからDropboxの認証情報を読み込む
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))

        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("WebClipCog: Dropboxの認証情報が.envファイルに設定されていません。")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        Bot(自分自身)が付けた 📥 リアクションを検知して処理を開始する
        (youtube_cog.py からコピー・修正)
        """
        
        # WebClipチャンネル以外は無視
        if payload.channel_id != self.web_clip_channel_id:
            return
            
        emoji_str = str(payload.emoji)

        # 1. このリアクションはトリガー(📥)か？
        if emoji_str == BOT_PROCESS_TRIGGER_REACTION: # '📥'
            
            # 2. このリアクションは Bot (＝自分自身) が付けたものか？
            if payload.user_id != self.bot.user.id:
                return 
 
            # 3. メッセージを取得
            channel = self.bot.get_channel(payload.channel_id)
            if not channel: return
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden):
                logging.warning(f"メッセージの取得に失敗しました: {payload.message_id}")
                return

            # 4. 既に処理中/処理完了のリアクションを付けているか？
            is_processed = any(r.emoji in (
                PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, 
                SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
                ) and r.me for r in message.reactions)
            
            if is_processed:
                logging.info(f"既に処理中または処理済みのメッセージのためスキップします: {message.jump_url}")
                return

            # 5. 【処理実行】
            logging.info(f"Bot (self) の '{BOT_PROCESS_TRIGGER_REACTION}' を検知しました。WebClip処理を開始します: {message.jump_url}")
            
            # トリガー 📥 (Bot/自分 が付けたもの) を削除
            try:
                await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException:
                logging.warning(f"Bot のリアクション削除に失敗しました: {message.jump_url}")

            await self._perform_clip(url=message.content.strip(), message=message)

        # 6. それ以外 (人間が付けたリアクションなど)
        elif payload.user_id == self.bot.user.id:
            # 自分が付けた処理中・完了リアクションを検知しても何もしない
            return
        else:
            return

    async def _perform_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック"""
        # --- ★ 修正: エラーハンドリングとGDocs連携の準備 ---
        obsidian_save_success = False
        gdoc_save_success = False
        error_reactions = set()
        title = "Untitled"
        content_md = ""
        # --- ★ 修正ここまで ---

        try:
            # ★ 修正: PROCESS_START_EMOJI を使用
            await message.add_reaction(PROCESS_START_EMOJI)

            loop = asyncio.get_running_loop()
            title, content_md = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            if not safe_title:
                safe_title = "Untitled"

            now = datetime.datetime.now(JST)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            daily_note_date = now.strftime('%Y-%m-%d')

            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_name_for_link = webclip_file_name.replace('.md', '')

            webclip_note_content = (
                f"# {title}\n\n"
                f"- **Source:** <{url}>\n\n"
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{content_md}"
            )
            
            # --- ★ 修正: Dropboxクライアント初期化とエラーハンドリング (youtube_cog.py に合わせる) ---
            dbx = None
            if self.dropbox_refresh_token:
                try:
                    dbx = dropbox.Dropbox(
                        oauth2_refresh_token=self.dropbox_refresh_token,
                        app_key=self.dropbox_app_key,
                        app_secret=self.dropbox_app_secret
                    )
                except Exception as e_dbx:
                     logging.error(f"Dropboxクライアントの初期化に失敗: {e_dbx}")
                     error_reactions.add(SAVE_ERROR_EMOJI)
            else:
                logging.error("Dropboxリフレッシュトークンがありません。")
                error_reactions.add(SAVE_ERROR_EMOJI)

            # --- Obsidianへの保存 ---
            if dbx:
                try:
                    webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                    # ★ 修正: to_thread を使用
                    await asyncio.to_thread(
                        dbx.files_upload,
                        webclip_note_content.encode('utf-8'),
                        webclip_file_path,
                        mode=WriteMode('add')
                    )
                    logging.info(f"クリップ成功: {webclip_file_path}")
    
                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
    
                    try:
                        # ★ 修正: to_thread を使用
                        _, res = await asyncio.to_thread(dbx.files_download, daily_note_path)
                        daily_note_content = res.content.decode('utf-8')
                    except ApiError as e:
                        if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                            daily_note_content = "" # ★ 修正: 新規作成時は空
                            logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                        else:
                            raise # 他のApiErrorは上位でキャッチ
    
                    link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{title}]]" # ★ 修正: リンクパスとタイトル
                    webclips_heading = "## WebClips"
    
                    # --- ★ 修正: 自前のロジック (元々のコード) を使用 ---
                    lines = daily_note_content.split('\n')
                    try:
                        heading_index = -1
                        for i, line in enumerate(lines):
                            if line.strip().lstrip('#').strip() == webclips_heading.lstrip('#').strip():
                                heading_index = i
                                break
                        if heading_index == -1: raise ValueError("Header not found")
                        
                        insert_index = heading_index + 1
                        # 次の見出し (##) が来るまで進む
                        while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                            insert_index += 1
                        
                        # 挿入位置の直前が空行でない場合、空行を挿入
                        if insert_index > heading_index + 1 and lines[insert_index - 1].strip() != "":
                            lines.insert(insert_index, "")
                            insert_index += 1
                            
                        lines.insert(insert_index, link_to_add)
                        new_daily_content = "\n".join(lines)
                    except ValueError:
                        # 「## WebClips」セクションが存在しない場合は、末尾に追加
                        logging.info(f"Section '{webclips_heading}' not found in daily note, appending.")
                        new_daily_content = daily_note_content.strip() + f"\n\n{webclips_heading}\n{link_to_add}\n"
                    # --- ★ 修正ここまで (自前ロジック) ---
                    
                    # ★ 修正: to_thread を使用
                    await asyncio.to_thread(
                        dbx.files_upload,
                        new_daily_content.encode('utf-8'),
                        daily_note_path,
                        mode=WriteMode('overwrite')
                    )
                    logging.info(f"デイリーノートを更新しました: {daily_note_path}")
                    obsidian_save_success = True
                
                except ApiError as e_obs_api:
                    logging.error(f"Error saving to Obsidian (Dropbox API): {e_obs_api}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
                except Exception as e_obs_other:
                    logging.error(f"Error saving to Obsidian (Other): {e_obs_other}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
            # --- ★ 修正: Dropbox処理ブロックここまで ---

            # --- ★ 追加: Google Docsへの保存 (Goal 2) ---
            if google_docs_enabled:
                gdoc_text_to_append = ""
                gdoc_source_type = "WebClip Error"

                if content_md: # 抽出した本文
                    gdoc_text_to_append = content_md
                    gdoc_source_type = "WebClip Content"
                elif url: # 本文抽出失敗時はURLのみ
                    gdoc_text_to_append = "(本文の抽出に失敗しました)"
                    gdoc_source_type = "WebClip URL (Content Failed)"

                if gdoc_text_to_append:
                    try:
                        await append_text_to_doc_async(
                            text_to_append=gdoc_text_to_append,
                            source_type=gdoc_source_type,
                            url=url,
                            title=title
                        )
                        gdoc_save_success = True
                        logging.info(f"Data ({gdoc_source_type}) sent to Google Docs for {url}")
                    except Exception as e_gdoc:
                        logging.error(f"Failed to send data to Google Docs for {url}: {e_gdoc}", exc_info=True)
                        error_reactions.add(GOOGLE_DOCS_ERROR_EMOJI)
            # --- ★ 追加ここまで ---

            # --- ★ 修正: 最終リアクション ---
            if obsidian_save_success:
                await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                if error_reactions: # GDocエラーなど
                    for reaction in error_reactions:
                        try: await message.add_reaction(reaction)
                        except discord.HTTPException: pass
            else:
                final_reactions = error_reactions if error_reactions else {PROCESS_ERROR_EMOJI}
                for reaction in final_reactions:
                    try: await message.add_reaction(reaction)
                    except discord.HTTPException: pass
            # --- ★ 修正ここまで ---

        except Exception as e:
            logging.error(f"Webクリップ処理中にエラー: {e}", exc_info=True)
            # ★ 修正: PROCESS_ERROR_EMOJI を使用
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
            # ★ 修正: PROCESS_START_EMOJI を使用
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass

    # ★ 修正: on_message は memo_cog.py が担当するため、ここではコメントアウト
    # @commands.Cog.listener()
    # async def on_message(self, message: discord.Message):
    #     if message.author.bot or message.channel.id != self.web_clip_channel_id:
    #         return
    #     content = message.content.strip()
    #     if content.startswith('http') and URL_REGEX.match(content):
    #         url = content
    #         await self._perform_clip(url=url, message=message)
    # ★ 修正ここまで

    @app_commands.command(name="clip", description="URLをObsidianにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        # ★ 修正: youtube_cog.py に合わせたインタラクション制御
        await interaction.response.defer(ephemeral=False, thinking=True)
        message_proxy = await interaction.original_response()

        # TempMessage クラス (youtube_cog.py からコピー)
        class TempMessage:
             def __init__(self, proxy):
                 self.id = proxy.id; self.reactions = []; self.channel = proxy.channel; self.jump_url = proxy.jump_url; self._proxy = proxy; self.content=proxy.content
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji)
                 except: pass
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user)
                 except: pass

        await self._perform_clip(url=url, message=TempMessage(message_proxy))
        # ★ 修正ここまで

async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))