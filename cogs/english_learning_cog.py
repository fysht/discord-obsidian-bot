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

# --- ロガーの設定 ---
# ファイルに出力する場合は、filename='bot.log'などを追加
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 共通関数をインポート (ファイルがない場合は下のダミー関数が使われます) ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。ダミー関数を使用します。")
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
        """Obsidianのノートの特定セクションに追記するダミー関数"""
        if section_header in current_content:
            # セクションが存在すれば、そのセクションの最後に追記
            # 既に同じリンクがないかチェック
            if link_to_add in current_content:
                return current_content
            # セクションヘッダーの直後（改行後）に追記
            section_content_start = current_content.find(section_header) + len(section_header)
            return current_content[:section_content_start] + f"\n{link_to_add}" + current_content[section_content_start:]
        else:
            # セクションがなければ、ファイルの最後にセクションごと追記
            return f"{current_content.strip()}\n\n{section_header}\n{link_to_add}\n"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SUPPORTED_AUDIO_TYPES = ['audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm']
SAKUBUN_NOTE_PATH = "/Study/瞬間英作文リスト.md"  # あなたのDropbox Vault内のパス

class TTSView(discord.ui.View):
    """テキストをDiscordのTTS機能で再生するためのボタンを持つView"""
    def __init__(self, text_to_speak: str):
        super().__init__(timeout=None)
        # メンションやMarkdown記法を除去してクリーンなテキストに
        clean_text = re.sub(r'<@!?\d+>', '', text_to_speak)
        clean_text = re.sub(r'[*_`~#]', '', clean_text)
        # DiscordのTTSは2000文字の制限があるため、超える分はカット
        self.text_to_speak = clean_text.strip()[:2000]

    @discord.ui.button(label="発音する", style=discord.ButtonStyle.secondary, emoji="🔊")
    async def pronounce_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.text_to_speak:
            await interaction.response.send_message("読み上げるテキストがありません。", ephemeral=True)
            return
        # ephemeral=Trueにすることで、本人にしか聞こえないメッセージとしてTTSが再生される
        await interaction.response.send_message(self.text_to_speak, tts=True, ephemeral=True)

