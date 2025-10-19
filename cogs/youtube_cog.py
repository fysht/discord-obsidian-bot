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
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript

from utils.obsidian_utils import update_section
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
except ImportError:
    logging.warning("google_docs_handler.pyが見つからないため、YouTube要約のGoogle Docs連携は無効です。")
    google_docs_enabled = False

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
TRIGGER_EMOJI = '📥'
# --- メモチャンネルIDを使用 ---
TARGET_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))


class YouTubeCog(commands.Cog):
    """YouTube動画の要約とObsidian/Google Docsへの保存を行うCog (リアクショントリガー)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        if not self.gemini_api_key:
            logging.warning("YouTubeCog: GEMINI_API_KEYが設定されていません。AI要約は無効になります。")
            self.gemini_model = None
        else:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")

        self.session = aiohttp.ClientSession()

        # チャンネルIDのチェック
        if TARGET_CHANNEL_ID == 0:
             logging.error("MEMO_CHANNEL_IDが設定されていません。YouTubeCogは動作しません。")

    async def cog_unload(self):
        """Cogアンロード時にセッションを閉じる"""
        if self.session and not self.session.closed:
            await self.session.close()
            logging.info("YouTubeCog: aiohttp session closed.")

    # --- on_raw_reaction_add リスナー ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """特定のリアクションが付与された際に動画要約処理を開始するイベントリスナー"""
        # --- 条件チェック ---
        if payload.channel_id != TARGET_CHANNEL_ID: return
        if payload.user_id == self.bot.user.id: return
        if str(payload.emoji) != TRIGGER_EMOJI: return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            logging.error(f"Cannot find channel with ID {payload.channel_id}")
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.warning(f"メッセージの取得に失敗しました: {payload.message_id}")
            return

        # 既に処理中・処理済みのリアクションがないか確認
        processing_emojis = ('⏳', '✅', '❌', '🔇', '❓', '⚠️', '💾', '🇬️')
        is_already_processed = any(r.emoji in processing_emojis and r.me for r in message.reactions)
        if is_already_processed:
            logging.info(f"既に処理中または処理済みのメッセージのためスキップします: {message.jump_url}")
            try: # ユーザーが付けた📥リアクションは消しておく
                user = await self.bot.fetch_user(payload.user_id)
                await message.remove_reaction(payload.emoji, user)
            except discord.HTTPException: pass
            return

        # メッセージ内容がYouTube URLか確認
        content = message.content.strip()
        if not YOUTUBE_URL_REGEX.search(content):
             logging.info(f"Reaction added to non-YouTube link message, skipping: {message.jump_url}")
             try: # ユーザーが付けた📥リアクションは消しておく
                user = await self.bot.fetch_user(payload.user_id)
                await message.remove_reaction(payload.emoji, user)
             except discord.HTTPException: pass
             return

        logging.info(f"リアクション '{TRIGGER_EMOJI}' を検知しました。要約処理を開始します: {message.jump_url}")

        # ユーザーが付与したトリガーリアクションを削除
        try:
            user = await self.bot.fetch_user(payload.user_id)
            await message.remove_reaction(payload.emoji, user)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logging.warning(f"ユーザーリアクションの削除に失敗しました: {message.jump_url}")

        # メインの処理関数を呼び出す
        await self.perform_summary_async(url=content, message=message)


    def _extract_transcript_text(self, fetched_data):
        """文字起こしデータ(リスト形式想定)からテキストを抽出する"""
        texts = []
        try:
            if isinstance(fetched_data, list):
                for snippet in fetched_data:
                    if isinstance(snippet, dict):
                         texts.append(snippet.get('text', ''))
                full_text = " ".join(t.strip() for t in texts if t and t.strip())
                cleaned_text = re.sub(r'\s+', ' ', full_text).strip()
                return cleaned_text
            else:
                 logging.warning(f"予期せぬ字幕データ形式のため、テキスト抽出に失敗しました: {type(fetched_data)}")
                 return ""
        except Exception as e:
            logging.error(f"字幕テキスト抽出中にエラー: {e}", exc_info=True)
            return ""

    async def perform_summary_async(self, url: str, message: discord.Message | discord.InteractionMessage):
        """YouTube要約処理のコアロジック (非同期版) - fetch部分は元のコード"""
        obsidian_save_success = False
        transcript_text_for_gdoc = "(文字起こし取得失敗)"
        video_title = "Untitled Video"
        start_time = datetime.datetime.now()
        interaction = getattr(message, 'interaction', None)
        video_id = None

        try:
            if isinstance(message, discord.Message):
                await message.add_reaction("⏳")
            elif interaction and not interaction.response.is_done():
                 pass # defer済み前提
            else:
                 logging.warning("InteractionMessage received but not deferred.")

            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                logging.warning(f"無効なYouTube URL: {url}")
                if isinstance(message, discord.Message): await message.add_reaction("❓")
                if interaction: await interaction.followup.send("無効なYouTube URLです。", ephemeral=True)
                return
            video_id = video_id_match.group(1)
            logging.info(f"Processing YouTube video: {video_id}")

            # --- 字幕取得 ---
            transcript_text = ""
            fetched_transcript = None
            try:
                # YouTubeTranscriptApiインスタンスを作成
                # api_instance = YouTubeTranscriptApi() # get_transcriptはクラスメソッドなのでインスタンス不要
                # get_transcript を非同期実行
                fetched_transcript = await asyncio.to_thread(
                    YouTubeTranscriptApi.get_transcript, # クラスメソッドを直接呼び出し
                    video_id,
                    languages=['ja', 'en']
                )

                transcript_text = self._extract_transcript_text(fetched_transcript)
                transcript_text_for_gdoc = transcript_text
                if transcript_text:
                    logging.info(f"Transcript fetched successfully for {video_id}. Length: {len(transcript_text)}")
                else:
                    logging.warning(f"Extracted transcript text is empty for {video_id}.")
                    raise NoTranscriptFound(video_id, ['ja', 'en'], "Extracted text was empty.")

            except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as e:
                logging.warning(f"字幕が見つかりませんでした/取得できませんでした (Video ID: {video_id}): {e}")
                if isinstance(message, discord.Message): await message.add_reaction("🔇")
                if interaction: await interaction.followup.send("この動画の字幕は見つからないか、無効になっています。", ephemeral=True)
                transcript_text_for_gdoc = "(字幕なしまたは取得失敗)"
            except Exception as e:
                logging.error(f"字幕取得中に予期せぬエラー (Video ID: {video_id}): {e}", exc_info=True)
                if isinstance(message, discord.Message): await message.add_reaction("❌")
                if interaction: await interaction.followup.send(f"字幕取得中にエラーが発生しました: {e}", ephemeral=True)
                transcript_text_for_gdoc = f"(字幕取得エラー: {e})"
                # return # 続行する場合コメントアウト

            # --- AI要約 (字幕がある場合のみ) ---
            concise_summary = "(AI要約失敗)"
            detail_summary = "(AI要約失敗)"
            if transcript_text and self.gemini_model:
                logging.info(f"Generating AI summaries for {video_id}...")
                concise_prompt = (
                    "以下のYouTube動画の文字起こし全文を元に、重要なポイントを箇条書きで3～5点にまとめてください。\n"
                    "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                    f"--- 文字起こし全文 ---\n{transcript_text}"
                 )
                detail_prompt = (
                    "以下のYouTube動画の文字起こし全文を元に、その内容を網羅する詳細で包括的な要約を、段落に分けて作成してください。\n"
                    "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                    f"--- 文字起こし全文 ---\n{transcript_text}"
                 )
                try:
                    tasks = [
                         self.gemini_model.generate_content_async(concise_prompt),
                         self.gemini_model.generate_content_async(detail_prompt)
                     ]
                    responses = await asyncio.gather(*[asyncio.wait_for(task, timeout=300) for task in tasks], return_exceptions=True)

                    if isinstance(responses[0], asyncio.TimeoutError): concise_summary = "(簡潔な要約タイムアウト)"
                    elif isinstance(responses[0], Exception): concise_summary = f"(簡潔な要約の生成に失敗: {type(responses[0]).__name__})"
                    elif responses[0] and hasattr(responses[0], 'text'): concise_summary = responses[0].text.strip()
                    else: concise_summary = "(簡潔な要約の応答不正)"

                    if isinstance(responses[1], asyncio.TimeoutError): detail_summary = "(詳細な要約タイムアウト)"
                    elif isinstance(responses[1], Exception): detail_summary = f"(詳細な要約の生成に失敗: {type(responses[1]).__name__})"
                    elif responses[1] and hasattr(responses[1], 'text'): detail_summary = responses[1].text.strip()
                    else: detail_summary = "(詳細な要約の応答不正)"

                    if not isinstance(responses[0], Exception) and not isinstance(responses[1], Exception): logging.info(f"AI summaries generated successfully for {video_id}.")
                except Exception as e_ai:
                    logging.error(f"AI summary generation failed: {e_ai}", exc_info=True)
                    concise_summary = f"(AI要約エラー: {type(e_ai).__name__})"
                    detail_summary = f"(AI要約エラー: {type(e_ai).__name__})"

            elif not self.gemini_model:
                 concise_summary = "(AI要約機能無効)"; detail_summary = "(AI要約機能無効)"
            else: # 字幕なし
                 concise_summary = "(字幕なしのため要約不可)"; detail_summary = "(字幕なしのため要約不可)"

            # --- 保存準備 ---
            now = datetime.datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')
            video_info = await self.get_video_info(video_id)
            video_title = video_info.get('title', f'YouTube_{video_id}')
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)[:100]
            if not safe_title: safe_title = f"YouTube_{video_id}"
            note_filename = f"{timestamp}-{safe_title}.md"
            note_filename_for_link = note_filename.replace('.md', '')

            # --- Obsidian用ノート内容 ---
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

            # --- Obsidianへの保存 ---
            dbx = None
            if self.dropbox_refresh_token and self.dropbox_app_key and self.dropbox_app_secret:
                 try:
                      dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret, timeout=300)
                      dbx.users_get_current_account()
                 except Exception as dbx_e: logging.error(f"Dropbox client init failed: {dbx_e}"); dbx = None
            else: logging.error("Dropbox credentials not found.")

            if dbx:
                try:
                    note_path = f"{self.dropbox_vault_path}/YouTube/{note_filename}"
                    await asyncio.to_thread(dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
                    logging.info(f"Summary saved (Obsidian): {note_path}")

                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                    daily_note_content = ""
                    try:
                        metadata, res = await asyncio.to_thread(dbx.files_download, daily_note_path)
                        daily_note_content = res.content.decode('utf-8')
                    except ApiError as e:
                        if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): daily_note_content = f"# {daily_note_date}\n\n"
                        else: raise
                    link_to_add = f"- [[YouTube/{note_filename_for_link}|{video_title}]]"
                    youtube_heading = "## YouTube Summaries"
                    new_daily_content = update_section(daily_note_content, link_to_add, youtube_heading)
                    await asyncio.to_thread(dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
                    logging.info(f"Daily note updated (Obsidian): {daily_note_path}")
                    obsidian_save_success = True

                except ApiError as e:
                     logging.error(f"Dropbox API error during Obsidian save: {e}", exc_info=True)
                     if isinstance(message, discord.Message): await message.add_reaction("⚠️")
                     obsidian_save_success = False
                except Exception as ob_e:
                     logging.error(f"Unexpected error during Obsidian save: {ob_e}", exc_info=True)
                     if isinstance(message, discord.Message): await message.add_reaction("💾")
                     obsidian_save_success = False
            else:
                 logging.error("Dropbox client not available. Skipping Obsidian save.")
                 if isinstance(message, discord.Message): await message.add_reaction("⚠️")
                 obsidian_save_success = False

            # --- Google Docsへの追記 (文字起こし全文) ---
            if google_docs_enabled and transcript_text_for_gdoc:
                 try:
                    await append_text_to_doc_async(
                        text_to_append=transcript_text_for_gdoc,
                        source_type="YouTube Transcript", url=url, title=video_title
                    )
                    logging.info(f"Transcript saved (Google Docs): {url}")
                 except Exception as e_gdoc:
                     logging.error(f"Failed to send YouTube transcript to Google Docs: {e_gdoc}", exc_info=True)
                     if isinstance(message, discord.Message): await message.add_reaction("🇬️")
            elif google_docs_enabled: # 文字起こし失敗時
                 try:
                      await append_text_to_doc_async("(文字起こしなしまたは取得失敗)", "YouTube Link (No Transcript)", url, video_title)
                      logging.info(f"YouTube link info saved (Google Docs): {url}")
                 except Exception as e_gdoc_link:
                      logging.error(f"Failed to send YouTube link info to Google Docs: {e_gdoc_link}", exc_info=True)

            # 成功リアクション (Obsidian保存成功が基準)
            if obsidian_save_success:
                if isinstance(message, discord.Message): await message.add_reaction("✅")

            final_message_content = f"✅ YouTube動画の処理が完了しました: <{url}>"
            if isinstance(message, discord.InteractionMessage):
                 await message.edit(content=final_message_content)

            logging.info(f"Processing finished for: {url}")

        except Exception as e:
            logging.error(f"Unexpected error in perform_summary_async for {url}: {e}", exc_info=True)
            error_message = f"❌ YouTube処理中に予期せぬエラーが発生しました: `{type(e).__name__}`"
            if isinstance(message, discord.Message):
                try: await message.add_reaction("❌"); await message.reply(error_message)
                except discord.HTTPException: pass
            elif interaction:
                try: await message.edit(content=error_message)
                except discord.HTTPException:
                     try: await interaction.followup.send(error_message, ephemeral=True)
                     except discord.HTTPException: pass

            if google_docs_enabled:
                try:
                    error_text = f"YouTube処理エラー\nURL: {url}\nError: {type(e).__name__}: {e}"
                    title_for_error = video_title if video_title != "Untitled Video" else f"YouTube_{video_id or 'UnknownID'}"
                    await append_text_to_doc_async(error_text, "YouTube Processing Error", url, title_for_error)
                except Exception as e_gdoc_err:
                     logging.error(f"Failed to record YouTube processing error to Google Docs: {e_gdoc_err}")
        finally:
            end_time = datetime.datetime.now()
            duration = (end_time - start_time).total_seconds()
            logging.info(f"YouTube process duration for {url}: {duration:.2f} seconds.")
            if isinstance(message, discord.Message):
                try: await message.remove_reaction("⏳", self.bot.user)
                except discord.HTTPException: pass


    # --- スラッシュコマンド ---
    @app_commands.command(name="ytsum", description="[手動] YouTube動画のURLを要約してObsidian/Google Docsに保存します。")
    @app_commands.describe(url="要約したいYouTube動画のURL")
    async def yt_summary_command(self, interaction: discord.Interaction, url: str):
        if not self.gemini_model:
            await interaction.response.send_message("⚠️ AI要約機能が無効です (Gemini APIキー未設定または初期化失敗)。文字起こしのみ保存されます。", ephemeral=True)

        # ephemeral=False にして処理が見えるようにし、thinking=Trueで待機
        await interaction.response.defer(ephemeral=False, thinking=True)
        message = await interaction.original_response()
        await self.perform_summary_async(url=url, message=message)
        # 完了メッセージは perform_summary_async 内で編集される

    # --- get_video_info ---
    async def get_video_info(self, video_id: str) -> dict:
        headers = {'User-Agent': 'Mozilla/5.0 ...'} # 省略
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        try:
            async with self.session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        title = data.get("title")
                        author = data.get("author_name")
                        if title and author: return {"title": title, "author_name": author}
                        else: logging.warning(f"oEmbed response missing title/author for {video_id}. Data: {data}"); return {"title": f"YouTube_{video_id}", "author_name": "N/A"}
                    except aiohttp.ContentTypeError as json_e: logging.warning(f"oEmbed response not valid JSON for {video_id}: {json_e}"); return {"title": f"YouTube_{video_id}", "author_name": "N/A"}
                else: response_text = await response.text(); logging.warning(f"oEmbed failed: Status {response.status} for {video_id}. Response: {response_text[:200]}"); return {"title": f"YouTube_{video_id}", "author_name": "N/A"}
        except asyncio.TimeoutError: logging.warning(f"oEmbed request timed out: {video_id}")
        except aiohttp.ClientError as e: logging.warning(f"oEmbed client error: {e} for {video_id}")
        except Exception as e: logging.warning(f"oEmbed unexpected error: {e} for {video_id}")
        return {"title": f"YouTube_{video_id}", "author_name": "N/A"}


async def setup(bot: commands.Bot):
    # チャンネルIDのチェック
    if TARGET_CHANNEL_ID == 0:
         logging.error("MEMO_CHANNEL_ID (TARGET_CHANNEL_ID for YouTubeCog) が設定されていないため、YouTubeCogをロードしません。")
         return
    await bot.add_cog(YouTubeCog(bot))