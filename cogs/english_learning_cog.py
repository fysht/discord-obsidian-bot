import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import random
import asyncio
from datetime import time, datetime
import zoneinfo
import aiohttp
import openai
import google.generativeai as genai
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re

# --- 共通関数をインポート ---
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SUPPORTED_AUDIO_TYPES = ['audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm']
SAKUBUN_NOTE_PATH = "/Study/瞬間英作文リスト.md"

class EnglishLearningCog(commands.Cog):
    """瞬間英作文とAI壁打ちチャットによる英語学習を支援するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()

        if not self._validate_env_vars():
            logging.error("EnglishLearningCog: 必須の環境変数が不足。Cogを無効化します。")
            return

        try:
            # APIクライアントの初期化
            self.session = aiohttp.ClientSession()
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            
            self.chat_sessions = {}  # {user_id: {"channel_id": int, "history": list}}
            self.sakubun_questions = []
            self.is_ready = True
            logging.info("✅ EnglishLearningCogが正常に初期化されました。")

        except Exception as e:
            logging.error(f"❌ EnglishLearningCogの初期化中にエラー: {e}", exc_info=True)

    def _load_env_vars(self):
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        return all([self.channel_id, self.gemini_api_key, self.openai_api_key, self.dropbox_refresh_token])

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            await self._load_sakubun_questions()
            self.morning_sakubun_task.start()
            self.evening_sakubun_task.start()

    async def cog_unload(self):
        if self.is_ready:
            await self.session.close()
            self.morning_sakubun_task.cancel()
            self.evening_sakubun_task.cancel()
            
    async def _load_sakubun_questions(self):
        """Obsidianから瞬間英作文の問題を読み込む"""
        try:
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"
            _, res = self.dbx.files_download(path)
            content = res.content.decode('utf-8')
            questions = re.findall(r'^- (.+)', content, re.MULTILINE)
            if questions:
                self.sakubun_questions = [q.strip() for q in questions]
                logging.info(f"Obsidianから{len(self.sakubun_questions)}問の瞬間英作文の問題を読み込みました。")
            else:
                logging.warning("Obsidianの瞬間英作文リストに問題が見つかりませんでした。")
        except Exception as e:
            logging.error(f"Obsidianからの瞬間英作文リストの読み込みに失敗: {e}")

    # --- 瞬間英作文 ---
    async def _run_sakubun_session(self, channel, num_questions: int, session_name: str):
        if not self.sakubun_questions:
            await channel.send("⚠️ 瞬間英作文の問題リストが空のため、出題できません。")
            return

        questions = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions)))
        
        embed = discord.Embed(
            title=f"✍️ 今日の{session_name}・瞬間英作文",
            description=f"これから{len(questions)}問、日本語の文を英語に翻訳するトレーニングを始めます。",
            color=discord.Color.purple()
        )
        await channel.send(embed=embed)
        await asyncio.sleep(5)

        for i, q_text in enumerate(questions):
            q_embed = discord.Embed(
                title=f"第 {i+1} 問",
                description=f"**{q_text}**",
                color=discord.Color.blue()
            )
            q_embed.set_footer(text="このメッセージに返信する形で、英訳を投稿してください（音声入力も可能です）。")
            await channel.send(embed=q_embed)
            await asyncio.sleep(20)
            
    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel:
            await self._run_sakubun_session(channel, 1, "朝")

    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel:
            await self._run_sakubun_session(channel, 2, "夜")

    # --- AI壁打ちチャット ---
    @app_commands.command(name="start_chat", description="AIとの英会話チャットを開始します。")
    async def start_chat(self, interaction: discord.Interaction):
        if interaction.channel.id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ利用できます。", ephemeral=True)
            return

        if interaction.user.id in self.chat_sessions:
            await interaction.response.send_message("既にチャットセッションを開始しています。", ephemeral=True)
            return
            
        await interaction.response.defer()

        # AIに最初の質問を生成させる
        prompt = "あなたはフレンドリーな英会話の相手です。自己紹介と、相手の調子を尋ねるような簡単な質問から会話を始めてください。"
        response = await self.gemini_model.generate_content_async(prompt)
        initial_question = response.text

        self.chat_sessions[interaction.user.id] = {
            "channel_id": interaction.channel_id,
            "history": [{"role": "model", "parts": [initial_question]}]
        }
        
        await interaction.followup.send(f"**AI:** {initial_question}")

    @app_commands.command(name="end_chat", description="AIとの英会話チャットを終了し、レビューを生成します。")
    async def end_chat(self, interaction: discord.Interaction):
        session = self.chat_sessions.pop(interaction.user.id, None)
        if not session:
            await interaction.response.send_message("チャットセッションを開始していません。", ephemeral=True)
            return

        await interaction.response.defer()

        if len(session["history"]) <= 1:
            await interaction.followup.send("会話の履歴が短すぎるため、レビューを生成できませんでした。")
            return

        # 会話ログからレビューを生成
        review_text = await self._generate_chat_review(session["history"])

        # Discordに投稿
        review_embed = discord.Embed(
            title="💬 英会話セッションレビュー",
            description=review_text,
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        )
        review_embed.set_footer(text=f"{interaction.user.display_name}さんのセッション")
        await interaction.channel.send(embed=review_embed)

        # Obsidianに保存
        await self._save_chat_log_to_obsidian(interaction.user.display_name, session["history"], review_text)

        await interaction.followup.send("チャットセッションを終了し、レビューを生成・保存しました。")

    async def _generate_chat_review(self, history: list) -> str:
        """会話履歴から重要フレーズなどを抽出・解説する"""
        conversation_log = "\n".join([f"**{'You' if turn['role'] == 'user' else 'AI'}:** {turn['parts'][0]}" for turn in history])
        
        prompt = f"""
        あなたはプロの英語教師です。以下の英会話ログを分析し、学習者が学ぶべき重要なポイントをまとめたレビューを作成してください。

        # 指示
        1.  **会話の要約**: まず、どのようなトピックについて話したか、1〜2文で簡潔にまとめてください。
        2.  **重要フレーズ**: 会話の中から、学習者が覚えるべき便利なフレーズや単語を3〜5個選び出し、意味と使い方を例文付きで解説してください。
        3.  **改善点**: 学習者の発言の中で、より自然な表現にできる箇所があれば、1〜2点指摘し、改善案を提示してください。
        4.  全体をMarkdown形式で、ポジティブなトーンで記述してください。

        # 会話ログ
        {conversation_log}
        """
        response = await self.gemini_model.generate_content_async(prompt)
        return response.text

    async def _save_chat_log_to_obsidian(self, user_name: str, history: list, review: str):
        """会話ログとレビューをObsidianに保存する"""
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        timestamp = now.strftime('%Y%m%d%H%M%S')
        
        title = f"英会話ログ {user_name} {date_str}"
        filename = f"{timestamp}-{title}.md"
        
        conversation_log = "\n".join([f"- **{'You' if turn['role'] == 'user' else 'AI'}:** {turn['parts'][0]}" for turn in history])

        # ノート本体の作成
        note_content = (
            f"# {title}\n\n"
            f"- **Date:** {date_str}\n"
            f"- **Participant:** {user_name}\n\n"
            f"[[{date_str}]]\n\n"
            f"---\n\n"
            f"## 💬 Session Review\n{review}\n\n"
            f"---\n\n"
            f"## 📜 Full Transcript\n{conversation_log}\n"
        )

        # Dropboxにアップロード
        note_path = f"{self.dropbox_vault_path}/English Learning/Chat Logs/{filename}"
        self.dbx.files_upload(note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
        logging.info(f"Obsidianに英会話ログを保存しました: {note_path}")

        # デイリーノートにリンクを追加
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        link_to_add = f"- [[English Learning/Chat Logs/{filename[:-3]}|{title}]]"
        section_header = "## English Learning"
        
        try:
            _, res = self.dbx.files_download(daily_note_path)
            current_content = res.content.decode('utf-8')
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                current_content = ""
            else: raise
        
        new_content = update_section(current_content, link_to_add, section_header)
        self.dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))


    # --- メッセージ処理 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return

        user_input = ""
        attachment = None
        if message.attachments and any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
            attachment = message.attachments[0]
        
        try:
            if attachment:
                await message.add_reaction("⏳")
                temp_audio_path = Path(f"./temp_english_{attachment.filename}")
                async with self.session.get(attachment.url) as resp:
                    if resp.status == 200:
                        with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                
                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                user_input = transcription.text
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("✅")
                if os.path.exists(temp_audio_path): os.remove(temp_audio_path)
            elif message.content:
                user_input = message.content.strip()

            if not user_input or user_input.startswith('/'):
                return
            
            if message.reference and message.reference.message_id:
                original_msg = await message.channel.fetch_message(message.reference.message_id)
                if original_msg.author.id == self.bot.user.id and original_msg.embeds and "第" in original_msg.embeds[0].title:
                    await self.handle_sakubun_answer(message, user_input, original_msg)
                    return

            if message.author.id in self.chat_sessions:
                await self.handle_chat_message(message, user_input)

        except Exception as e:
            logging.error(f"英語学習機能のメッセージ処理中にエラー: {e}", exc_info=True)
            await message.add_reaction("❌")

    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        """瞬間英作文の回答を評価し、フィードバックを返す"""
        await message.add_reaction("🤔")
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")

        prompt = f"""
        あなたはプロの英語教師です。以下の「日本語の原文」と「学習者の英訳」を比較し、添削と解説を行ってください。

        # 指示
        - まず、学習者の英訳が文法的に正しいか、自然な表現かを評価してください。
        - 次に、より良い表現や別の言い回しがあれば、模範解答として2〜3個提示してください。
        - 最後に、重要な文法ポイントや単語の使い方について、簡潔で分かりやすい解説を加えてください。
        - 全体を一つのメッセージとして、Markdown形式で出力してください。

        # 日本語の原文
        {japanese_question}

        # 学習者の英訳
        {user_answer}
        """

        response = await self.gemini_model.generate_content_async(prompt)
        feedback_embed = discord.Embed(
            title=f"添削結果: 「{japanese_question}」",
            description=response.text,
            color=discord.Color.green()
        )
        await message.reply(embed=feedback_embed)
        await message.remove_reaction("🤔", self.bot.user)

    async def handle_chat_message(self, message: discord.Message, user_message: str):
        """AI壁打ちチャットの応答を生成する"""
        session = self.chat_sessions[message.author.id]
        
        session["history"].append({"role": "user", "parts": [user_message]})
        if len(session["history"]) > 20: # 履歴を少し長めに保持
            session["history"] = session["history"][-20:]

        chat = self.gemini_model.start_chat(history=session["history"])
        
        prompt = f"""
        あなたはフレンドリーな英会話の相手です。以下のルールに従って、自然な会話を続けてください。
        - ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。
        - もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。
        - あなた自身の返答は、常に自然な英語で行ってください。
        """

        async with message.channel.typing():
            response = await chat.send_message_async(prompt)
            ai_response = response.text
            
            session["history"].append({"role": "model", "parts": [ai_response]})
            await message.reply(ai_response)

async def setup(bot: commands.Bot):
    await bot.add_cog(EnglishLearningCog(bot))