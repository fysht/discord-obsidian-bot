import os
import discord
from discord.ext import commands
from discord import app_commands
import logging
import json
import dropbox
from dropbox.files import FileMetadata, WriteMode, DownloadError
from dropbox.exceptions import ApiError
import google.generativeai as genai
import zoneinfo
from datetime import datetime
import re

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
STUDY_CHANNEL_ID = int(os.getenv("STUDY_CHANNEL_ID", 0))
VAULT_STUDY_PATH = "/Study"
SOURCE_NOTE_NAME = "学習ソース.md"
LOG_PATH = "/Study/Logs" # 学習ログの保存先

class StudyCog(commands.Cog, name="Study"):
    """AI講師との対話による学習を支援するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()
        
        if not self._validate_env_vars():
            logging.error("StudyCog: 必須の環境変数が不足。Cogを無効化します。")
            return
        try:
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            genai.configure(api_key=self.gemini_api_key)
            self.chat_sessions = {}
            self.is_ready = True
            logging.info("✅ StudyCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"StudyCogの初期化中にエラー: {e}", exc_info=True)

    def _load_env_vars(self):
        self.dropbox_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path=os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.dropbox_app_key=os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret=os.getenv("DROPBOX_APP_SECRET")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

    def _validate_env_vars(self) -> bool:
        return all([self.dropbox_refresh_token, self.dropbox_vault_path, self.gemini_api_key, STUDY_CHANNEL_ID != 0])

    def _get_session_path(self, user_id: int) -> str:
        """ユーザーごとのセッション保存パスを返す"""
        return f"{self.dropbox_vault_path}/.bot/study_session_{user_id}.json"

    async def get_study_source_content(self, topic: str = None) -> str:
        """Obsidianの「学習ソース.md」から内容を読み込む。トピックが指定されていれば絞り込む"""
        try:
            file_path = f"{self.dropbox_vault_path}{VAULT_STUDY_PATH}/{SOURCE_NOTE_NAME}"
            _, res = self.dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            logging.info(f"学習ソース「{SOURCE_NOTE_NAME}」を読み込みました。")

            if not topic:
                return content

            # トピックに関連するセクションを抽出（Markdownの見出しを利用）
            pattern = re.compile(rf"(^#+\s*{re.escape(topic)}.*?)(?=^#+\s|\Z)", re.MULTILINE | re.DOTALL)
            match = pattern.search(content)
            
            if match:
                logging.info(f"トピック「{topic}」に関連する部分を抽出しました。")
                return match.group(1)
            else:
                lines = content.splitlines()
                start_index = -1
                for i, line in enumerate(lines):
                    if topic in line:
                        start_index = i
                        break
                if start_index != -1:
                    start = max(0, start_index - 10)
                    end = min(len(lines), start_index + 11)
                    return "\n".join(lines[start:end])
                
                return ""

        except ApiError as e:
            logging.error(f"学習ソースの読み込みに失敗: {e}")
            return ""

    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        """Dropboxからユーザーの対話履歴を読み込む"""
        try:
            _, res = self.dbx.files_download(self._get_session_path(user_id))
            return json.loads(res.content)
        except ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                return None
            logging.error(f"セッションファイルの読み込みに失敗: {e}")
            return None

    async def _save_session_to_dropbox(self, user_id: int, history: list):
        """Dropboxにユーザーの対話履歴を保存する"""
        try:
            path = self._get_session_path(user_id)
            # ContentオブジェクトをJSONシリアライズ可能な形式に変換
            serializable_history = [
                {"role": turn.role, "parts": [part.text for part in turn.parts]}
                for turn in history
            ]
            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode('utf-8')
            self.dbx.files_upload(content, path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"セッションファイルの保存に失敗: {e}")

    async def _generate_study_review(self, history: list) -> str:
        """対話履歴から学習レビューを生成する"""
        # ★エラー修正箇所
        conversation_log = "\n".join([f"**{'あなた' if turn.role == 'user' else 'AI講師'}:** {turn.parts[0].text}" for turn in history if turn.role in ['user', 'model']])
        if not conversation_log:
            return "今回のセッションでは、レビューを作成するのに十分な対話がありませんでした。"

        prompt = f"""
