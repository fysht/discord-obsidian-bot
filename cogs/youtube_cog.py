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
    logging.info("YouTubeCog: utils/obsidian_utils.py を読み込みました。")
except ImportError:
    logging.warning("YouTubeCog: utils/obsidian_utils.pyが見つからないため、簡易的な追記処理を使用します。")
    # (フォールバック用の簡易 update_section 関数)
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

# --- Google Docs連携 ---
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
# --- ここまで ---

class YouTubeCog(commands.Cog, name="YouTubeCog"):
    """YouTube動画の要約とObsidian/Google Docsへの保存を行うCog (Botリアクショントリガー)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        # ★ レシピチャンネルIDも取得
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
        if not self.youtube_summary_channel_id: missing_vars.append("YOUTUBE_SUMMARY_CHANNEL_ID")
        # ★ レシピチャンネルIDも必須チェックに追加
        if not self.recipe_channel_id: missing_vars.append("RECIPE_CHANNEL_ID") 
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
        if self.session and not self.session.closed:
            await self.session.close()
            logging.info("YouTubeCog: aiohttp session closed.")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        Bot(自分自身)が付けた 📥 リアクションを検知して処理を開始する
        """
        
        # ★ 監視対象チャンネルを増やす
        if payload.channel_id not in [self.youtube_summary_channel_id, self.recipe_channel_id]:
            return
            
        emoji_str = str(payload.emoji)

        if emoji_str == BOT_PROCESS_TRIGGER_REACTION: # '📥'
            
            # (local worker の) Bot 自身のリアクションか？
            if payload.user_id != self.bot.user.id:
                return 

            channel = self.bot.get_channel(payload.channel_id)
            if not channel: return
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden):
                logging.warning(f"メッセージの取得に失敗しました: {payload.message_id}")
                return

            # 既に処理中/処理完了のリアクションを付けているか？
            is_processed = any(r.emoji in (
                PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, 
                TRANSCRIPT_NOT_FOUND_EMOJI, INVALID_URL_EMOJI, SUMMARY_ERROR_EMOJI,
                SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
                ) and r.me for r in message.reactions)
            
            if is_processed:
                logging.info(f"既に処理中または処理済みのメッセージのためスキップします: {message.jump_url}")
                return

            logging.info(f"Bot (self) の '{BOT_PROCESS_TRIGGER_REACTION}' を検知しました。要約処理を開始します: {message.jump_url}")
            
            try:
                await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException:
                logging.warning(f"Bot のリアクション削除に失敗しました: {message.jump_url}")

            # ★ _perform_summary に message オブジェクト自体を渡す
            await self._perform_summary(url=message.content.strip(), message=message)

        elif payload.user_id == self.bot.user.id:
            return
        else:
            return


    def _extract_transcript_text(self, fetched_data):
        """youtube_transcript_apiのfetch()の戻り値からテキストを結合する"""
        texts = []
        try:
            # v3 (dict)
            for snippet in fetched_data:
                if isinstance(snippet, dict):
                    texts.append(snippet.get('text', ''))
            return " ".join(t.strip() for t in texts if t and t.strip())
        except TypeError: 
             # v4 (TranscriptEntry)
             if isinstance(fetched_data, list):
                for item in fetched_data:
                        if hasattr(item, 'text'):
                            texts.append(getattr(item, 'text', ''))
                return " ".join(t.strip() for t in texts if t and t.strip())
        logging.warning(f"RecipeCog: 予期せぬ字幕データ形式: {type(fetched_data)}")
        return ""


    # ★ process_pending_summaries の引数を変更
    async def process_pending_summaries(self, channel_id: int, recipe_channel_id: int):
        """起動時などに未処理の要約リクエストをまとめて処理する関数"""
        
        # ★ 2つのチャンネルをスキャンする
        scan_channels = {channel_id, recipe_channel_id}
        scan_channels.discard(0) # 0が設定されていたら除外
        
        pending_messages = []
        
        for ch_id in scan_channels:
            channel = self.bot.get_channel(ch_id)
            if not channel:
                logging.error(f"YouTubeCog: チャンネルID {ch_id} が見つかりません。")
                continue

            logging.info(f"チャンネル '{channel.name}' の未処理YouTube要約をスキャンします...")
            
            try:
                # 制限は多すぎないように (例: 100件)
                async for message in channel.history(limit=100): 
                    
                    has_pending_trigger_by_bot = False # 📥 (Botが付けた)
                    is_processed_by_local = False # ✅, ❌, 🔇... (by local)
                    is_stuck_processing_local = False # ⏳ (by local)

                    for r in message.reactions:
                        emoji_str = str(r.emoji)

                        if emoji_str == BOT_PROCESS_TRIGGER_REACTION: # 📥
                            if r.me: # このBot (local) が付けた 📥 か？
                                has_pending_trigger_by_bot = True
                        
                        if emoji_str in (
                            PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, TRANSCRIPT_NOT_FOUND_EMOJI, 
                            INVALID_URL_EMOJI, SUMMARY_ERROR_EMOJI, SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
                        ) and r.me:
                            is_processed_by_local = True
                        
                        if emoji_str == PROCESS_START_EMOJI and r.me: # ⏳
                            is_stuck_processing_local = True 
                            logging.info(f"Message {message.id}: ⏳ (Stuck) を検知。")

                    # (📥 がBotによって付けられている OR ⏳ でスタックしている) AND (まだ処理完了していない)
                    if (has_pending_trigger_by_bot or is_stuck_processing_local) and not is_processed_by_local:
                        logging.info(f"Message {message.id} (Ch: {channel.name}): 処理対象に追加します。")
                        pending_messages.append(message) # メッセージだけ追加
                
            except discord.Forbidden:
                logging.error(f"チャンネル {channel.name} の履歴読み取り権限がありません。")
            except discord.HTTPException as e:
                logging.error(f"チャンネル {channel.name} の履歴読み取り中にエラー: {e}")


        if not pending_messages:
            logging.info("処理対象の新しいYouTube要約はありませんでした。")
            return

        # 時刻順（古い順）にソートして処理
        pending_messages.sort(key=lambda m: m.created_at)
        
        logging.info(f"{len(pending_messages)}件の未処理YouTube要約が見つかりました。古いものから順に処理します...")
        
        for message in pending_messages:
            logging.info(f"処理開始: {message.jump_url}")
            url = message.content.strip()
            
            try:
                await message.clear_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                logging.warning(f"📥 リアクションのクリアに失敗しました: {e}")
            
            try:
                await message.clear_reaction(PROCESS_START_EMOJI)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass 
            
            # ★ message オブジェクトを渡す
            await self._perform_summary(url=url, message=message)
            await asyncio.sleep(5) # 連続処理のための待機


    async def _perform_summary(self, url: str, message: discord.Message | discord.InteractionMessage):
        """YouTube要約処理のコアロジック (チャンネルIDに応じて処理分岐)"""
        obsidian_save_success = False
        gdoc_save_success = False
        error_reactions = set()
        video_title = "Untitled Video"
        video_id = None
        transcript_text = ""

        # ★ チャンネルIDに基づいてAIプロンプトと保存先を決定
        is_recipe_channel = (message.channel.id == self.recipe_channel_id)
        
        if is_recipe_channel:
            logging.info("RecipeCog (YouTube) として処理を開始します。")
            save_folder = "/Recipes" # ★ 保存先フォルダ
            daily_note_header = "## Recipes" # ★ デイリーノートの見出し
            gdoc_source_type = "Recipe (YouTube)"
            concise_prompt = (
                f"以下のYouTube動画の文字起こしから、レシピ情報（材料と作り方）を抽出し、簡潔なMarkdown形式で要約してください。\n"
                f"「## 材料」と「## 作り方」の2つのセクションを必ず作成してください。\n"
                f"材料は箇条書き（-）で、作り方は番号付きリスト（1. ...）で記述してください。\n"
                f"それ以外の情報（導入、感想など）は含めないでください。\n\n"
                f"--- 文字起こし全文 ---\n"
            )
            # レシピの場合、詳細要約は不要
            detail_prompt = None 
        else:
            logging.info("YouTubeCog (General) として処理を開始します。")
            save_folder = "/YouTube" # ★ 保存先フォルダ
            daily_note_header = "## YouTube Summaries" # ★ デイリーノートの見出し
            gdoc_source_type = "YouTube Transcript"
            concise_prompt = (
                "以下のYouTube動画の文字起こし全文を元に、重要なポイントを3～5点で簡潔にまとめてください。\n"
                "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"--- 文字起こし全文 ---\n"
            )
            detail_prompt = (
                "以下のYouTube動画の文字起こし全文を元に、その内容を網羅する詳細で包括的な要約を作成してください。\n"
                "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"--- 文字起こし全文 ---\n"
            )

        try:
            if isinstance(message, discord.Message):
                try: await message.add_reaction(PROCESS_START_EMOJI)
                except discord.HTTPException: pass

            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                if isinstance(message, discord.Message): error_reactions.add(INVALID_URL_EMOJI)
                raise ValueError("Invalid YouTube URL")
            video_id = video_id_match.group(1)

            # --- 字幕取得ロジック ---
            try:
                api = YouTubeTranscriptApi() 
                fetched = await asyncio.to_thread(
                    api.fetch,
                    video_id,
                    languages=['ja', 'en']
                )
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
            # --- 字幕取得ロジックここまで ---

            # --- AI要約 ---
            concise_summary = "(要約対象なし)"
            detail_summary = "(対象外)" if is_recipe_channel else "(要約対象なし)"
            
            if transcript_text and self.gemini_model:
                try:
                    tasks = []
                    # 簡潔（またはレシピ）要約タスク
                    tasks.append(self.gemini_model.generate_content_async(concise_prompt + transcript_text))
                    
                    # 詳細要約タスク（レシピでない場合のみ）
                    if detail_prompt:
                        tasks.append(self.gemini_model.generate_content_async(detail_prompt + transcript_text))

                    responses = await asyncio.gather(*[asyncio.wait_for(task, timeout=300) for task in tasks], return_exceptions=True)

                    # 簡潔（レシピ）要約の結果
                    if isinstance(responses[0], (Exception, asyncio.TimeoutError)):
                         concise_summary = f"({('レシピ' if is_recipe_channel else '簡潔')}要約エラー: {type(responses[0]).__name__})"
                         error_reactions.add(SUMMARY_ERROR_EMOJI)
                    elif hasattr(responses[0], 'text'): concise_summary = responses[0].text
                    else: concise_summary = "(要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                    # 詳細要約の結果（レシピでない場合のみ）
                    if detail_prompt:
                        if isinstance(responses[1], (Exception, asyncio.TimeoutError)):
                             detail_summary = f"(詳細要約エラー: {type(responses[1]).__name__})"
                             error_reactions.add(SUMMARY_ERROR_EMOJI)
                        elif hasattr(responses[1], 'text'): detail_summary = responses[1].text
                        else: detail_summary = "(詳細要約応答不正)"; error_reactions.add(SUMMARY_ERROR_EMOJI)

                    if not error_reactions.intersection({SUMMARY_ERROR_EMOJI}): logging.info(f"AI summaries generated for {video_id} (Channel: {message.channel.name})")

                except Exception as e_gather:
                    logging.error(f"AI summary gather failed: {e_gather}", exc_info=True)
                    concise_summary = detail_summary = "(AI要約プロセスエラー)"
                    if isinstance(message, discord.Message): error_reactions.add(SUMMARY_ERROR_EMOJI)

            elif not self.gemini_model: concise_summary = detail_summary = "(AI要約機能無効)"; error_reactions.add(SUMMARY_ERROR_EMOJI)
            elif not transcript_text: concise_summary = detail_summary = "(字幕なしのため要約不可)"

            # ★ Discordへの投稿 (レシピチャンネルの場合のみ)
            if is_recipe_channel and isinstance(message, discord.Message):
                try:
                    video_info_for_embed = await self.get_video_info(video_id)
                    title_for_embed = video_info_for_embed.get("title", f"YouTube_{video_id}")
                    embed = discord.Embed(
                        title=f"🍳 レシピ要約 (YT): {title_for_embed}",
                        url=url,
                        description=concise_summary, # レシピ要約
                        color=discord.Color.orange()
                    )
                    await message.reply(embed=embed, mention_author=False)
                except Exception as e_discord:
                    logging.error(f"RecipeCog (YT): Discordへの要約投稿失敗: {e_discord}")

            # --- 保存準備 ---
            now = datetime.datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')
            video_info = await self.get_video_info(video_id)
            video_title = video_info.get("title", f"YouTube_{video_id}")
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)[:100]
            if not safe_title: safe_title = f"YouTube_{video_id}"
            note_filename = f"{timestamp}-{safe_title}.md"
            note_filename_for_link = note_filename.replace('.md', '')

            # --- Obsidian用ノート内容 (★ チャンネルに応じて分岐) ---
            if is_recipe_channel:
                note_content = (
                    f"# {video_title}\n\n"
                    f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>\n\n'
                    f"- **URL:** {url}\n"
                    f"- **Channel:** {video_info.get('author_name', 'N/A')}\n"
                    f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"[[{daily_note_date}]]\n\n"
                    f"---\n\n"
                    f"{concise_summary}" # レシピ要約のみ
                )
            else:
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

            # --- Obsidianへの保存 (★ 保存先フォルダとデイリーノート見出しを動的に) ---
            if self.dbx:
                try:
                    # ★ save_folder を使用
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

                    # ★ リンクパスと見出しを動的に
                    link_path_part = save_folder.lstrip('/')
                    link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{video_title}]]" 
                    youtube_heading = daily_note_header # ★ daily_note_header を使用
                    
                    new_daily_content = update_section(daily_note_content, link_to_add, youtube_heading)

                    await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
                    logging.info(f"Daily note updated with link: {daily_note_path} (Header: {youtube_heading})")
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

            # --- Google Docsへの保存 (★ gdoc_source_type を動的に) ---
            if google_docs_enabled:
                gdoc_text_to_append = ""
                # gdoc_source_type は先頭で設定済み
                
                if is_recipe_channel:
                    # レシピの場合は、要約＋文字起こし
                    gdoc_text_to_append = f"## レシピ要約 (AI)\n{concise_summary}\n\n## 文字起こし（抜粋）\n{transcript_text[:3000]}..."
                elif transcript_text:
                    # 通常のYouTubeの場合は、文字起こし全文
                    gdoc_text_to_append = transcript_text
                elif video_id:
                    error_reason = "(字幕なしまたは取得失敗)"
                    if TRANSCRIPT_NOT_FOUND_EMOJI in error_reactions: error_reason = "(字幕なしまたは取得失敗)"
                    if PROCESS_ERROR_EMOJI in error_reactions: error_reason = "(字幕取得エラー)"
                    gdoc_text_to_append = error_reason
                    gdoc_source_type = "YouTube Link (No Transcript)" # ソースタイプを上書き

                if gdoc_text_to_append:
                    try:
                        await append_text_to_doc_async(
                            text_to_append=gdoc_text_to_append,
                            source_type=gdoc_source_type, # ★ 動的に設定
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

    @app_commands.command(name="yt_summary", description="[手動] YouTube動画URLをObsidian/Google Docsに保存します。")
    @app_commands.describe(url="処理したいYouTube動画のURL")
    async def yt_summary_command(self, interaction: discord.Interaction, url: str):
        if not self.is_ready:
             await interaction.response.send_message("❌ YouTube Cogが初期化されていません。", ephemeral=True)
             return
             
        # ★ スラッシュコマンドは「レシピ」か「通常」か判断できない
        # 暫定的に、インタラクションが飛んできたチャンネルIDで判断する
        target_channel_id = interaction.channel_id
        if target_channel_id not in [self.youtube_summary_channel_id, self.recipe_channel_id]:
            await interaction.response.send_message(f"❌ このコマンドは <#{self.youtube_summary_channel_id}> または <#{self.recipe_channel_id}> でのみ実行できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        message_proxy = await interaction.original_response()

        # スラッシュコマンド用のダミーメッセージラッパー
        class TempMessage:
             def __init__(self, proxy, content, channel):
                 self.id = proxy.id; self.reactions = []; self.channel = channel; self.jump_url = proxy.jump_url; self._proxy = proxy; self.content=content
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji)
                 except: pass
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user)
                 except: pass

        # ★ TempMessage に interaction.channel を渡す
        temp_msg = TempMessage(message_proxy, content=url, channel=interaction.channel)
        await self._perform_summary(url=url, message=temp_msg)

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
    # ★ 必須IDチェックに RECIPE_CHANNEL_ID を追加
    if int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0)) == 0 or int(os.getenv("RECIPE_CHANNEL_ID", 0)) == 0:
        logging.error("YouTubeCog: YOUTUBE_SUMMARY_CHANNEL_ID または RECIPE_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    cog_instance = YouTubeCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("YouTubeCog loaded successfully.")
    else:
        logging.error("YouTubeCog failed to initialize properly and was not loaded.")
        del cog_instance