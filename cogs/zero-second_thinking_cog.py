import os
import discord
from discord.ext import commands
import logging
import aiohttp
import openai
import google.generativeai as genai
from datetime import datetime
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]
SECTION_ORDER = [
    "## WebClips",
    "## YouTube Summaries",
    "## AI Logs",
    "## Zero-Second Thinking",
    "## Memo"
]


class ZeroSecondThinkingCog(commands.Cog):
    """
    Discord上でゼロ秒思考を支援するためのCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.zero_second_thinking_channel_id = int(os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID", 0))
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        # --- ユーザーの状態を管理 ---
        self.user_states = {}

        # --- 初期チェック ---
        if not self.zero_second_thinking_channel_id:
            logging.warning("ZeroSecondThinkingCog: ZERO_SECOND_THINKING_CHANNEL_IDが設定されていません。")
        if not self.openai_api_key:
            logging.warning("ZeroSecondThinkingCog: OPENAI_API_KEYが設定されていません。")
        if not self.gemini_api_key:
            logging.warning("ZeroSecondThinkingCog: GEMINI_API_KEYが設定されていません。")
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("ZeroSecondThinkingCog: Dropboxの認証情報が不足しています。")

        # --- APIクライアントの初期化 ---
        self.session = aiohttp.ClientSession()
        if self.openai_api_key:
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)

    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        await self.session.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、Zero-Second Thinkingのフローを処理する"""
        # --- 処理実行の条件判定 ---
        if message.author.bot:
            return
        if message.channel.id != self.zero_second_thinking_channel_id:
            return

        user_id = message.author.id

        # --- テキストメッセージの処理 ---
        if message.content.lower() == 's':
            await self._start_new_session(message)
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass
            return
        
        if message.content.lower() == 'd':
            await self._dig_deeper(message)
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass
            return

        # --- 音声メッセージ（添付ファイル）の処理 ---
        if message.attachments and any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
            if user_id in self.user_states and 'last_question' in self.user_states[user_id]:
                await self._process_thinking_memo(message, message.attachments[0])
            else:
                return

    async def _start_new_session(self, message: discord.Message):
        """新しいZero-Second Thinkingセッションを開始する"""
        user_id = message.author.id
        try:
            async with message.channel.typing():
                model = genai.GenerativeModel("gemini-2.5-pro")
                prompt = (
                    "あなたはこれから、私が「ゼロ秒思考」を行うのを支援します。\n"
                    "ゼロ秒思考とは、赤羽雄二氏が提唱する、A4用紙に1件1ページのメモを1分以内に書き、深く考える習慣です。\n"
                    "これから私がこのゼロ秒思考を行いますので、ゼロ秒思考を行うのに適したお題を1つ、前置きや返答を一切含めずに生成してください。\n"
                    "テーマはビジネス、自己啓発、プライベートなど多岐にわたりますが、深い洞察を促すような問いをお願いします。"
                )
                response = await model.generate_content_async(prompt)
                question = response.text.strip()

                self.user_states[user_id] = {'last_question': question}
                
                await message.channel.send(f"お題: **{question}**")
                logging.info(f"[Zero-Second Thinking] New session for {message.author}: {question}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Failed to start new session: {e}", exc_info=True)
            await message.channel.send(f"エラーが発生しました: {e}")

    async def _dig_deeper(self, message: discord.Message):
        """既存の思考を深掘りする"""
        user_id = message.author.id
        state = self.user_states.get(user_id)

        if not state or 'last_question' not in state or 'last_answer' not in state:
            await message.channel.send("先に `s` で新しい思考を開始し、一度音声で回答してください。")
            return

        try:
            async with message.channel.typing():
                model = genai.GenerativeModel("gemini-2.5-pro")
                prompt = (
                    "以下の『これまでの文脈』を踏まえて、思考をさらに深掘りするための、鋭い問いを1つだけ、前置きや返答を一切含めずに生成してください。\n\n"
                    "# これまでの文脈\n"
                    f"## 問い\n{state['last_question']}\n\n"
                    f"## 回答\n{state['last_answer']}"
                )
                response = await model.generate_content_async(prompt)
                new_question = response.text.strip()

                self.user_states[user_id]['last_question'] = new_question
                
                await message.channel.send(f"次の問い: **{new_question}**")
                logging.info(f"[Zero-Second Thinking] Digging deeper for {message.author}: {new_question}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Failed to dig deeper: {e}", exc_info=True)
            await message.channel.send(f"エラーが発生しました: {e}")

    def _update_daily_note_with_ordered_section(self, current_content: str, link_to_add: str, section_header: str) -> str:
        """定義された順序に基づいてデイリーノートのコンテンツを更新する"""
        lines = current_content.split('\n')
        
        try:
            header_index = lines.index(section_header)
            insert_index = header_index + 1
            while insert_index < len(lines) and (lines[insert_index].strip().startswith('- ') or not lines[insert_index].strip()):
                insert_index += 1
            lines.insert(insert_index, link_to_add)
            return "\n".join(lines)
        except ValueError:
            existing_sections = {line.strip(): i for i, line in enumerate(lines) if line.strip() in SECTION_ORDER}
            
            insert_after_index = -1
            new_section_order_index = SECTION_ORDER.index(section_header)
            for i in range(new_section_order_index - 1, -1, -1):
                preceding_header = SECTION_ORDER[i]
                if preceding_header in existing_sections:
                    header_line_index = existing_sections[preceding_header]
                    insert_after_index = header_line_index + 1
                    while insert_after_index < len(lines) and not lines[insert_after_index].strip().startswith('## '):
                        insert_after_index += 1
                    break
            
            if insert_after_index != -1:
                lines.insert(insert_after_index, f"\n{section_header}\n{link_to_add}")
                return "\n".join(lines)

            insert_before_index = -1
            for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
                following_header = SECTION_ORDER[i]
                if following_header in existing_sections:
                    insert_before_index = existing_sections[following_header]
                    break
            
            if insert_before_index != -1:
                lines.insert(insert_before_index, f"{section_header}\n{link_to_add}\n")
                return "\n".join(lines)

            if current_content.strip():
                 lines.append("")
            lines.append(section_header)
            lines.append(link_to_add)
            return "\n".join(lines)

    async def _process_thinking_memo(self, message: discord.Message, attachment: discord.Attachment):
        """音声メモを処理し、思考を記録するコアロジック"""
        temp_audio_path = None
        user_id = message.author.id
        state = self.user_states.get(user_id, {})
        last_question = state.get('last_question', '不明なお題')

        try:
            await message.add_reaction("⏳")
            temp_audio_path = Path(f"./temp_{attachment.filename}")

            async with self.session.get(attachment.url) as resp:
                if resp.status == 200:
                    with open(temp_audio_path, 'wb') as f:
                        f.write(await resp.read())
                else:
                    raise Exception(f"音声ファイルのダウンロードに失敗: Status {resp.status}")

            with open(temp_audio_path, "rb") as audio_file:
                transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            transcribed_text = transcription.text

            model = genai.GenerativeModel("gemini-2.5-pro")
            formatting_prompt = (
                "以下の音声メモの文字起こしを、構造化された箇条書きのMarkdown形式でまとめてください。\n"
                "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"---\n\n{transcribed_text}"
            )
            response = await model.generate_content_async(formatting_prompt)
            formatted_answer = response.text.strip()

            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                now = datetime.now(JST)
                daily_note_date = now.strftime('%Y-%m-%d')
                
                if 'note_path' in state:
                    note_path = state['note_path']
                    _, res = dbx.files_download(note_path)
                    current_content = res.content.decode('utf-8')
                    content_to_append = (
                        f"\n\n---\n\n"
                        f"### ▼ {last_question}\n"
                        f"{formatted_answer}"
                    )
                    new_content = current_content + content_to_append
                    dbx.files_upload(new_content.encode('utf-8'), note_path, mode=WriteMode('overwrite'))
                    logging.info(f"[Zero-Second Thinking] Appended to note for {message.author}: {note_path}")

                else:
                    safe_title = re.sub(r'[\\/*?:"<>|]', "", last_question)
                    if not safe_title: safe_title = "Untitled"
                    timestamp = now.strftime('%Y%m%d%H%M%S')
                    note_filename = f"{timestamp}-{safe_title}.md"
                    note_path = f"{self.dropbox_vault_path}/Zero-Second Thinking/{note_filename}"
                    new_note_content = (
                        f"# {last_question}\n\n"
                        f"- **Source:** Discord Voice Memo\n"
                        f"- **作成日:** {daily_note_date}\n\n"
                        f"[[{daily_note_date}]]\n\n"
                        f"---\n\n"
                        f"## 回答\n"
                        f"{formatted_answer}"
                    )
                    dbx.files_upload(new_note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
                    logging.info(f"[Zero-Second Thinking] Created new note for {message.author}: {note_path}")
                    
                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                    daily_note_content = ""
                    try:
                        _, res = dbx.files_download(daily_note_path)
                        daily_note_content = res.content.decode('utf-8')
                    except ApiError as e:
                        if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                            pass
                        else:
                            raise
                    
                    note_filename_for_link = note_filename.replace('.md', '')
                    link_to_add = f"- [[Zero-Second Thinking/{note_filename_for_link}]]"
                    section_header = "## Zero-Second Thinking"

                    new_daily_content = self._update_daily_note_with_ordered_section(
                        daily_note_content, link_to_add, section_header
                    )
                    
                    dbx.files_upload(
                        new_daily_content.encode('utf-8'),
                        daily_note_path,
                        mode=WriteMode('overwrite')
                    )

                    self.user_states[user_id]['note_path'] = note_path

            await message.channel.send(f"**思考が記録されました**\n{formatted_answer}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
            
            self.user_states[user_id]['last_answer'] = formatted_answer
            logging.info(f"[Zero-Second Thinking] Successfully processed for {message.author}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Error during processing: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

async def setup(bot: commands.Bot):
    """CogをBotに追加する"""
    if not all([os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID"),
                os.getenv("OPENAI_API_KEY"), 
                os.getenv("GEMINI_API_KEY"), 
                os.getenv("DROPBOX_REFRESH_TOKEN")]):
        logging.error("ZeroSecondThinkingCog: 必要な環境変数が不足しているため、Cogをロードしません。")
        return
    await bot.add_cog(ZeroSecondThinkingCog(bot))