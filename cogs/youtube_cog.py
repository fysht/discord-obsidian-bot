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
import aiohttp
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript

# --- 共通関数インポート (元のコードにはなかったので簡易版をCog内に定義) ---
# from utils.obsidian_utils import update_section # -> 簡易版を使用
# --- Google Docs連携 ---
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("YouTubeCog: Google Docs連携が有効です。")
except ImportError:
    logging.warning("YouTubeCog: google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False
    # ダミー関数を定義
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass # 何もしない
# --- ここまで ---

# --- 定数定義 ---
try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
    from datetime import timezone, timedelta
    JST = timezone(timedelta(hours=+9), "JST")

# YouTube URL Regex (グループ1でVideo IDを抽出)
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
# Botが付与する処理開始トリガー
BOT_PROCESS_TRIGGER_REACTION = '📥'
# 処理ステータス用
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
# エラー詳細用
TRANSCRIPT_NOT_FOUND_EMOJI = '🔇' # 字幕なし
INVALID_URL_EMOJI = '❓' # 無効URL
SUMMARY_ERROR_EMOJI = '⚠️' # 要約失敗/タイムアウト
SAVE_ERROR_EMOJI = '💾' # Obsidian保存失敗
GOOGLE_DOCS_ERROR_EMOJI = '🇬' # Google Docs連携エラー

class YouTubeCog(commands.Cog):
    """YouTube動画の要約とObsidian/Google Docsへの保存を行うCog (リアクショントリガー)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数読み込み ---
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        # --- クライアント初期化とチェック ---
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

    # --- 簡易的な update_section ---
    def _update_daily_note_section(self, current_content: str, text_to_add: str, section_header: str) -> str:
        """デイリーノートの指定セクションに追記する簡易関数"""
        lines = current_content.split('\n')
        new_content_lines = list(lines) # コピーを作成

        try:
            heading_index = -1
            for i, line in enumerate(new_content_lines):
                if line.strip() == section_header:
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
            # セクションがない場合は末尾に追加
            logging.info(f"Section '{section_header}' not found in daily note, appending.")
            return current_content.strip() + f"\n\n{section_header}\n{text_to_add}\n"
    # --- 簡易的な update_section ここまで ---

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Botが付与したトリガーリアクションを検知して処理を開始"""
        # 必要なチェック
        if payload.channel_id != self.youtube_summary_channel_id: return
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

        # 要約処理を実行 (元の _perform_summary を呼び出す)
        await self._perform_summary(url=url, message=message)


    def _extract_transcript_text(self, fetched_data):
        # 元のコードのロジック
        texts = []
        try:
            for snippet in fetched_data:
                if isinstance(snippet, dict):
                    texts.append(snippet.get('text', ''))
                elif hasattr(snippet, 'text'):
                    texts.append(getattr(snippet, 'text', ''))
                else: texts.append(str(snippet))
            cleaned_text = " ".join(t.strip() for t in texts if t and t.strip())
            return re.sub(r'\s+', ' ', cleaned_text).strip()
        except TypeError:
             if hasattr(fetched_data, 'text'): return getattr(fetched_data, 'text', '').strip()
             logging.warning(f"予期せぬ字幕データ形式: {type(fetched_data)}")
             return ""
        except Exception as e:
            logging.error(f"字幕テキスト抽出エラー: {e}", exc_info=True)
            return ""

    # process_pending_summaries は不要

    async def _perform_summary(self, url: str, message: discord.Message | discord.InteractionMessage):
        """YouTube要約処理のコアロジック (Google Docs保存追加)"""
        obsidian_save_success = False
        gdoc_save_success = False # Google Docs 保存成功フラグ
        error_reactions = set() # エラーリアクション保持
        video_title = "Untitled Video" # 初期値
        video_id = None # 初期値

        try:
            # 処理開始リアクション
            if isinstance(message, discord.Message):
                try: await message.add_reaction(PROCESS_START_EMOJI)
                except discord.HTTPException: pass

            # --- URL解析 & Video ID 取得 ---
            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                if isinstance(message, discord.Message): error_reactions.add(INVALID_URL_EMOJI)
                raise ValueError("Invalid YouTube URL") # エラーを発生させて終了
            video_id = video_id_match.group(1)

            # --- 字幕取得 ---
            transcript_text = ""
            try:
                fetched = await asyncio.to_thread(
                    YouTubeTranscriptApi.get_transcript, # get_transcript を使用
                    video_id,
                    languages=['ja', 'en']
                )
                transcript_text = self._extract_transcript_text(fetched)
                if not transcript_text:
                     logging.warning(f"字幕テキストが空でした (Video ID: {video_id})")
                     if isinstance(message, discord.Message): error_reactions.add(TRANSCRIPT_NOT_FOUND_EMOJI)
                     # 字幕がなくても続行

            except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as e:
                logging.warning(f"字幕取得失敗 (Video ID: {video_id}): {e}")
                if isinstance(message, discord.Message): error_reactions.add(TRANSCRIPT_NOT_FOUND_EMOJI)
                # 字幕がなくても続行
            except Exception as e_trans:
                logging.error(f"字幕取得中に予期せぬエラー (Video ID: {video_id}): {e_trans}", exc_info=True)
                if isinstance(message, discord.Message): error_reactions.add(PROCESS_ERROR_EMOJI)
                # エラーでも続行（ただし要約は不可）

            # --- AI要約 ---
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
                    # タイムアウトを設定 (例: 5分)
                    responses = await asyncio.gather(*[asyncio.wait_for(task, timeout=300) for task in tasks], return_exceptions=True)

                    # 結果のハンドリング
                    if isinstance(responses[0], (Exception, asyncio.TimeoutError)):
                         concise_summary = f"(簡潔な要約エラー: {type(responses[0]).__name__})"
                         error_reactions.add(SUMMARY_ERROR_EMOJI)
                         logging.error(f"Concise summary failed: {responses[0]}")
                    elif hasattr(responses[0], 'text'): concise_summary = responses[0].text
                    else: concise_summary = "(簡潔な要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                    if isinstance(responses[1], (Exception, asyncio.TimeoutError)):
                         detail_summary = f"(詳細な要約エラー: {type(responses[1]).__name__})"
                         error_reactions.add(SUMMARY_ERROR_EMOJI)
                         logging.error(f"Detailed summary failed: {responses[1]}")
                    elif hasattr(responses[1], 'text'): detail_summary = responses[1].text
                    else: detail_summary = "(詳細な要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                    if not error_reactions: logging.info(f"AI summaries generated for {video_id}")

                except Exception as e_gather:
                    logging.error(f"AI summary gather failed: {e_gather}", exc_info=True)
                    concise_summary = detail_summary = "(AI要約プロセスエラー)"
                    if isinstance(message, discord.Message): error_reactions.add(SUMMARY_ERROR_EMOJI)

            elif not self.gemini_model: concise_summary = detail_summary = "(AI要約機能無効)"; error_reactions.add(SUMMARY_ERROR_EMOJI)
            elif not transcript_text: concise_summary = detail_summary = "(字幕なしのため要約不可)"

            # --- 保存準備 ---
            now = datetime.datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')
            video_info = await self.get_video_info(video_id)
            video_title = video_info.get("title", f"YouTube_{video_id}") # get_video_infoの結果を使用
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
                f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n\n" # Clipped 日時を追加
                f"[[{daily_note_date}]]\n\n"
                f"---\n\n"
                f"## Concise Summary\n{concise_summary}\n\n"
                f"## Detailed Summary\n{detail_summary}\n\n"
            )

            # --- Obsidianへの保存 ---
            if self.dbx:
                try:
                    # 個別ノート保存
                    note_path = f"{self.dropbox_vault_path}/YouTube/{note_filename}"
                    await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
                    logging.info(f"Summary saved to Obsidian note: {note_path}")

                    # デイリーノート更新
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
                    youtube_heading = "## YouTube Summaries" # 元のコードの変数名
                    # 簡易的な update_section を使用
                    new_daily_content = self._update_daily_note_section(daily_note_content, link_to_add, youtube_heading)

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

            # --- Google Docsへの保存 ---
            if google_docs_enabled:
                gdoc_text_to_append = ""
                gdoc_source_type = "YouTube Error" # デフォルト
                if transcript_text:
                    # 字幕がある場合は字幕を保存
                    gdoc_text_to_append = transcript_text
                    gdoc_source_type = "YouTube Transcript"
                elif video_id:
                    # 字幕がない場合はエラー理由を含めてリンク情報のみ保存
                    error_reason = "(字幕なしまたは取得失敗)"
                    if TRANSCRIPT_NOT_FOUND_EMOJI in error_reactions: error_reason = "(字幕なしまたは取得失敗)"
                    if PROCESS_ERROR_EMOJI in error_reactions: error_reason = "(字幕取得エラー)" # より深刻なエラー
                    gdoc_text_to_append = error_reason
                    gdoc_source_type = "YouTube Link (No Transcript)"
                # else: URL解析失敗時は video_id がないので何もしない

                if gdoc_text_to_append: # 送信するテキストがある場合のみ実行
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

            # --- 最終リアクション ---
            if isinstance(message, discord.Message):
                if obsidian_save_success: # Obsidian成功を基準
                    if not error_reactions: # 他にエラーがなければ成功
                        await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    else:
                        await message.add_reaction(PROCESS_COMPLETE_EMOJI) # Obsidian成功は示す
                        for reaction in error_reactions:
                            try: await message.add_reaction(reaction)
                            except discord.HTTPException: pass
                else:
                    # Obsidian失敗
                    final_reactions = error_reactions if error_reactions else {PROCESS_ERROR_EMOJI}
                    for reaction in final_reactions:
                        try: await message.add_reaction(reaction)
                        except discord.HTTPException: pass

        except ValueError as e_val: # Invalid URLなど
             logging.error(f"Processing stopped due to ValueError: {e_val}")
             # エラーリアクションは try ブロック内で設定済みのはず
        except Exception as e:
            # _perform_summary 全体の予期せぬエラー
            logging.error(f"YouTube要約処理全体でエラー: {e}", exc_info=True)
            if isinstance(message, discord.Message):
                try: await message.add_reaction(PROCESS_ERROR_EMOJI)
                except discord.HTTPException: pass
            elif isinstance(message, discord.InteractionMessage): # スラッシュコマンドの場合
                interaction = getattr(message, 'interaction', None)
                if interaction:
                    try: await interaction.followup.send(f"❌ 処理中に予期せぬエラー: `{type(e).__name__}`", ephemeral=True)
                    except discord.HTTPException: pass

            # Google Docsにエラー情報を記録 (任意)
            if google_docs_enabled:
                try:
                    error_text = f"YouTube処理全体のエラー\nURL: {url}\nError: {type(e).__name__}: {e}"
                    title_for_error = video_title if video_title != "Untitled Video" else f"YouTube_{video_id or 'UnknownID'}"
                    await append_text_to_doc_async(error_text, "YouTube Processing Error", url, title_for_error)
                except Exception as e_gdoc_err:
                     logging.error(f"Failed to record YouTube processing error to Google Docs: {e_gdoc_err}")

        finally:
            # 処理中リアクションを削除
            if isinstance(message, discord.Message):
                try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass

    # --- 元のスラッシュコマンド (InteractionMessage の扱いに注意) ---
    @app_commands.command(name="yt_summary", description="[手動] YouTube動画URLをObsidian/Google Docsに保存します。")
    @app_commands.describe(url="処理したいYouTube動画のURL")
    async def yt_summary_command(self, interaction: discord.Interaction, url: str):
        if not self.is_ready:
             await interaction.response.send_message("❌ YouTube Cogが初期化されていません。", ephemeral=True)
             return

        await interaction.response.defer(ephemeral=False, thinking=True) # thinking=Trueに変更
        message_proxy = await interaction.original_response()

        # _perform_summary は Message オブジェクトを期待する
        # InteractionMessage ではリアクション操作が不安定になる可能性がある
        class TempMessage: # ダミークラス
             def __init__(self, proxy):
                 self.id = proxy.id; self.reactions = []; self.channel = proxy.channel; self.jump_url = proxy.jump_url; self._proxy = proxy; self.content=proxy.content # content追加
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji)
                 except: pass
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user)
                 except: pass

        await self._perform_summary(url=url, message=TempMessage(message_proxy))
        # 完了・エラーのフィードバックはリアクションで行われるため、ここではメッセージ編集しない
        # await interaction.edit_original_response(content=f"YouTube処理を実行しました: {url}")


    # --- get_video_info (元のコードのまま) ---
    async def get_video_info(self, video_id: str) -> dict:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        try:
            headers = {'User-Agent': 'Mozilla/5.0 ...'} # 適切なUser-Agentを設定
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