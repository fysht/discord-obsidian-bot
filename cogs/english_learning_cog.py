# cogs/english_learning_cog.py
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
import google.generativeai as genai
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re

# --- 共通関数をインポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。")
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
        # ダミー関数
        return current_content + f"\n\n{section_header}\n{link_to_add}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SAKUBUN_NOTE_PATH = "/Study/瞬間英作文リスト.md"
LOG_PATH = "/English Learning/Chat Logs" # ログ保存先

# --- UIコンポーネント ---
class TTSView(discord.ui.View):
    def __init__(self, text_to_speak: str):
        super().__init__(timeout=None)
        clean_text = re.sub(r'<@!?\d+>', '', text_to_speak)
        clean_text = re.sub(r'[*_`~#]', '', clean_text)
        self.text_to_speak = clean_text.strip()[:2000]

    @discord.ui.button(label="発音する", style=discord.ButtonStyle.secondary, emoji="🔊")
    async def pronounce_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.text_to_speak:
            await interaction.response.send_message("読み上げるテキストがありません。", ephemeral=True)
            return
        await interaction.response.send_message(self.text_to_speak, tts=True, ephemeral=True)

class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    """瞬間英作文とAI壁打ちチャットによる英語学習を支援するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()

        if not self._validate_env_vars():
            logging.error("EnglishLearningCog: 必須の環境変数が不足しています。Cogを無効化します。")
            return

        try:
            self.session = aiohttp.ClientSession()
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            self.chat_sessions = {}
            self.sakubun_questions = []
            self.is_ready = True
            logging.info("✅ EnglishLearningCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ EnglishLearningCogの初期化中にエラーが発生しました: {e}", exc_info=True)

    def _load_env_vars(self):
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        return all([self.channel_id != 0, self.gemini_api_key, self.dropbox_refresh_token])

    def _get_session_path(self, user_id: int) -> str:
        return f"{self.dropbox_vault_path}/.bot/english_session_{user_id}.json"

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return
        await self._load_sakubun_questions()
        if not self.morning_sakubun_task.is_running(): self.morning_sakubun_task.start()
        if not self.evening_sakubun_task.is_running(): self.evening_sakubun_task.start()

    async def cog_unload(self):
        if self.is_ready:
            await self.session.close()
            self.morning_sakubun_task.cancel()
            self.evening_sakubun_task.cancel()
            
    # (既存の _load_sakubun_questions, morning_sakubun_task, evening_sakubun_task, _run_sakubun_session は変更なし)
    async def _load_sakubun_questions(self):
        if not self.is_ready: return
        try:
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"
            _, res = self.dbx.files_download(path)
            content = res.content.decode('utf-8')
            questions = re.findall(r'^- (.+)', content, re.MULTILINE)
            if questions:
                self.sakubun_questions = [q.strip() for q in questions]
                logging.info(f"Obsidianから{len(self.sakubun_questions)}問の瞬間英作文の問題を読み込みました。")
            else:
                logging.warning(f"Obsidianのファイル ({SAKUBUN_NOTE_PATH}) に問題が見つかりませんでした。")
        except ApiError as e:
            logging.error(f"Dropbox APIエラー: {e}")
        except Exception as e:
            logging.error(f"Obsidianからの問題読み込み中に予期せぬエラーが発生しました: {e}")

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

    async def _run_sakubun_session(self, channel: discord.TextChannel, num_questions: int, session_name: str):
        if not self.sakubun_questions:
            await channel.send("⚠️ 瞬間英作文の問題リストが空のため、出題できません。")
            return
        questions = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions)))
        embed = discord.Embed(
            title=f"✍️ 今日の{session_name}・瞬間英作文",
            description=f"これから{len(questions)}問、日本語の文を英語に翻訳するトレーニングを始めます。",
            color=discord.Color.purple()
        ).set_footer(text="20秒後に問題が出題されます。")
        await channel.send(embed=embed)
        await asyncio.sleep(20)
        for i, q_text in enumerate(questions):
            q_embed = discord.Embed(
                title=f"第 {i+1} 問", description=f"**{q_text}**", color=discord.Color.blue()
            ).set_footer(text="このメッセージに返信する形で、英訳を投稿してください。")
            await channel.send(embed=q_embed)
            await asyncio.sleep(20)

    # ★コマンド名を /english に変更
    @app_commands.command(name="english", description="AIとの英会話チャットを開始または再開します。")
    async def english(self, interaction: discord.Interaction):
        if interaction.channel.id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ利用できます。", ephemeral=True)
            return
        if interaction.user.id in self.chat_sessions:
            await interaction.response.send_message("既にチャットセッションを開始しています。終了するには `/end` と入力してください。", ephemeral=True)
            return
        
        await interaction.response.defer()

        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        model = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)

        history_json = await self._load_session_from_dropbox(interaction.user.id)
        history = [
            {'role': item['role'], 'parts': item['parts']}
            for item in history_json
        ] if history_json else []

        chat = model.start_chat(history=history)
        self.chat_sessions[interaction.user.id] = chat
        
        async with interaction.channel.typing():
            if history:
                prompt = "Hi there! Let's continue our conversation. How are you doing?"
                response_text = prompt
            else:
                initial_prompt = "Hi! I'm your AI English conversation partner. Let's have a chat! How's your day going so far?"
                response = await chat.send_message_async(initial_prompt)
                response_text = response.text

            await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text))

    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        try:
            _, res = self.dbx.files_download(self._get_session_path(user_id))
            return json.loads(res.content)
        except ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                return None
            logging.error(f"英語セッションファイルの読み込みに失敗: {e}")
            return None

    async def _save_session_to_dropbox(self, user_id: int, history: list):
        try:
            path = self._get_session_path(user_id)
            serializable_history = [
                {"role": turn.role, "parts": [part.text for part in turn.parts]}
                for turn in history
            ]
            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode('utf-8')
            self.dbx.files_upload(content, path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"英語セッションファイルの保存に失敗: {e}")
            
    # (既存の _extract_english_for_tts, _generate_chat_review, _save_chat_log_to_obsidian, handle_sakubun_answer は流用・エラー修正)
    async def _extract_english_for_tts(self, review_text: str) -> str:
        try:
            prompt = f"以下の英会話レビューから、発音練習に使える英語のフレーズや例文だけを抜き出してください。抜き出したフレーズや文は、スペースで区切って一行で出力してください。日本語の解説や見出し、記号は一切含めないでください。\n\n# 元のレビュー\n{review_text}"
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"TTS用の英語抽出に失敗しました: {e}")
            return ""

    async def _generate_chat_review(self, history: list) -> str:
        conversation_log = "\n".join([f"**{'You' if turn.role == 'user' else 'AI'}:** {turn.parts[0].text}" for turn in history if turn.role in ['user', 'model']])
        if not conversation_log:
            return "今回のセッションでは、レビューを作成するのに十分な対話がありませんでした。"
            
        prompt = f"あなたはプロの英語教師です。以下の英会話ログを分析し、学習者が学ぶべき重要なポイントをまとめたレビューを作成してください。\n\n# 指示\n1. **会話の要約**: どのようなトピックについて話したか、1〜2文で簡潔にまとめてください。\n2. **重要フレーズ**: 会話の中から、学習者が覚えるべき便利なフレーズや単語を3〜5個選び出し、意味と使い方を例文付きで解説してください。\n3. **改善点**: 学習者の発言の中で、より自然な表現にできる箇所があれば、1〜2点指摘し、改善案を提示してください。\n4. 全体をMarkdown形式で、ポジティブなトーンで記述してください。\n\n# 会話ログ\n{conversation_log}"
        response = await self.gemini_model.generate_content_async(prompt)
        return response.text

    async def _save_chat_log_to_obsidian(self, user: discord.User, history: list, review: str):
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        timestamp = now.strftime('%Y%m%d%H%M%S')
        title = f"英会話ログ {user.display_name} {date_str}"
        filename = f"{timestamp}-{title}.md"
        
        conversation_log = "\n".join([f"- **{'You' if turn.role == 'user' else 'AI'}:** {turn.parts[0].text}" for turn in history if turn.role in ['user', 'model']])
        
        note_content = f"# {title}\n\n- **Date:** {date_str}\n- **Participant:** {user.display_name}\n\n[[{date_str}]]\n\n---\n\n## 💬 Session Review\n{review}\n\n---\n\n## 📜 Full Transcript\n{conversation_log}\n"
        note_path = f"{self.dropbox_vault_path}{LOG_PATH}/{filename}"
        try:
            self.dbx.files_upload(note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"Obsidianに英会話ログを保存しました: {note_path}")
            # (デイリーノートへのリンク追記は省略)
        except Exception as e:
            logging.error(f"Obsidianへのログ保存中にエラーが発生しました: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return
            
        # ★コマンド統一: /end テキストで終了
        if message.content.strip().lower() == "/end":
            session = self.chat_sessions.pop(message.author.id, None)
            if session:
                await message.channel.send("Ending the chat session. Creating your review...")
                async with message.channel.typing():
                    review_text = await self._generate_chat_review(session.history)
                    english_for_tts = await self._extract_english_for_tts(review_text)
                    
                    review_embed = discord.Embed(
                        title="💬 English Conversation Review",
                        description=review_text,
                        color=discord.Color.gold(),
                        timestamp=datetime.now(JST)
                    ).set_footer(text=f"{message.author.display_name}'s session")
                    
                    view = TTSView(english_for_tts) if english_for_tts else None
                    await message.channel.send(embed=review_embed, view=view)
                    
                    await self._save_session_to_dropbox(message.author.id, session.history)
                    await self._save_chat_log_to_obsidian(message.author, session.history, review_text)
            else:
                await message.reply("No active chat session found.", delete_after=10)
            return

        # 瞬間英作文の回答処理
        if message.reference and message.reference.message_id:
            original_msg = await message.channel.fetch_message(message.reference.message_id)
            if original_msg.author.id == self.bot.user.id and original_msg.embeds and "第" in original_msg.embeds[0].title:
                await self.handle_sakubun_answer(message, message.content.strip(), original_msg)
                return
        
        # 英会話チャットのメッセージ処理
        if message.author.id in self.chat_sessions:
            await self.handle_chat_message(message)

    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        await message.add_reaction("🤔")
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")
        prompt = f"あなたはプロの英語教師です。以下の「日本語の原文」と「学習者の英訳」を比較し、添削と解説を行ってください。\n\n# 指示\n- 学習者の英訳が文法的に正しいか、自然な表現かを評価してください。\n- より良い表現や別の言い回しがあれば、`### Model Answer` という見出しを付けて、箇条書きで2〜3個提示してください。\n- 重要な文法ポイントや単語の使い方について、簡潔で分かりやすい解説を加えてください。\n- 全体を一つのメッセージとして、Markdown形式で出力してください。\n\n# 日本語の原文\n{japanese_question}\n\n# 学習者の英訳\n{user_answer}"
        response = await self.gemini_model.generate_content_async(prompt)
        feedback_text = response.text
        feedback_embed = discord.Embed(
            title=f"添削結果: 「{japanese_question}」", description=feedback_text, color=discord.Color.green()
        )
        view = None
        model_answers_match = re.search(r"### Model Answer(?:s)?\n(.+?)(?:\n###|$)", feedback_text, re.DOTALL | re.IGNORECASE)
        if model_answers_match:
            answers_text = model_answers_match.group(1).strip()
            text_to_speak = re.sub(r'^\s*[-*]\s*|\d+\.\s*', '', answers_text, flags=re.MULTILINE).replace('\n', ' ')
            if text_to_speak:
                view = TTSView(text_to_speak)
        await message.reply(embed=feedback_embed, view=view)
        await message.remove_reaction("🤔", self.bot.user)

    async def handle_chat_message(self, message: discord.Message):
        session = self.chat_sessions.get(message.author.id)
        if not session: return

        async with message.channel.typing():
            try:
                response = await session.send_message_async(message.content)
                await message.reply(response.text, view=TTSView(response.text))
            except Exception as e:
                logging.error(f"英会話チャットの応答生成中にエラー: {e}")
                await message.reply("Sorry, an error occurred while generating a response. Please try again.")

async def setup(bot: commands.Bot):
    if int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) != 0:
        await bot.add_cog(EnglishLearningCog(bot))
    else:
        logging.warning("ENGLISH_LEARNING_CHANNEL_IDが設定されていないため、EnglishLearningCogをロードしませんでした。")