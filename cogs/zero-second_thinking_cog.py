import os
import discord
from discord.ext import commands, tasks
import logging
import aiohttp
import openai
import google.generativeai as genai
from datetime import datetime, time
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re

from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]
THINKING_TIMES = [
    time(hour=9, minute=0, tzinfo=JST),
    time(hour=12, minute=0, tzinfo=JST),
    time(hour=15, minute=0, tzinfo=JST),
    time(hour=18, minute=0, tzinfo=JST),
    time(hour=21, minute=0, tzinfo=JST),
]

class ZeroSecondThinkingCog(commands.Cog):
    """
    Discord上でゼロ秒思考を支援するためのCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.channel_id = int(os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID", 0))
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        self.user_states = {} # { "user_id": { "last_question": "...", "last_answer": "...", "note_path": "..." } }

        # --- 初期チェックとAPIクライアント初期化 ---
        if not all([self.channel_id, self.openai_api_key, self.gemini_api_key, self.dropbox_refresh_token]):
            logging.warning("ZeroSecondThinkingCog: 必要な環境変数が不足しています。")
            self.is_ready = False
        else:
            self.session = aiohttp.ClientSession()
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            self.is_ready = True

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            self.thinking_prompt_loop.start()
            logging.info(f"ゼロ秒思考の定時通知タスクを開始しました。")

    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        if self.is_ready:
            await self.session.close()
            self.thinking_prompt_loop.cancel()

    @tasks.loop(time=THINKING_TIMES)
    async def thinking_prompt_loop(self):
        """定時にお題を投稿するループ"""
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        
        try:
            prompt = (
                "あなたはこれから、私が「ゼロ秒思考」を行うのを支援します。\n"
                "ゼロ秒思考とは、赤羽雄二氏が提唱する、A4用紙に1件1ページのメモを1分以内に書き、深く考える習慣です。\n"
                "これから私がこのゼロ秒思考を行いますので、ゼロ秒思考を行うのに適したお題を1つ、前置きや返答を一切含めずに生成してください。\n"
                "テーマはビジネス、自己啓発、プライベートなど多岐にわたりますが、深い洞察を促すような問いをお願いします。"
            )
            response = await self.gemini_model.generate_content_async(prompt)
            question = response.text.strip()
            
            embed = discord.Embed(title="🤔 ゼロ秒思考の時間です", description=f"お題: **{question}**", color=discord.Color.teal())
            embed.set_footer(text="このメッセージに返信する形で、思考を書き出してください（音声入力も可能です）。")
            await channel.send(embed=embed)
            
        except Exception as e:
            logging.error(f"[Zero-Second Thinking] 定時お題生成エラー: {e}", exc_info=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、Zero-Second Thinkingのフローを処理する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return
        # ボットのメッセージへの返信か確認
        if not message.reference or not message.reference.message_id:
            return

        channel = self.bot.get_channel(self.channel_id)
        original_msg = await channel.fetch_message(message.reference.message_id)

        if original_msg.author.id != self.bot.user.id or not original_msg.embeds:
            return
            
        embed_title = original_msg.embeds[0].title
        if "ゼロ秒思考の時間です" not in embed_title:
            return
            
        # 埋め込みからお題を抽出
        last_question_match = re.search(r'お題: \*\*(.+?)\*\*', original_msg.embeds[0].description)
        if not last_question_match:
            return
        last_question = last_question_match.group(1)

        # 音声 or テキストで処理
        if message.attachments and any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
             await self._process_thinking_memo(message, last_question, message.attachments[0])
        elif message.content:
             await self._process_thinking_memo(message, last_question)

    async def _process_thinking_memo(self, message: discord.Message, last_question: str, attachment: discord.Attachment = None):
        """思考メモを処理し、Obsidianに記録する"""
        temp_audio_path = None
        try:
            await message.add_reaction("⏳")

            if attachment: # 音声入力の場合
                temp_audio_path = Path(f"./temp_{attachment.filename}")
                async with self.session.get(attachment.url) as resp:
                    if resp.status == 200:
                        with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                
                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                transcribed_text = transcription.text

                formatting_prompt = (
                    "以下の音声メモの文字起こしを、構造化された箇条書きのMarkdown形式でまとめてください。\n"
                    "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                    f"---\n\n{transcribed_text}"
                )
                response = await self.gemini_model.generate_content_async(formatting_prompt)
                formatted_answer = response.text.strip()
            else: # テキスト入力の場合
                formatted_answer = message.content

            # --- Obsidianへの保存処理 ---
            now = datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            
            safe_title = re.sub(r'[\\/*?:"<>|]', "", last_question)
            if not safe_title: safe_title = "Untitled"
            timestamp = now.strftime('%Y%m%d%H%M%S')
            note_filename = f"{timestamp}-{safe_title}.md"
            note_path = f"{self.dropbox_vault_path}/Zero-Second Thinking/{note_filename}"

            new_note_content = (
                f"# {last_question}\n\n"
                f"- **Source:** Discord Voice/Text Memo\n"
                f"- **作成日:** {daily_note_date}\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"## 回答\n{formatted_answer}"
            )
            self.dbx.files_upload(new_note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"[Zero-Second Thinking] 新規ノートを作成: {note_path}")

            # --- デイリーノートへのリンク追記 ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
            daily_note_content = ""
            try:
                _, res = self.dbx.files_download(daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    pass
                else: raise
            
            note_filename_for_link = note_filename.replace('.md', '')
            link_to_add = f"- [[Zero-Second Thinking/{note_filename_for_link}]]"
            section_header = "## Zero-Second Thinking"
            new_daily_content = update_section(daily_note_content, link_to_add, section_header)
            
            self.dbx.files_upload(new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))

            await message.channel.send(f"**思考が記録されました**\n>>> {formatted_answer}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] 処理中にエラー: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
            except discord.HTTPException: pass
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)


async def setup(bot: commands.Bot):
    """CogをBotに追加する"""
    await bot.add_cog(ZeroSecondThinkingCog(bot))