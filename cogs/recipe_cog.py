import os
import discord
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

# ユーティリティをインポート
from utils.obsidian_utils import update_section
from web_parser import parse_url_with_readability # Webパーサー
from google_docs_handler import append_text_to_doc_async

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
RECIPE_CHANNEL_ID = int(os.getenv("RECIPE_CHANNEL_ID", 0))
# YouTube Regex (このCogでは使用しない)
# YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|...)([a-zA-Z0-9_-]{11})')
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
PARSE_ERROR_EMOJI = '📄' # 記事本文の解析失敗
SUMMARY_ERROR_EMOJI = '⚠️' # AI要約失敗
SAVE_ERROR_EMOJI = '💾' # Obsidian保存失敗
GOOGLE_DOCS_ERROR_EMOJI = '🇬' # Google Docs保存失敗

class RecipeCog(commands.Cog, name="RecipeCog"):
    """
    #recipe チャンネルの 📥 リアクションを検知し、
    「Webサイト」のURLからレシピ情報をAIで要約し、保存するCog
    (main.py でロードされる)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- .envからの読み込み ---
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY") # dbx初期化用
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET") # dbx初期化用
        
        # --- クライアント初期化 ---
        self.dbx = None
        self.gemini_model = None
        self.session = None
        self.is_ready = False

        # --- 必須変数のチェック ---
        missing_vars = []
        if not RECIPE_CHANNEL_ID: missing_vars.append("RECIPE_CHANNEL_ID")
        if not self.dropbox_refresh_token: missing_vars.append("DROPBOX_REFRESH_TOKEN")
        if not self.gemini_api_key: missing_vars.append("GEMINI_API_KEY")

        if missing_vars:
            logging.error(f"RecipeCog (Web): 必要な環境変数 ({', '.join(missing_vars)}) が不足。Cogは動作しません。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret, timeout=300
            )
            self.dbx.users_get_current_account()
            logging.info("RecipeCog (Web): Dropbox client initialized.")

            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            logging.info("RecipeCog (Web): Gemini client initialized.")

            self.session = aiohttp.ClientSession()
            logging.info("RecipeCog (Web): aiohttp session started.")

            self.is_ready = True
        except Exception as e:
            logging.error(f"RecipeCog (Web): Failed to initialize clients: {e}", exc_info=True)

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Bot(自分自身)が付けた 📥 リアクションを検知して処理を開始"""
        
        if payload.channel_id != RECIPE_CHANNEL_ID:
            return
            
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION:
            return
            
        # このCog(main.py)で動くBot自身のリアクション(memo_cogが付けたもの)を検知
        if payload.user_id != self.bot.user.id:
            return 

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.warning(f"RecipeCog (Web): メッセージの取得に失敗: {payload.message_id}")
            return

        # 既に処理中・処理済みのリアクションがあるか確認
        is_processed = any(r.emoji in (
            PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, 
            PARSE_ERROR_EMOJI, SUMMARY_ERROR_EMOJI, SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
            ) and r.me for r in message.reactions)
        
        if is_processed:
            logging.info(f"RecipeCog (Web): 既に処理中または処理済みのメッセージ: {message.jump_url}")
            return

        logging.info(f"RecipeCog (Web): '{BOT_PROCESS_TRIGGER_REACTION}' を検知。Webレシピ要約処理を開始: {message.jump_url}")
        
        try:
            # 📥 リアクションは main bot (self.bot.user) が消す
            await message.remove_reaction(payload.emoji, self.bot.user)
        except discord.HTTPException:
            pass

        # メインの処理を実行
        await self._perform_recipe_summary(message)


    async def _perform_recipe_summary(self, message: discord.Message):
        """レシピの取得・要約・保存のコアロジック (Webサイト版)"""
        
        obsidian_save_success = False
        gdoc_save_success = False
        error_reactions = set()
        
        url = message.content.strip()
        source_content = "" # 記事本文
        title = "不明なレシピ"

        try:
            await message.add_reaction(PROCESS_START_EMOJI)

            # --- 1. ソースコンテンツの取得 (Webサイト) ---
            logging.info(f"RecipeCog (Web): ウェブサイトを処理します: {url}")
            try:
                # Discord Embedからタイトルを優先取得
                if message.embeds:
                    embed_title = message.embeds[0].title
                    if embed_title and embed_title != discord.Embed.Empty:
                        title = embed_title
                
                loop = asyncio.get_running_loop()
                parsed_title, parsed_content = await loop.run_in_executor(
                    None, parse_url_with_readability, url
                )
                
                if title == "不明なレシピ" and parsed_title and parsed_title != "No Title Found":
                    title = parsed_title
                source_content = parsed_content
                
                if not source_content or "取得に失敗しました" in source_content:
                     logging.warning(f"RecipeCog (Web): web_parserが本文取得に失敗: {url}")
                     error_reactions.add(PARSE_ERROR_EMOJI)
                     
            except Exception as e_web:
                logging.error(f"RecipeCog (Web): web_parser実行エラー: {e_web}", exc_info=True)
                error_reactions.add(PROCESS_ERROR_EMOJI)

            # --- 2. AIによる要約 ---
            recipe_summary = "(AI要約失敗)"
            if source_content and not error_reactions.intersection({PARSE_ERROR_EMOJI, PROCESS_ERROR_EMOJI}):
                try:
                    logging.info(f"RecipeCog (Web): AI要約を開始します (Title: {title})...")
                    prompt = f"""
                    以下のWebページの内容から、レシピ情報（材料と作り方）を抽出し、簡潔なMarkdown形式で要約してください。
                    
                    # 指示
                    - 「## 材料」と「## 作り方」の2つのセクションを必ず作成してください。
                    - 材料は箇条書き（-）でリストアップしてください（分量もあれば含める）。
                    - 作り方は番号付きリスト（1. 2. ...）で手順を説明してください。
                    - それ以外の余計な情報（導入、感想、関連リンクなど）は含めないでください。
                    - 材料または作り方が見つからない場合は、「見つかりませんでした。」と記載してください。

                    # ソースコンテンツ
                    {source_content[:15000]}
                    """ # 長すぎるコンテンツをAI APIの制限に合わせる
                    
                    response = await self.gemini_model.generate_content_async(prompt)
                    recipe_summary = response.text.strip()
                    logging.info("RecipeCog (Web): AI要約が完了。")
                    
                except Exception as e_ai:
                    logging.error(f"RecipeCog (Web): Gemini要約エラー: {e_ai}", exc_info=True)
                    error_reactions.add(SUMMARY_ERROR_EMOJI)
            elif not source_content:
                recipe_summary = "(ソースコンテンツが取得できなかったため要約不可)"

            # --- 3. Discordに投稿 ---
            try:
                embed = discord.Embed(
                    title=f"🍳 レシピ要約 (Web): {title}",
                    url=url,
                    description=recipe_summary,
                    color=discord.Color.orange()
                )
                await message.reply(embed=embed, mention_author=False)
            except Exception as e_discord:
                logging.error(f"RecipeCog (Web): Discordへの要約投稿失敗: {e_discord}")

            # --- 4. Obsidianに保存 ---
            try:
                safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:100]
                if not safe_title: safe_title = f"Recipe_{message.id}"
                
                now = datetime.datetime.now(JST)
                timestamp = now.strftime('%Y%m%d%H%M%S')
                daily_note_date = now.strftime('%Y-%m-%d')
                
                note_filename = f"{timestamp}-{safe_title}.md"
                note_path = f"{self.dropbox_vault_path}/Recipes/{note_filename}" # "Recipes" フォルダ

                note_content = (
                    f"# {title}\n\n"
                    f"- **Source:** <{url}>\n"
                    f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n"
                    f"[[{daily_note_date}]]\n\n"
                    f"---\n\n"
                    f"{recipe_summary}"
                )
                
                # ノート本体を保存
                await asyncio.to_thread(
                    self.dbx.files_upload,
                    note_content.encode('utf-8'),
                    note_path,
                    mode=WriteMode('add')
                )
                logging.info(f"RecipeCog (Web): レシピノートをObsidianに保存: {note_path}")

                # デイリーノートにリンクを追記
                daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                daily_note_content = ""
                try:
                    _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                    daily_note_content = res.content.decode('utf-8')
                except ApiError as e_dn:
                    if isinstance(e_dn.error, DownloadError) and e_dn.error.is_path() and e_dn.error.get_path().is_not_found():
                        daily_note_content = f"# {daily_note_date}\n"
                    else: raise

                note_filename_for_link = note_filename.replace('.md', '')
                link_to_add = f"- [[Recipes/{note_filename_for_link}|{title}]]" 
                section_header = "## Recipes" # utils/obsidian_utils.py で定義した見出し
                
                new_daily_content = update_section(daily_note_content, link_to_add, section_header)

                await asyncio.to_thread(
                    self.dbx.files_upload,
                    new_daily_content.encode('utf-8'),
                    daily_note_path,
                    mode=WriteMode('overwrite')
                )
                logging.info(f"RecipeCog (Web): デイリーノートにリンクを追記: {daily_note_path}")
                obsidian_save_success = True

            except Exception as e_obs:
                logging.error(f"RecipeCog (Web): Obsidian保存エラー: {e_obs}", exc_info=True)
                error_reactions.add(SAVE_ERROR_EMOJI)

            # --- 5. Google Docsに保存 ---
            try:
                gdoc_content = f"## レシピ要約\n{recipe_summary}\n\n## ソースコンテンツ（抜粋）\n{source_content[:3000]}..."
                await append_text_to_doc_async(
                    text_to_append=gdoc_content,
                    source_type="Recipe (Web)",
                    url=url,
                    title=title
                )
                gdoc_save_success = True
                logging.info(f"RecipeCog (Web): Google Docsにレシピを保存しました: {url}")
            except Exception as e_gdoc:
                logging.error(f"RecipeCog (Web): Google Docs保存エラー: {e_gdoc}", exc_info=True)
                error_reactions.add(GOOGLE_DOCS_ERROR_EMOJI)

            # --- 6. 最終リアクション ---
            if obsidian_save_success:
                await message.add_reaction(PROCESS_COMPLETE_EMOJI)
            
            for reaction in error_reactions:
                await message.add_reaction(reaction)

        except Exception as e_main:
            logging.error(f"RecipeCog (Web): 処理全体で予期せぬエラー: {e_main}", exc_info=True)
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass

# --- Cogセットアップ ---
async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if RECIPE_CHANNEL_ID == 0:
        logging.error("RecipeCog (Web): RECIPE_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    cog_instance = RecipeCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("RecipeCog (Web) loaded successfully.")
    else:
        logging.error("RecipeCog (Web) failed to initialize properly and was not loaded.")