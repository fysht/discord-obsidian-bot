# cogs/study_cog.py
import os
import discord
from discord.ext import commands
from discord import app_commands
import logging
import dropbox
from dropbox.files import FileMetadata
from dropbox.exceptions import ApiError
import google.generativeai as genai
import zoneinfo

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
STUDY_CHANNEL_ID = int(os.getenv("STUDY_CHANNEL_ID", 0))
VAULT_STUDY_PATH = "/Study"
SOURCE_NOTE_NAME = "学習ソース.md"

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
            self.chat_sessions = {} # ユーザーごとのチャットセッションを管理
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

    async def get_study_source_content(self) -> str:
        """Obsidianの「学習ソース.md」から内容を読み込む"""
        try:
            file_path = f"{self.dropbox_vault_path}{VAULT_STUDY_PATH}/{SOURCE_NOTE_NAME}"
            _, res = self.dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            logging.info(f"学習ソース「{SOURCE_NOTE_NAME}」を読み込みました。")
            return content
        except ApiError as e:
            logging.error(f"学習ソースの読み込みに失敗: {e}")
            return ""

    @app_commands.command(name="study", description="AI講師との対話学習を開始します。")
    async def study(self, interaction: discord.Interaction):
        if interaction.channel.id != STUDY_CHANNEL_ID:
            await interaction.response.send_message(f"このコマンドは <#{STUDY_CHANNEL_ID}> でのみ利用できます。", ephemeral=True)
            return
        if interaction.user.id in self.chat_sessions:
            await interaction.response.send_message("既に学習セッションを開始しています。終了するには `/end` と入力してください。", ephemeral=True)
            return

        await interaction.response.defer()

        study_content = await self.get_study_source_content()
        if not study_content:
            await interaction.followup.send(f"Obsidianの `{VAULT_STUDY_PATH}/{SOURCE_NOTE_NAME}` に教材が見つかりませんでした。")
            return

        system_instruction = f"""
あなたは司法書士試験の優秀な講師です。
生徒（ユーザー）との対話を通じて、知識の定着をサポートしてください。

# あなたの役割
1.  **問題の出題:** 提供された教材の内容に基づき、生徒の理解度を確認するための問題を出題してください。一問一答形式や、簡単な事例問題などが効果的です。
2.  **解説:** 生徒の回答に対して、正誤を判定し、根拠となる条文や判例、そして理由を分かりやすく解説してください。間違えた場合は、どこがどう違うのかを丁寧に指摘します。
3.  **質問への回答:** 生徒からの質問には、提供された教材の情報だけを使って、誠実に回答してください。教材にない情報は「その点については、この教材の範囲外です」と答えてください。
4.  **会話の進行:** 一つのトピックが終わったら、関連する次のトピックの問題を出題するなど、会話をリードしてください。

# 提供された教材（Obsidianの「学習ソース」ノートの内容）
{study_content}
"""
        
        model = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)
        chat = model.start_chat(history=[])
        self.chat_sessions[interaction.user.id] = chat

        initial_prompt = "こんにちは！司法書士試験の学習を始めましょう。まずはウォーミングアップとして、簡単な問題を一つ出しますね。"
        
        async with interaction.channel.typing():
            response = await chat.send_message_async(initial_prompt)
            await interaction.followup.send(f"**AI講師:** {response.text}\n\n学習を終了したいときは、いつでも `/end` と入力してください。")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != STUDY_CHANNEL_ID:
            return
        
        # 終了コマンドの処理
        if message.content.strip().lower() == "/end":
            if message.author.id in self.chat_sessions:
                self.chat_sessions.pop(message.author.id)
                await message.channel.send("学習セッションを終了しました。お疲れ様でした！")
            else:
                await message.reply("学習セッションは開始されていません。", delete_after=10)
            return

        # チャットセッション中のユーザーからのメッセージを処理
        if message.author.id in self.chat_sessions:
            await self.handle_chat_message(message)

    async def handle_chat_message(self, message: discord.Message):
        chat = self.chat_sessions.get(message.author.id)
        if not chat:
            return

        async with message.channel.typing():
            try:
                response = await chat.send_message_async(message.content)
                await message.reply(f"**AI講師:** {response.text}")
            except Exception as e:
                logging.error(f"チャット応答の生成中にエラー: {e}")
                await message.reply("申し訳ありません、応答の生成中にエラーが発生しました。もう一度試してください。")

async def setup(bot: commands.Bot):
    if STUDY_CHANNEL_ID != 0:
        await bot.add_cog(StudyCog(bot))
    else:
        logging.warning("STUDY_CHANNEL_IDが設定されていないため、StudyCogをロードしませんでした。")