あなたはプロの家庭教師です。以下の生徒との学習対話ログを分析し、学習内容をまとめたレビューを作成してください。
# 指示
1.  **学習トピック**: どのようなトピックについて学んだか、1〜2文で簡潔にまとめてください。
2.  **キーポイント**: 生徒が学んだ特に重要な知識や概念を3点ほど箇条書きで抜き出してください。
3.  **弱点とアドバイス**: 生徒が間違えたり、理解が不十分だった点を1〜2点指摘し、次回の学習に向けた具体的なアドバイスを提示してください。
4.  全体をMarkdown形式で、生徒を励ますようなポジティブなトーンで記述してください。
# 学習対話ログ
{conversation_log}
"""
        try:
            model = genai.GenerativeModel("gemini-2.5-pro")
            response = await model.generate_content_async(prompt)
            return response.text
        except Exception as e:
            logging.error(f"学習レビューの生成に失敗: {e}")
            return "レビューの生成中にエラーが発生しました。"

    async def _save_log_to_obsidian(self, user: discord.User, history: list, review: str):
        """対話ログとレビューをObsidianに保存する"""
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        timestamp = now.strftime('%Y%m%d%H%M%S')
        title = f"学習ログ {user.display_name} {date_str}"
        filename = f"{timestamp}-{title}.md"
        
        # ★エラー修正箇所
        conversation_log = "\n".join([f"- **{'あなた' if turn.role == 'user' else 'AI講師'}:** {turn.parts[0].text}" for turn in history if turn.role in ['user', 'model']])
        
        note_content = f"# {title}\n\n- **Date:** {date_str}\n- **User:** {user.display_name}\n\n[[{date_str}]]\n\n---\n\n## 📝 学習レビュー\n{review}\n\n---\n\n## 📜 全対話ログ\n{conversation_log}\n"
        note_path = f"{self.dropbox_vault_path}{LOG_PATH}/{filename}"
        
        try:
            self.dbx.files_upload(note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"Obsidianに学習ログを保存しました: {note_path}")
        except Exception as e:
            logging.error(f"Obsidianへのログ保存中にエラー: {e}")

    @app_commands.command(name="study", description="AI講師との対話学習を開始または再開します。")
    @app_commands.describe(topic="学習したいトピック名（例: 所有権保存の登記）")
    async def study(self, interaction: discord.Interaction, topic: str):
        if interaction.channel.id != STUDY_CHANNEL_ID:
            await interaction.response.send_message(f"このコマンドは <#{STUDY_CHANNEL_ID}> でのみ利用できます。", ephemeral=True)
            return
        if interaction.user.id in self.chat_sessions:
            await interaction.response.send_message("既に学習セッションを開始しています。終了するには `/end` と入力してください。", ephemeral=True)
            return

        await interaction.response.defer()

        study_content = await self.get_study_source_content(topic=topic)
        if not study_content:
            await interaction.followup.send(f"Obsidianの教材からトピック「{topic}」に関する情報を見つけられませんでした。見出しや内容を確認してください。")
            return

        system_instruction = f"""
あなたは司法書士試験の優秀な講師です。生徒（ユーザー）との対話を通じて、知識の定着をサポートしてください。
# あなたの役割
1.  **問題の出題:** 提供された教材の内容に基づき、生徒の理解度を確認するための問題を出題してください。
2.  **解説:** 生徒の回答に対して、正誤を判定し、根拠となる条文や理由を分かりやすく解説してください。
3.  **質問への回答:** 生徒からの質問には、提供された教材の情報だけを使って、誠実に回答してください。
# 提供された教材（'{topic}'に関する抜粋）
{study_content}
"""
        
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
                prompt = "さて、前回の続きから始めましょうか。準備はいいですか？何か質問があればどうぞ。"
                response_text = prompt
            else:
                initial_prompt = f"こんにちは！「{topic}」について学習を始めましょう。まずはウォーミングアップとして、このトピックに関する簡単な問題を一つ出しますね。"
                response = await chat.send_message_async(initial_prompt)
                response_text = response.text

            await interaction.followup.send(f"**AI講師:** {response_text}\n\n学習を終了したいときは、いつでも `/end` と入力してください。")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != STUDY_CHANNEL_ID:
            return
        
        if message.content.strip().lower() == "/end":
            session = self.chat_sessions.pop(message.author.id, None)
            if session:
                await message.channel.send("学習セッションを終了します。今回の学習内容のまとめを作成しています...")
                async with message.channel.typing():
                    review = await self._generate_study_review(session.history)
                    review_embed = discord.Embed(
                        title="📝 今回の学習レビュー",
                        description=review,
                        color=discord.Color.gold(),
                        timestamp=datetime.now(JST)
                    ).set_footer(text=f"{message.author.display_name}さんのセッション")
                    await message.channel.send(embed=review_embed)
                    
                    await self._save_session_to_dropbox(message.author.id, session.history)
                    await self._save_log_to_obsidian(message.author, session.history, review)
            else:
                await message.reply("学習セッションは開始されていません。", delete_after=10)
            return

        if message.author.id in self.chat_sessions:
            await self.handle_chat_message(message)

    async def handle_chat_message(self, message: discord.Message):
        chat = self.chat_sessions.get(message.author.id)
        if not chat: return

        async with message.channel.typing():
            try:
                response = await chat.send_message_async(message.content)
                await message.reply(f"**AI講師:** {response.text}")
            except Exception as e:
                logging.error(f"チャット応答の生成中にエラー: {e}")
                await message.reply("申し訳ありません、応答の生成中にエラーが発生しました。")

async def setup(bot: commands.Bot):
    if STUDY_CHANNEL_ID != 0:
        await bot.add_cog(StudyCog(bot))
    else:
        logging.warning("STUDY_CHANNEL_IDが設定されていないため、StudyCogをロードしませんでした。")