class EnglishLearningCog(commands.Cog):
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
            self.gemini_model_base = genai.GenerativeModel("gemini-2.5-pro")
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            self.chat_sessions = {}
            self.sakubun_questions = []
            self.is_ready = True
            logging.info("✅ EnglishLearningCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ EnglishLearningCogの初期化中にエラーが発生しました: {e}", exc_info=True)

    def _load_env_vars(self):
        """環境変数を読み込む"""
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        """必須の環境変数が設定されているか検証する"""
        required_vars = {
            "ENGLISH_LEARNING_CHANNEL_ID": self.channel_id != 0,
            "GEMINI_API_KEY": self.gemini_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
            "DROPBOX_REFRESH_TOKEN": self.dropbox_refresh_token,
            "DROPBOX_APP_KEY": self.dropbox_app_key,
            "DROPBOX_APP_SECRET": self.dropbox_app_secret
        }
        for var, is_set in required_vars.items():
            if not is_set:
                logging.error(f"環境変数 '{var}' が設定されていません。")
                return False
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        """ボットの準備ができたときにタスクを開始する"""
        if not self.is_ready:
            return
        await self._load_sakubun_questions()
        if not self.morning_sakubun_task.is_running():
            self.morning_sakubun_task.start()
        if not self.evening_sakubun_task.is_running():
            self.evening_sakubun_task.start()

    async def cog_unload(self):
        """Cogがアンロードされるときにリソースをクリーンアップする"""
        if self.is_ready:
            await self.session.close()
            self.morning_sakubun_task.cancel()
            self.evening_sakubun_task.cancel()

    async def _load_sakubun_questions(self):
        """Dropbox上のObsidianファイルから瞬間英作文の問題を読み込む"""
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
        """指定されたチャンネルで瞬間英作文セッションを実行する"""
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
            ).set_footer(text="このメッセージに返信する形で、英訳を投稿してください（音声入力も可能です）。")
            await channel.send(embed=q_embed)
            await asyncio.sleep(20)

    @app_commands.command(name="start_chat", description="AIとの英会話チャットを開始します。")
    async def start_chat(self, interaction: discord.Interaction):
        if interaction.channel.id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ利用できます。", ephemeral=True)
            return
        if interaction.user.id in self.chat_sessions:
            await interaction.response.send_message("既にチャットセッションを開始しています。", ephemeral=True)
            return
        await interaction.response.defer()
        prompt = "あなたはフレンドリーな英会話の相手です。自己紹介と、相手の調子を尋ねるような簡単な質問から会話を始めてください。"
        response = await self.gemini_model_base.generate_content_async(prompt)
        initial_question = response.text
        self.chat_sessions[interaction.user.id] = {
            "history": [{"role": "model", "parts": [initial_question]}]
        }
        await interaction.followup.send(f"**AI:** {initial_question}", view=TTSView(initial_question))

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
        review_text = await self._generate_chat_review(session["history"])
        english_for_tts = await self._extract_english_for_tts(review_text)
        review_embed = discord.Embed(
            title="💬 英会話セッションレビュー",
            description=review_text,
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        ).set_footer(text=f"{interaction.user.display_name}さんのセッション")
        view = TTSView(english_for_tts) if english_for_tts else None
        await interaction.channel.send(embed=review_embed, view=view)
        await self._save_chat_log_to_obsidian(interaction.user.display_name, session["history"], review_text)
        await interaction.followup.send("チャットセッションを終了し、レビューを生成・保存しました。")

    async def _extract_english_for_tts(self, review_text: str) -> str:
        """レビューテキストからTTSで読み上げるべき英語の例文のみを抽出する"""
        try:
            prompt = f"以下の英会話レビューから、発音練習に使える英語のフレーズや例文だけを抜き出してください。抜き出したフレーズや文は、スペースで区切って一行で出力してください。日本語の解説や見出し、記号は一切含めないでください。\n\n# 元のレビュー\n{review_text}"
            response = await self.gemini_model_base.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"TTS用の英語抽出に失敗しました: {e}")
            return ""

    async def _generate_chat_review(self, history: list) -> str:
        """会話履歴からレビューを生成する"""
        conversation_log = "\n".join([f"**{'You' if turn['role'] == 'user' else 'AI'}:** {turn['parts'][0]}" for turn in history])
        prompt = f"あなたはプロの英語教師です。以下の英会話ログを分析し、学習者が学ぶべき重要なポイントをまとめたレビューを作成してください。\n\n# 指示\n1. **会話の要約**: どのようなトピックについて話したか、1〜2文で簡潔にまとめてください。\n2. **重要フレーズ**: 会話の中から、学習者が覚えるべき便利なフレーズや単語を3〜5個選び出し、意味と使い方を例文付きで解説してください。\n3. **改善点**: 学習者の発言の中で、より自然な表現にできる箇所があれば、1〜2点指摘し、改善案を提示してください。\n4. 全体をMarkdown形式で、ポジティブなトーンで記述してください。\n\n# 会話ログ\n{conversation_log}"
        response = await self.gemini_model_base.generate_content_async(prompt)
        return response.text

    async def _save_chat_log_to_obsidian(self, user_name: str, history: list, review: str):
        """会話ログとレビューをObsidian Vaultに保存する"""
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        timestamp = now.strftime('%Y%m%d%H%M%S')
        title = f"英会話ログ {user_name} {date_str}"
        filename = f"{timestamp}-{title}.md"
        conversation_log = "\n".join([f"- **{'You' if turn['role'] == 'user' else 'AI'}:** {turn['parts'][0]}" for turn in history])
        note_content = f"# {title}\n\n- **Date:** {date_str}\n- **Participant:** {user_name}\n\n[[{date_str}]]\n\n---\n\n## 💬 Session Review\n{review}\n\n---\n\n## 📜 Full Transcript\n{conversation_log}\n"
        note_path = f"{self.dropbox_vault_path}/English Learning/Chat Logs/{filename}"
        try:
            self.dbx.files_upload(note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"Obsidianに英会話ログを保存しました: {note_path}")
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
            link_to_add = f"- [[English Learning/Chat Logs/{filename[:-3]}|{title}]]"
            section_header = "## English Learning"
            try:
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# {date_str}\n" # デイリーノートがなければ作成
                else: raise
            new_content = update_section(current_content, link_to_add, section_header)
            self.dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート ({date_str}.md) にリンクを追記しました。")
        except Exception as e:
            logging.error(f"Obsidianへのログ保存中にエラーが発生しました: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ受信時の処理"""
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return
        user_input = ""
        try:
            if message.attachments and any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
                await message.add_reaction("⏳")
                attachment = message.attachments[0]
                temp_audio_path = Path(f"./temp_english_{attachment.filename}")
                await attachment.save(temp_audio_path)
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
            logging.error(f"メッセージ処理中にエラーが発生しました: {e}", exc_info=True)
            await message.add_reaction("❌")

    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        """瞬間英作文の回答を評価し、フィードバックを返す"""
        await message.add_reaction("🤔")
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")
        prompt = f"あなたはプロの英語教師です。以下の「日本語の原文」と「学習者の英訳」を比較し、添削と解説を行ってください。\n\n# 指示\n- 学習者の英訳が文法的に正しいか、自然な表現かを評価してください。\n- より良い表現や別の言い回しがあれば、`### Model Answer` という見出しを付けて、箇条書きで2〜3個提示してください。\n- 重要な文法ポイントや単語の使い方について、簡潔で分かりやすい解説を加えてください。\n- 全体を一つのメッセージとして、Markdown形式で出力してください。\n\n# 日本語の原文\n{japanese_question}\n\n# 学習者の英訳\n{user_answer}"
        response = await self.gemini_model_base.generate_content_async(prompt)
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

    async def handle_chat_message(self, message: discord.Message, user_message: str):
        """AI壁打ちチャットの応答を生成する"""
        session = self.chat_sessions[message.author.id]
        session["history"].append({"role": "user", "parts": [user_message]})
        if len(session["history"]) > 20:
            session["history"] = session["history"][-20:]
        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        model_with_system_prompt = genai.GenerativeModel(
            "gemini-2.5-pro", system_instruction=system_instruction
        )
        chat = model_with_system_prompt.start_chat(history=session["history"])
        async with message.channel.typing():
            response = await chat.send_message_async(user_message)
            ai_response = response.text
            session["history"].append({"role": "model", "parts": [ai_response]})
            await message.reply(ai_response, view=TTSView(ai_response))

async def setup(bot: commands.Bot):
    """Cogをボットに追加する"""
    await bot.add_cog(EnglishLearningCog(bot))

# --- ボットのメイン処理 ---
async def main():
    # .envファイルから環境変数を読み込む (python-dotenvが必要)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        logging.warning("python-dotenvがインストールされていません。.envファイルは読み込まれません。")

    # ボットのインテントを設定
    intents = discord.Intents.default()
    intents.message_content = True # メッセージ内容を読み取るために必要
    intents.members = True       # メンバー情報を取得するために推奨

    # ボットを初期化
    bot = commands.Bot(command_prefix="/", intents=intents)

    @bot.event
    async def on_ready():
        logging.info(f'ログインしました: {bot.user} (ID: {bot.user.id})')
        # スラッシュコマンドを同期
        try:
            synced = await bot.tree.sync()
            logging.info(f"{len(synced)}個のスラッシュコマンドを同期しました。")
        except Exception as e:
            logging.error(f"スラッシュコマンドの同期に失敗しました: {e}")

    # Cogをロード
    await setup(bot)

    # ボットを起動
    discord_token = os.getenv("DISCORD_BOT_TOKEN")
    if not discord_token:
        logging.critical("環境変数 'DISCORD_BOT_TOKEN' が設定されていません。ボットを起動できません。")
        return
    
    await bot.start(discord_token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("ボットを手動でシャットダウンします。")