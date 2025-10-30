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
import aiohttp
import google.generativeai as genai
# --- 修正: 参考コードに合わせて get_transcript ではなく、fetchに必要なAPIをインポート ---
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
# --- 修正ここまで ---

# --- 共通関数インポート (utils/obsidian_utils.py を想定) ---
try:
    from utils.obsidian_utils import update_section
    logging.info("YouTubeCog: utils/obsidian_utils.py を読み込みました。")
except ImportError:
    logging.warning("YouTubeCog: utils/obsidian_utils.pyが見つからないため、簡易的な追記処理を使用します。")
    # 簡易版のダミー関数 (添付コード cogs/youtube_cog.py 内の _update_daily_note_section を流用)
    def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
        """デイリーノートの指定セクションに追記する簡易関数"""
        lines = current_content.split('\n')
        new_content_lines = list(lines)
        try:
            heading_index = -1
            for i, line in enumerate(new_content_lines):
                # ヘッダーレベルを問わず、テキストが一致するか確認
                if line.strip().lstrip('#').strip() == section_header.lstrip('#').strip():
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

# --- Google Docs連携 (添付コードのまま) ---
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("YouTubeCog: Google Docs連携が有効です。")
except ImportError:
    logging.warning("YouTubeCog: google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass
# --- ここまで ---

# --- 定数定義 (参考コード・添付コードより) ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
# memo_cog.py が付与するリアクション
BOT_PROCESS_TRIGGER_REACTION = '📥'
# 処理ステータス用
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
TRANSCRIPT_NOT_FOUND_EMOJI = '🔇' # 字幕なし
INVALID_URL_EMOJI = '❓' # 無効URL
SUMMARY_ERROR_EMOJI = '⚠️' # 要約失敗/タイムアウト
SAVE_ERROR_EMOJI = '💾' # Obsidian保存失敗
GOOGLE_DOCS_ERROR_EMOJI = '🇬' # Google Docs連携エラー
# --- ここまで ---

class YouTubeCog(commands.Cog):
    """YouTube動画の要約とObsidian/Google Docsへの保存を行うCog (Botリアクショントリガー)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数読み込み (添付コードのまま) ---
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        # --- クライアント初期化とチェック (添付コードのまま) ---
        self.dbx = None
        self.gemini_model = None
        self.session = None # aiohttp session
        self.is_ready = False

        missing_vars = []
        if not self.youtube_summary_channel_id: missing_vars.append("YOUTUBE_SUMMARY_CHANNEL_ID")
        if not self.dropbox_app_key: missing_vars.append("DROPBOX_APP_KEY")
        if not self.dropbox_app_secret: missing_vars.append("DROPBOX_APP_SECRET")
        if not self.dropbox_refresh_token: missing_vars.append("DROPBOX_REFRESH_TOKEN")
        if not self.gemini_api_key: missing_vars.append("GEMINI_API_KEY")

        if missing_vars:
            logging.error(f"YouTubeCog: 必要な環境変数 ({', '.join(missing_vars)}) が不足。Cogは動作しません。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret, timeout=300
            )
            self.dbx.users_get_current_account()
            logging.info("YouTubeCog: Dropbox client initialized.")

            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            logging.info("YouTubeCog: Gemini client initialized.")

            self.session = aiohttp.ClientSession()
            logging.info("YouTubeCog: aiohttp session started.")

            self.is_ready = True
        except Exception as e:
            logging.error(f"YouTubeCog: Failed to initialize clients: {e}", exc_info=True)


    async def cog_unload(self):
        """Cogアンロード時にセッションを閉じる"""
        if self.session and not self.session.closed:
            await self.session.close()
            logging.info("YouTubeCog: aiohttp session closed.")

    # --- 修正: _update_daily_note_section を utils.obsidian_utils の update_section を使うように変更 ---
    # (ImportError時にダミー関数が定義されるため、このCogは動作を継続できる)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        Botが付与したトリガーリアクションを検知して処理を開始
        (添付コードのロジックをそのまま使用。memo_cog.pyと連携)
        """
        # 必要なチェック
        if payload.channel_id != self.youtube_summary_channel_id: return
        # ★修正: Bot自身のリアクション（memo_cogが付けた📥）を検知する
        if payload.user_id != self.bot.user.id: return
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        if not self.is_ready:
            logging.error("YouTubeCog: Cog is not ready. Cannot process summary request.")
            return

        # 対象メッセージを取得
        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"Failed to fetch message {payload.message_id} for YouTube summary processing.")
            return

        # メッセージ内容からURLを抽出
        content = message.content.strip()
        url_match = YOUTUBE_URL_REGEX.search(content)
        if not url_match:
            logging.warning(f"YouTube summary trigger on message {message.id} which does not contain a valid YouTube URL.")
            await message.add_reaction(INVALID_URL_EMOJI)
            try: await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException: pass
            return
        url = url_match.group(0)

        # 既に処理中・処理済みでないか確認
        processed_emojis = {
            PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI,
            TRANSCRIPT_NOT_FOUND_EMOJI, INVALID_URL_EMOJI, SUMMARY_ERROR_EMOJI,
            SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
        }
        if any(r.emoji in processed_emojis and r.me for r in message.reactions):
            logging.info(f"Message {message.id} (URL: {url}) is already processed or in progress. Skipping.")
            try: await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException: pass
            return

        logging.info(f"Received YouTube summary trigger for URL: {url} (Message ID: {message.id})")

        # Botが付与したトリガーリアクションを削除
        try: await message.remove_reaction(payload.emoji, self.bot.user)
        except discord.HTTPException: pass

        # 要約処理を実行
        await self._perform_summary(url=url, message=message)

    # --- 修正: 参考コードの _extract_transcript_text を使用 ---
    def _extract_transcript_text(self, fetched_data):
        texts = []
        try:
            for snippet in fetched_data:
                if isinstance(snippet, dict):
                    texts.append(snippet.get('text', ''))
                elif hasattr(snippet, 'text'):
                    texts.append(getattr(snippet, 'text', ''))
                else:
                    texts.append(str(snippet))
            return " ".join(t.strip() for t in texts if t and t.strip())
        except TypeError:
            # 参考コードのTypeErrorハンドリング（リストの場合のフォールバック）
            if isinstance(fetched_data, list):
                for item in fetched_data:
                        if isinstance(item, dict):
                            texts.append(item.get('text', ''))
                return " ".join(t.strip() for t in texts if t and t.strip())
            
            logging.warning(f"予期せぬ字幕データ形式のため、テキスト抽出に失敗しました: {type(fetched_data)}")
            return ""
    # --- 修正ここまで ---

    # --- 参考コードの process_pending_summaries を追加 ---
    async def process_pending_summaries(self):
        """起動時などに未処理の要約リクエストをまとめて処理する関数"""
        channel = self.bot.get_channel(self.youtube_summary_channel_id)
        if not channel:
            logging.error(f"YouTubeCog: チャンネルID {self.youtube_summary_channel_id} が見つかりません。")
            return

        logging.info(f"チャンネル '{channel.name}' の未処理YouTube要約をスキャンします...")
        
        pending_messages = []
        async for message in channel.history(limit=200):
            # BOT_PROCESS_TRIGGER_REACTION（📥）で判定するように修正
            has_pending_reaction = any(r.emoji == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions)
            if has_pending_reaction:
                is_processed = any(r.emoji in (PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, PROCESS_START_EMOJI, TRANSCRIPT_NOT_FOUND_EMOJI) and r.me for r in message.reactions)
                if not is_processed:
                    pending_messages.append(message)
        
        if not pending_messages:
            logging.info("処理対象の新しいYouTube要約はありませんでした。")
            return

        logging.info(f"{len(pending_messages)}件の未処理YouTube要約が見つかりました。古いものから順に処理します...")
        for message in reversed(pending_messages):
            logging.info(f"処理開始: {message.jump_url}")
            url = message.content.strip()

            try:
                # Botが付けたリアクションをBotが消す
                await message.remove_reaction(BOT_PROCESS_TRIGGER_REACTION, self.bot.user)
            except (discord.Forbidden, discord.NotFound):
                logging.warning(f"リアクションの削除に失敗しました: {message.jump_url}")
            
            await self._perform_summary(url=url, message=message)
            await asyncio.sleep(5) # 連続処理のための待機
    # --- 修正ここまで ---


    async def _perform_summary(self, url: str, message: discord.Message | discord.InteractionMessage):
        """YouTube要約処理のコアロジック (字幕取得を参考コードベースに修正)"""
        obsidian_save_success = False
        gdoc_save_success = False
        error_reactions = set()
        video_title = "Untitled Video"
        video_id = None
        transcript_text = "" # ★ transcript_text を try の外で初期化

        try:
            if isinstance(message, discord.Message):
                try: await message.add_reaction(PROCESS_START_EMOJI)
                except discord.HTTPException: pass

            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                if isinstance(message, discord.Message): error_reactions.add(INVALID_URL_EMOJI)
                raise ValueError("Invalid YouTube URL")
            video_id = video_id_match.group(1)

            # --- 修正: 字幕取得ロジックを参考コードベースに変更 ---
            try:
                fetched = await asyncio.to_thread(
                    YouTubeTranscriptApi().fetch, # ★ 参考コードの fetch() を使用
                    video_id,
                    languages=['ja', 'en']
                )
                transcript_text = self._extract_transcript_text(fetched)
                if not transcript_text:
                     logging.warning(f"字幕テキストが空でした (Video ID: {video_id})")
                     if isinstance(message, discord.Message): error_reactions.add(TRANSCRIPT_NOT_FOUND_EMOJI)
                     # ★ 参考コードと異なり、続行してGDocsにエラーを保存する

            except (TranscriptsDisabled, NoTranscriptFound) as e:
                logging.warning(f"字幕取得失敗 (Video ID: {video_id}): {e}")
                if isinstance(message, discord.Message): error_reactions.add(TRANSCRIPT_NOT_FOUND_EMOJI)
                # ★ 参考コードと異なり、続行
            except Exception as e_trans:
                logging.error(f"字幕取得中に予期せぬエラー (Video ID: {video_id}): {e_trans}", exc_info=True)
                if isinstance(message, discord.Message): error_reactions.add(PROCESS_ERROR_EMOJI)
                # ★ 参考コードと異なり、続行
            # --- 修正ここまで ---


            # --- AI要約 (添付コードのロジックを流用) ---
            concise_summary = "(要約対象なし)"
            detail_summary = "(要約対象なし)"
            if transcript_text and self.gemini_model:
                try:
                    concise_prompt = (
                        "以下のYouTube動画の文字起こし全文を元に、重要なポイントを3～5点で簡潔にまとめてください。\n"
                        "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                        f"--- 文字起こし全文 ---\n{transcript_text}"
                    )
                    detail_prompt = (
                        "以下のYouTube動画の文字起こし全文を元に、その内容を網羅する詳細で包括的な要約を作成してください。\n"
                        "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                        f"--- 文字起こし全文 ---\n{transcript_text}"
                    )
                    tasks = [
                        self.gemini_model.generate_content_async(concise_prompt),
                        self.gemini_model.generate_content_async(detail_prompt)
                    ]
                    responses = await asyncio.gather(*[asyncio.wait_for(task, timeout=300) for task in tasks], return_exceptions=True)

                    if isinstance(responses[0], (Exception, asyncio.TimeoutError)):
                         concise_summary = f"(簡潔な要約エラー: {type(responses[0]).__name__})"
                         error_reactions.add(SUMMARY_ERROR_EMOJI)
                    elif hasattr(responses[0], 'text'): concise_summary = responses[0].text
                    else: concise_summary = "(簡潔な要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                    if isinstance(responses[1], (Exception, asyncio.TimeoutError)):
                         detail_summary = f"(詳細な要約エラー: {type(responses[1]).__name__})"
                         error_reactions.add(SUMMARY_ERROR_EMOJI)
                    elif hasattr(responses[1], 'text'): detail_summary = responses[1].text
                    else: detail_summary = "(詳細な要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                    if not error_reactions.intersection({SUMMARY_ERROR_EMOJI}): logging.info(f"AI summaries generated for {video_id}")

                except Exception as e_gather:
                    logging.error(f"AI summary gather failed: {e_gather}", exc_info=True)
                    concise_summary = detail_summary = "(AI要約プロセスエラー)"
                    if isinstance(message, discord.Message): error_reactions.add(SUMMARY_ERROR_EMOJI)

            elif not self.gemini_model: concise_summary = detail_summary = "(AI要約機能無効)"; error_reactions.add(SUMMARY_ERROR_EMOJI)
            # ★ 添付コードの「(字幕なしのため要約不可)」エラーメッセージ
            elif not transcript_text: concise_summary = detail_summary = "(字幕なしのため要約不可)"
            # --- AI要約ここまで ---


            # --- 保存準備 (添付コードのまま) ---
            now = datetime.datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')
            video_info = await self.get_video_info(video_id)
            video_title = video_info.get("title", f"YouTube_{video_id}")
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)[:100]
            if not safe_title: safe_title = f"YouTube_{video_id}"
            note_filename = f"{timestamp}-{safe_title}.md"
            note_filename_for_link = note_filename.replace('.md', '')

            # --- Obsidian用ノート内容 (添付コードのまま) ---
            note_content = (
                f"# {video_title}\n\n"
                f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>\n\n'
                f"- **URL:** {url}\n"
                f"- **Channel:** {video_info.get('author_name', 'N/A')}\n"
                f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"---\n\n"
                f"## Concise Summary\n{concise_summary}\n\n"
                f"## Detailed Summary\n{detail_summary}\n\n"
            )

            # --- Obsidianへの保存 (添付コードのまま、ただし utils.obsidian_utils を使用) ---
            if self.dbx:
                try:
                    note_path = f"{self.dropbox_vault_path}/YouTube/{note_filename}"
                    await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
                    logging.info(f"Summary saved to Obsidian note: {note_path}")

                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                    daily_note_content = ""
                    try:
                        _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                        daily_note_content = res.content.decode('utf-8')
                    except ApiError as e_dn:
                        if isinstance(e_dn.error, DownloadError) and e_dn.error.is_path() and e_dn.error.get_path().is_not_found():
                            daily_note_content = f"# {daily_note_date}\n"
                        else: raise

                    link_to_add = f"- [[YouTube/{note_filename_for_link}|{video_title}]]"
                    youtube_heading = "## YouTube Summaries"
                    # ★ 共通関数 update_section を使用
                    new_daily_content = update_section(daily_note_content, link_to_add, youtube_heading)

                    await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
                    logging.info(f"Daily note updated with YouTube link: {daily_note_path}")
                    obsidian_save_success = True

                except ApiError as e_obs_api:
                    logging.error(f"Error saving to Obsidian (Dropbox API): {e_obs_api}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
                except Exception as e_obs_other:
                    logging.error(f"Error saving to Obsidian (Other): {e_obs_other}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
            else:
                logging.error("Dropbox client not available. Skipping Obsidian save.")
                error_reactions.add(SAVE_ERROR_EMOJI)

            # --- Google Docsへの保存 (添付コードのまま) ---
            if google_docs_enabled:
                gdoc_text_to_append = ""
                gdoc_source_type = "YouTube Error"
                if transcript_text:
                    gdoc_text_to_append = transcript_text
                    gdoc_source_type = "YouTube Transcript"
                elif video_id:
                    error_reason = "(字幕なしまたは取得失敗)"
                    if TRANSCRIPT_NOT_FOUND_EMOJI in error_reactions: error_reason = "(字幕なしまたは取得失敗)"
                    if PROCESS_ERROR_EMOJI in error_reactions: error_reason = "(字幕取得エラー)"
                    gdoc_text_to_append = error_reason
                    gdoc_source_type = "YouTube Link (No Transcript)"

                if gdoc_text_to_append:
                    try:
                        await append_text_to_doc_async(
                            text_to_append=gdoc_text_to_append,
                            source_type=gdoc_source_type,
                            url=url,
                            title=video_title
                        )
                        gdoc_save_success = True
                        logging.info(f"Data ({gdoc_source_type}) sent to Google Docs for {url}")
                    except Exception as e_gdoc:
                        logging.error(f"Failed to send data to Google Docs for {url}: {e_gdoc}", exc_info=True)
                        error_reactions.add(GOOGLE_DOCS_ERROR_EMOJI)

            # --- 最終リアクション (添付コードのまま) ---
            if isinstance(message, discord.Message):
                if obsidian_save_success:
                    if not error_reactions:
                        await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    else:
                        await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                        for reaction in error_reactions:
                            try: await message.add_reaction(reaction)
                            except discord.HTTPException: pass
                else:
                    final_reactions = error_reactions if error_reactions else {PROCESS_ERROR_EMOJI}
                    for reaction in final_reactions:
                        try: await message.add_reaction(reaction)
                        except discord.HTTPException: pass

        except ValueError as e_val:
             logging.error(f"Processing stopped due to ValueError: {e_val}")
        except Exception as e:
            logging.error(f"YouTube要約処理全体でエラー: {e}", exc_info=True)
            if isinstance(message, discord.Message):
                try: await message.add_reaction(PROCESS_ERROR_EMOJI)
                except discord.HTTPException: pass
            elif isinstance(message, discord.InteractionMessage):
                interaction = getattr(message, 'interaction', None)
                if interaction:
                    try: await interaction.followup.send(f"❌ 処理中に予期せぬエラー: `{type(e).__name__}`", ephemeral=True)
                    except discord.HTTPException: pass

            if google_docs_enabled:
                try:
                    error_text = f"YouTube処理全体のエラー\nURL: {url}\nError: {type(e).__name__}: {e}"
                    title_for_error = video_title if video_title != "Untitled Video" else f"YouTube_{video_id or 'UnknownID'}"
                    await append_text_to_doc_async(error_text, "YouTube Processing Error", url, title_for_error)
                except Exception as e_gdoc_err:
                     logging.error(f"Failed to record YouTube processing error to Google Docs: {e_gdoc_err}")

        finally:
            if isinstance(message, discord.Message):
                try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass

    # --- スラッシュコマンド (添付コードのまま) ---
    @app_commands.command(name="yt_summary", description="[手動] YouTube動画URLをObsidian/Google Docsに保存します。")
    @app_commands.describe(url="処理したいYouTube動画のURL")
    async def yt_summary_command(self, interaction: discord.Interaction, url: str):
        if not self.is_ready:
             await interaction.response.send_message("❌ YouTube Cogが初期化されていません。", ephemeral=True)
             return

        await interaction.response.defer(ephemeral=False, thinking=True)
        message_proxy = await interaction.original_response()

        # ダミークラス（添付コードのまま）
        class TempMessage:
             def __init__(self, proxy):
                 self.id = proxy.id; self.reactions = []; self.channel = proxy.channel; self.jump_url = proxy.jump_url; self._proxy = proxy; self.content=proxy.content
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji)
                 except: pass
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user)
                 except: pass

        await self._perform_summary(url=url, message=TempMessage(message_proxy))
        # 完了・エラーのフィードバックはリアクションで行われる

    # --- get_video_info (添付コードのまま) ---
    async def get_video_info(self, video_id: str) -> dict:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
            async with self.session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        title = data.get("title")
                        author_name = data.get("author_name")
                        if title and author_name:
                            return {"title": title, "author_name": author_name}
                        else:
                            logging.warning(f"oEmbed missing title/author for {video_id}. Data: {data}")
                            return {"title": f"YouTube_{video_id}", "author_name": "N/A"}
                    except aiohttp.ContentTypeError:
                         text = await response.text()
                         logging.warning(f"oEmbed response not JSON for {video_id}. Text: {text[:100]}")
                         return {"title": f"YouTube_{video_id}", "author_name": "N/A"}
                else:
                    text = await response.text()
                    logging.warning(f"oEmbed failed: Status {response.status} for {video_id}. Text: {text[:100]}")
                    return {"title": f"YouTube_{video_id}", "author_name": "N/A"}
        except asyncio.TimeoutError:
            logging.warning(f"oEmbed request timed out for {video_id}")
        except aiohttp.ClientError as e:
            logging.warning(f"oEmbed client error for {video_id}: {e}")
        except Exception as e:
            logging.warning(f"oEmbed unexpected error for {video_id}: {e}")
        return {"title": f"YouTube_{video_id}", "author_name": "N/A"}


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0)) == 0:
        logging.error("YouTubeCog: YOUTUBE_SUMMARY_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    cog_instance = YouTubeCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("YouTubeCog loaded successfully.")
    else:
        logging.error("YouTubeCog failed to initialize properly and was not loaded.")
        del cog_instance