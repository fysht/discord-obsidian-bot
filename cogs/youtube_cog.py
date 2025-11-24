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
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- 共通関数インポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("YouTubeCog: utils/obsidian_utils.pyが見つからないため、簡易的な追記処理を使用します。")
    def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
        lines = current_content.split('\n')
        new_content_lines = list(lines)
        try:
            heading_index = -1
            for i, line in enumerate(new_content_lines):
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

# --- Webパーサーインポート ---
try:
    from web_parser import parse_url_with_readability
except ImportError:
    logging.warning("YouTubeCog: web_parser が見つかりません。Webレシピの解析は無効です。")
    parse_url_with_readability = None

# --- Google Docs連携 ---
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
except ImportError:
    logging.warning("YouTubeCog: google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
TRANSCRIPT_NOT_FOUND_EMOJI = '🔇'
INVALID_URL_EMOJI = '❓'
SUMMARY_ERROR_EMOJI = '⚠️'
SAVE_ERROR_EMOJI = '💾'
GOOGLE_DOCS_ERROR_EMOJI = '🇬'

class YouTubeCog(commands.Cog, name="YouTubeCog"): 
    """YouTube動画とWebレシピの要約・保存を行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        self.recipe_channel_id = int(os.getenv("RECIPE_CHANNEL_ID", 0)) 
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.dbx = None
        self.gemini_model = None
        self.session = None
        self.is_ready = False

        missing_vars = []
        if not self.youtube_summary_channel_id and not self.recipe_channel_id: 
            missing_vars.append("YOUTUBE_SUMMARY_CHANNEL_ID or RECIPE_CHANNEL_ID")
        if not self.dropbox_app_key: missing_vars.append("DROPBOX_APP_KEY")
        if not self.dropbox_app_secret: missing_vars.append("DROPBOX_APP_SECRET")
        if not self.dropbox_refresh_token: missing_vars.append("DROPBOX_REFRESH_TOKEN")
        if not self.gemini_api_key: missing_vars.append("GEMINI_API_KEY")
        if self.recipe_channel_id and not parse_url_with_readability:
            logging.warning("YouTubeCog: RECIPE_CHANNEL_IDが設定されていますが、web_parserが見つかりません。")

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
            self.gemini_model = genai.GenerativeModel("gemini-3-pro-preview")
            logging.info("YouTubeCog: Gemini client initialized.")

            self.session = aiohttp.ClientSession()
            logging.info("YouTubeCog: aiohttp session started.")

            self.is_ready = True
        except Exception as e:
            logging.error(f"YouTubeCog: Failed to initialize clients: {e}", exc_info=True)


    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
            logging.info("YouTubeCog: aiohttp session closed.")

    # ★ 新規追加: 監視対象チャンネルへのURL投稿を検知して処理トリガーを付与
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """YouTubeまたはレシピチャンネルにURLが投稿された場合、処理トリガー(📥)を付与する"""
        if not self.is_ready or message.author.bot:
            return
            
        if message.channel.id not in (self.youtube_summary_channel_id, self.recipe_channel_id):
            return

        # YouTube URL、またはレシピチャンネルで http を含むメッセージならトリガー
        if YOUTUBE_URL_REGEX.search(message.content) or (self.recipe_channel_id == message.channel.id and "http" in message.content):
            logging.info(f"YouTubeCog: URL detected in monitored channel ({message.channel.name}). Adding trigger.")
            try:
                if not any(str(r.emoji) == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions):
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except discord.HTTPException as e:
                logging.warning(f"YouTubeCog: Failed to add trigger reaction: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Bot(自分自身)が付けた 📥 リアクションを検知して処理を開始する"""
        if payload.channel_id not in (self.youtube_summary_channel_id, self.recipe_channel_id):
            return
            
        emoji_str = str(payload.emoji)

        if emoji_str == BOT_PROCESS_TRIGGER_REACTION: 
            if payload.user_id != self.bot.user.id:
                return 

            channel = self.bot.get_channel(payload.channel_id)
            if not channel: return
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden):
                logging.warning(f"メッセージの取得に失敗しました: {payload.message_id}")
                return

            is_processed = any(r.emoji in (
                PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, 
                TRANSCRIPT_NOT_FOUND_EMOJI, INVALID_URL_EMOJI, SUMMARY_ERROR_EMOJI,
                SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
                ) and r.me for r in message.reactions)
            
            if is_processed:
                return

            logging.info(f"Bot (self) の '{BOT_PROCESS_TRIGGER_REACTION}' を検知しました。処理を開始します: {message.jump_url}")
            
            try:
                await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException:
                pass

            await self._perform_summary(url=message.content.strip(), message=message)


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
            if isinstance(fetched_data, list):
                for item in fetched_data:
                        if isinstance(item, dict):
                            texts.append(item.get('text', ''))
                return " ".join(t.strip() for t in texts if t and t.strip())
            logging.warning(f"予期せぬ字幕データ形式のため、テキスト抽出に失敗しました: {type(fetched_data)}")
            return ""

    async def process_pending_summaries(self):
        """起動時などに未処理の要約リクエストをまとめて処理する"""
        scan_channels = []
        if self.youtube_summary_channel_id:
            scan_channels.append(self.bot.get_channel(self.youtube_summary_channel_id))
        if self.recipe_channel_id:
            if self.recipe_channel_id != self.youtube_summary_channel_id:
                scan_channels.append(self.bot.get_channel(self.recipe_channel_id))

        if not scan_channels:
            logging.error("YouTubeCog: スキャン対象のチャンネル (YouTube/Recipe) が見つかりません。")
            return
            
        pending_messages = []
        
        for channel in scan_channels:
            if not channel: continue
            logging.info(f"チャンネル '{channel.name}' の未処理リアクションをスキャンします...")
            try:
                async for message in channel.history(limit=200):
                    has_pending_trigger_by_bot = False
                    is_processed_by_local = False
                    is_stuck_processing_local = False

                    for r in message.reactions:
                        emoji_str = str(r.emoji)
                        if emoji_str == BOT_PROCESS_TRIGGER_REACTION and r.me: 
                            has_pending_trigger_by_bot = True
                        if emoji_str in (
                            PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, TRANSCRIPT_NOT_FOUND_EMOJI, 
                            INVALID_URL_EMOJI, SUMMARY_ERROR_EMOJI, SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
                        ) and r.me:
                            is_processed_by_local = True
                        if emoji_str == PROCESS_START_EMOJI and r.me:
                            is_stuck_processing_local = True 

                    if (has_pending_trigger_by_bot or is_stuck_processing_local) and not is_processed_by_local:
                        pending_messages.append(message)
            except discord.Forbidden:
                logging.error(f"チャンネル {channel.name} の履歴読み取り権限がありません。")
                continue
            except discord.HTTPException as e:
                logging.error(f"チャンネル {channel.name} の履歴読み取り中にエラー: {e}")
                continue

        if not pending_messages:
            logging.info("処理対象の新しいYouTube/Recipe要約はありませんでした。")
            return

        logging.info(f"{len(pending_messages)}件の未処理YouTube/Recipe要約が見つかりました。古いものから順に処理します...")
        pending_messages.sort(key=lambda m: m.created_at)

        for message in pending_messages:
            logging.info(f"処理開始: {message.jump_url}")
            url = message.content.strip()
            try: await message.clear_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except: pass
            try: await message.clear_reaction(PROCESS_START_EMOJI)
            except: pass
            
            await self._perform_summary(url=url, message=message)
            await asyncio.sleep(5)


    async def _perform_summary(self, url: str, message: discord.Message | discord.InteractionMessage):
        """YouTube要約またはWebレシピ抽出処理のコアロジック"""
        obsidian_save_success = False
        gdoc_save_success = False
        error_reactions = set()
        video_title = "Untitled"
        video_id = None
        transcript_text = ""
        title_from_content = None

        is_recipe_channel = False
        if isinstance(message, discord.Message):
            is_recipe_channel = (message.channel.id == self.recipe_channel_id)
        elif isinstance(message, discord.InteractionMessage):
             if message.channel:
                 is_recipe_channel = (message.channel.id == self.recipe_channel_id)

        try:
            if isinstance(message, discord.Message):
                try: await message.add_reaction(PROCESS_START_EMOJI)
                except discord.HTTPException: pass

            video_id_match = YOUTUBE_URL_REGEX.search(url)
            
            if video_id_match:
                video_id = video_id_match.group(1)
                logging.info(f"Processing as YouTube URL (Video ID: {video_id})")
                try:
                    api = YouTubeTranscriptApi() 
                    fetched = await asyncio.to_thread(api.fetch, video_id, languages=['ja', 'en'])
                    transcript_text = self._extract_transcript_text(fetched)
                    if not transcript_text:
                         logging.warning(f"字幕テキストが空でした (Video ID: {video_id})")
                         if isinstance(message, discord.Message): error_reactions.add(TRANSCRIPT_NOT_FOUND_EMOJI)
                except (TranscriptsDisabled, NoTranscriptFound) as e:
                    logging.warning(f"字幕取得失敗 (Video ID: {video_id}): {e}")
                    if isinstance(message, discord.Message): error_reactions.add(TRANSCRIPT_NOT_FOUND_EMOJI)
                except Exception as e_trans:
                    logging.error(f"字幕取得中に予期せぬエラー (Video ID: {video_id}): {e_trans}", exc_info=True) 
                    if isinstance(message, discord.Message): error_reactions.add(PROCESS_ERROR_EMOJI)
            
            elif is_recipe_channel and parse_url_with_readability:
                logging.info(f"Non-YouTube URL detected in Recipe channel. Parsing as webpage: {url}")
                try:
                    loop = asyncio.get_running_loop()
                    parsed_title, content_md = await loop.run_in_executor(None, parse_url_with_readability, url)
                    if parsed_title and parsed_title != "No Title Found": title_from_content = parsed_title
                    if content_md and "URLの取得に失敗" not in content_md:
                        transcript_text = content_md
                        logging.info(f"Webpage parsed successfully. Title: {parsed_title}")
                    else:
                        logging.warning(f"Webpage parsing failed or returned empty content for {url}")
                        if isinstance(message, discord.Message): error_reactions.add(TRANSCRIPT_NOT_FOUND_EMOJI)
                except Exception as e_web:
                    logging.error(f"Webpage parsing failed with exception: {e_web}", exc_info=True)
                    if isinstance(message, discord.Message): error_reactions.add(PROCESS_ERROR_EMOJI)
            else:
                if not parse_url_with_readability and is_recipe_channel:
                     logging.error("Web recipe detected but web_parser is not available.")
                     if isinstance(message, discord.Message): error_reactions.add(PROCESS_ERROR_EMOJI)
                     raise ValueError("Web Parser not available")
                else:
                    if isinstance(message, discord.Message): error_reactions.add(INVALID_URL_EMOJI)
                    raise ValueError("Invalid URL: Non-YouTube URL in YouTube Summary channel")

            concise_summary = "(要約対象なし)"
            detail_summary = "(対象外)"
            
            if transcript_text and self.gemini_model:
                try:
                    if is_recipe_channel:
                        logging.info("Generating AI Recipe summary...")
                        recipe_prompt = (
                            "以下のWebページ本文またはYouTube動画の文字起こしから、レシピ情報（材料と作り方）を抽出し、簡潔なMarkdown形式で要約してください。\n"
                            "「## Ingredients」と「## Instructions」の2つのセクションを必ず作成してください。\n"
                            "「## Ingredients」セクションには、**何人前のレシピか**（例: 2人分）を明記し、各材料の**分量も必ず記載**してください。（例: - 卵 2個）\n"
                            "「## Instructions」セクションは番号付きリスト（1. ...）で記述してください。\n"
                            "それ以外の情報（導入、感想など）は含めないでください。\n\n"
                            f"--- 本文/文字起こし ---\n{transcript_text}"
                        )
                        try:
                            response = await asyncio.wait_for(self.gemini_model.generate_content_async(recipe_prompt), timeout=300)
                            if hasattr(response, 'text') and response.text.strip():
                                concise_summary = response.text.strip()
                            else:
                                concise_summary = "(レシピ要約応答不正)"
                                error_reactions.add(SUMMARY_ERROR_EMOJI)
                        except (Exception, asyncio.TimeoutError) as e_recipe:
                            logging.error(f"AI recipe summary failed: {e_recipe}", exc_info=True)
                            concise_summary = f"(レシピ要約エラー: {type(e_recipe).__name__})"
                            error_reactions.add(SUMMARY_ERROR_EMOJI)
                    else:
                        logging.info("Generating AI General YouTube summaries...")
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
                        elif hasattr(responses[0], 'text'): concise_summary = responses[0].text.strip()
                        else: concise_summary = "(簡潔な要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                        if isinstance(responses[1], (Exception, asyncio.TimeoutError)):
                             detail_summary = f"(詳細な要約エラー: {type(responses[1]).__name__})"
                             error_reactions.add(SUMMARY_ERROR_EMOJI)
                        elif hasattr(responses[1], 'text'): detail_summary = responses[1].text.strip()
                        else: detail_summary = "(詳細な要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                    if not error_reactions.intersection({SUMMARY_ERROR_EMOJI}): logging.info(f"AI summaries generated for {url}")

                except Exception as e_gather:
                    logging.error(f"AI summary gather failed: {e_gather}", exc_info=True)
                    concise_summary = detail_summary = "(AI要約プロセスエラー)"
                    if isinstance(message, discord.Message): error_reactions.add(SUMMARY_ERROR_EMOJI)

            elif not self.gemini_model: concise_summary = detail_summary = "(AI要約機能無効)"; error_reactions.add(SUMMARY_ERROR_EMOJI)
            elif not transcript_text: concise_summary = detail_summary = "(字幕/本文なしのため要約不可)"

            if is_recipe_channel:
                logging.info("Sending recipe summary to Discord channel.")
                title_for_embed = video_title if video_id else title_from_content
                if not title_for_embed: title_for_embed = "Recipe"
                try:
                    embed = discord.Embed(
                        title=f"🧑‍🍳 レシピ要約 (AI): {title_for_embed}",
                        description=concise_summary,
                        color=discord.Color.orange(),
                        url=url
                    )
                    if isinstance(message, discord.Message):
                        await message.reply(embed=embed, mention_author=False)
                    elif isinstance(message, discord.InteractionMessage):
                         interaction = getattr(message, 'interaction', None)
                         if interaction: await interaction.followup.send(embed=embed)
                except discord.HTTPException as e_discord:
                    logging.error(f"RecipeCog (YT): Discordへの要約投稿失敗: {e_discord}", exc_info=True)

            now = datetime.datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')
            video_info = {}

            if video_id:
                video_info = await self.get_video_info(video_id)
                video_title = video_info.get("title", f"YouTube_{video_id}")
            elif title_from_content:
                video_title = title_from_content
            else:
                video_title = f"Untitled_{timestamp}"
                
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)[:100]
            if not safe_title: safe_title = f"Untitled_{timestamp}"
            note_filename = f"{timestamp}-{safe_title}.md"
            note_filename_for_link = note_filename.replace('.md', '')
            
            if is_recipe_channel:
                save_folder = "/Recipes"
                daily_note_header = "## Recipes"
                gdoc_source_type = "Recipe"
            else:
                save_folder = "/YouTube"
                daily_note_header = "## YouTube Summaries"
                gdoc_source_type = "YouTube Transcript"

            note_content = f"# {video_title}\n\n"
            if video_id:
                note_content += f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>\n\n'
            note_content += f"- **URL:** {url}\n"
            if video_id:
                note_content += f"- **Channel:** {video_info.get('author_name', 'N/A')}\n"
            note_content += f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n\n[[{daily_note_date}]]\n\n---\n\n"

            if is_recipe_channel:
                note_content += f"## レシピ要約 (AI)\n{concise_summary}\n"
                if not video_id and transcript_text:
                     note_content += f"\n---\n\n## 元記事 (本文)\n{transcript_text[:10000]}...\n"
            else:
                note_content += f"## Concise Summary\n{concise_summary}\n\n"
                note_content += f"## Detailed Summary\n{detail_summary}\n\n"
                if transcript_text:
                     note_content += f"\n---\n\n## Full Transcript\n{transcript_text[:10000]}...\n"

            if self.dbx:
                try:
                    note_path = f"{self.dropbox_vault_path}{save_folder}/{note_filename}"
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

                    link_path_part = save_folder.lstrip('/')
                    link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{video_title}]]" 
                    new_daily_content = update_section(daily_note_content, link_to_add, daily_note_header)

                    await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
                    logging.info(f"Daily note updated with link ({daily_note_header}): {daily_note_path}")
                    obsidian_save_success = True

                except ApiError as e_obs_api:
                    logging.error(f"Error saving to Obsidian (Dropbox API): {e_obs_api}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
                except Exception as e_obs_other:
                    logging.error(f"Error saving to Obsidian (Other): {e_obs_other}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
            else:
                error_reactions.add(SAVE_ERROR_EMOJI)

            if google_docs_enabled:
                gdoc_text_to_append = ""
                if is_recipe_channel:
                    gdoc_text_to_append = f"## レシピ要約 (AI)\n{concise_summary}"
                elif transcript_text:
                    gdoc_text_to_append = transcript_text
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
             logging.warning(f"Processing stopped due to ValueError: {e_val}")
             if isinstance(message, discord.Message):
                try: await message.add_reaction(INVALID_URL_EMOJI)
                except discord.HTTPException: pass
        except Exception as e:
            logging.error(f"YouTube/Recipe処理全体でエラー: {e}", exc_info=True)
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
                    error_text = f"YouTube/Recipe処理全体のエラー\nURL: {url}\nError: {type(e).__name__}: {e}"
                    title_for_error = video_title if video_title != "Untitled" else f"URL_{video_id or 'Unknown'}"
                    await append_text_to_doc_async(error_text, "Processing Error", url, title_for_error)
                except Exception as e_gdoc_err:
                     logging.error(f"Failed to record processing error to Google Docs: {e_gdoc_err}")

        finally:
            if isinstance(message, discord.Message):
                try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass

    @app_commands.command(name="yt_summary", description="[手動] YouTube動画URLをObsidian/Google Docsに保存します。")
    @app_commands.describe(url="処理したいYouTube動画のURL")
    async def yt_summary_command(self, interaction: discord.Interaction, url: str):
        if not self.is_ready:
             await interaction.response.send_message("❌ YouTube Cogが初期化されていません。", ephemeral=True)
             return
             
        if interaction.channel_id not in (self.youtube_summary_channel_id, self.recipe_channel_id):
             await interaction.response.send_message(f"❌ このコマンドは <#{self.youtube_summary_channel_id}> または <#{self.recipe_channel_id}> でのみ実行できます。", ephemeral=True)
             return

        await interaction.response.defer(ephemeral=False, thinking=True)
        message_proxy = await interaction.original_response()

        class TempMessage:
             def __init__(self, proxy, channel):
                 self.id = proxy.id; self.reactions = []; self.channel = channel; self.jump_url = proxy.jump_url; self._proxy = proxy; self.content=proxy.content
                 self.interaction = interaction
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji)
                 except: pass
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user)
                 except: pass

        await self._perform_summary(url=url, message=TempMessage(message_proxy, interaction.channel))

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
    youtube_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
    recipe_channel_id = int(os.getenv("RECIPE_CHANNEL_ID", 0))
    
    if youtube_channel_id == 0 and recipe_channel_id == 0:
        logging.error("YouTubeCog: YOUTUBE_SUMMARY_CHANNEL_ID と RECIPE_CHANNEL_ID が両方とも設定されていません。Cogをロードしません。")
        return
        
    cog_instance = YouTubeCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("YouTubeCog (and Recipe) loaded successfully.")
    else:
        logging.error("YouTubeCog failed to initialize properly and was not loaded.")
        del cog_instance