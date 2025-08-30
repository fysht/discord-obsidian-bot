import os
import re
import json
import discord
import dropbox
import logging
import google.generativeai as genai
from discord.ext import commands
from datetime import datetime, timezone, timedelta

# --- ロガーの設定 ---
# ログの出力形式を定義
log_format = '%(asctime)s - %(levelname)s - %(message)s'
# 基本的な設定を適用 (INFOレベル以上のログを出力)
logging.basicConfig(level=logging.INFO, format=log_format)
# このファイル用のロガーインスタンスを作成
logger = logging.getLogger(__name__)

# --- 定数 ---
# 日本標準時 (JST) を定義
JST = timezone(timedelta(hours=+9), 'JST')


class GenerativeAiLogCog(commands.Cog):
    """
    指定されたチャンネルのメッセージを監視し、
    生成AIの回答をObsidianに自動で保存するCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False

        self._load_environment_variables()

        if not self._are_credentials_valid():
            logger.error("❌ Generative AI Log Cogの必須環境変数が不足しています。このCogは無効化されます。")
            return

        try:
            self.dbx = self._initialize_dropbox_client()
            self.ai_model = self._initialize_ai_model()
            self.is_ready = True
            logger.info("✅ Generative AI Log Cog is loaded and ready.")
        except Exception:
            logger.error("❌ Generative AI Log Cogの初期化中にエラーが発生しました。", exc_info=True)

    def _load_environment_variables(self):
        """環境変数をインスタンス変数に読み込む。"""
        self.channel_id = os.getenv("AI_LOG_CHANNEL_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH")

    def _are_credentials_valid(self) -> bool:
        """必須の環境変数がすべて設定されているかを確認する"""
        required_vars = [
            self.channel_id, self.gemini_api_key, self.dropbox_app_key,
            self.dropbox_app_secret, self.dropbox_refresh_token, self.dropbox_vault_path
        ]
        return all(required_vars)

    def _initialize_dropbox_client(self) -> dropbox.Dropbox:
        """Dropboxクライアントを初期化する。"""
        return dropbox.Dropbox(
            app_key=self.dropbox_app_key,
            app_secret=self.dropbox_app_secret,
            oauth2_refresh_token=self.dropbox_refresh_token
        )

    def _initialize_ai_model(self) -> genai.GenerativeModel:
        """生成AIモデルを初期化する。"""
        genai.configure(api_key=self.gemini_api_key)
        return genai.GenerativeModel('gemini-2.5-pro')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (not self.is_ready or
            message.author.bot or
            str(message.channel.id) != self.channel_id or
            not message.content):
            return

        logger.info(f"📄 Processing message from {message.author.name} in #{message.channel.name}")

        try:
            full_content = message.content
            separator = "\n---\n"
            title_part = ""
            body_part = ""

            if separator in full_content:
                parts = full_content.split(separator, 1)
                title_part = parts[0].strip()
                body_part = parts[1].strip()
            else:
                body_part = full_content.strip()

            ai_response = await self._generate_title_and_summary(full_content)
            title = title_part if title_part else ai_response.get("title", "Untitled Log")
            summary = ai_response.get("summary", "No summary generated.")

            now = datetime.now(JST)
            sanitized_title = self._sanitize_filename(title)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            filename = f"{timestamp}-{sanitized_title}.md"
            
            markdown_content = self._create_markdown_content(
                title=title, summary=summary, full_answer=body_part, date=now
            )

            dropbox_path = f"{self.dropbox_vault_path}/AI Logs/{filename}"
            self._upload_to_dropbox(dropbox_path, markdown_content)
            logger.info(f"⬆️ Successfully uploaded to Dropbox: {dropbox_path}")

            await self._add_link_to_daily_note(filename, title, now)
            logger.info("🔗 Successfully added link to the daily note.")
            
            await message.add_reaction("✅")

        except Exception:
            logger.error("❌ An error occurred while processing the message.", exc_info=True)
            await message.add_reaction("❌")

    async def _generate_title_and_summary(self, content: str) -> dict:
        """AIを呼び出し、テキストからタイトルと要約をJSON形式で生成する"""
        prompt = f"""
        以下のテキストは、AIアシスタントとの会話ログです。この内容を分析し、Obsidianのノートとして保存するのに最適な「タイトル」と、内容の要点を3行程度でまとめた「要約」を生成してください。
        制約事項:
        - 出力は必ず下記のJSON形式でなければなりません。
        - JSON以外の説明文や前置きは一切含めないでください。
        出力形式:
        {{
            "title": "生成されたタイトル",
            "summary": "生成された要約"
        }}
        ---
        入力テキスト:
        {content}
        ---
        """
        response = await self.ai_model.generate_content_async(prompt)
        
        cleaned_text = re.search(r'\{.*\}', response.text, re.DOTALL)
        if not cleaned_text:
            raise ValueError("AI response does not contain a valid JSON object.")
        
        return json.loads(cleaned_text.group(0))

    def _sanitize_filename(self, filename: str) -> str:
        """ファイル名として不適切な文字をハイフンに置換し、長さを制限する"""
        sanitized = re.sub(r'[\\/*?:"<>|]', '-', filename)
        return sanitized[:100]

    def _create_markdown_content(self, title: str, summary: str, full_answer: str, date: datetime) -> str:
        """Obsidian保存用のMarkdownコンテンツを整形して生成する"""
        date_str = date.strftime('%Y-%m-%d')
        return (
            f"# {title}\n\n"
            f"- **Source:** \n"
            f"- **作成日:** {date_str}\n\n"
            f"[[{date_str}]]\n\n"
            f"---\n\n"
            f"## Summary\n{summary}\n\n"
            f"---\n\n"
            f"## Full Text\n{full_answer}\n"
        )

    def _upload_to_dropbox(self, path: str, content: str):
        """指定されたDropboxパスにコンテンツをアップロードする"""
        self.dbx.files_upload(
            content.encode('utf-8'),
            path,
            mode=dropbox.files.WriteMode('add'),
            mute=True
        )

    async def _add_link_to_daily_note(self, filename: str, title: str, date: datetime):
        """その日のデイリーノートに、作成したログへのリンクを追記する"""
        daily_note_date_str = date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date_str}.md"
        link_to_add = f"- [[AI Logs/{filename[:-3]}|{title}]]\n"
        section_header = "\n## Logs\n"

        try:
            _, res = self.dbx.files_download(daily_note_path)
            content = res.content.decode('utf-8')
            
            log_section_pattern = r'(##\s+Logs\s*\n)'
            match = re.search(log_section_pattern, content)

            if match:
                insert_pos = match.end()
                new_content = f"{content[:insert_pos]}{link_to_add}{content[insert_pos:]}"
            else:
                new_content = f"{content.strip()}\n{section_header}{link_to_add}"

        except dropbox.exceptions.ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                new_content = f"{section_header}{link_to_add}"
            else:
                raise

        self.dbx.files_upload(
            new_content.encode('utf-8'),
            daily_note_path,
            mode=dropbox.files.WriteMode('overwrite'),
            mute=True
        )

async def setup(bot: commands.Bot):
    """Cogをボットに登録するためのセットアップ関数"""
    await bot.add_cog(GenerativeAiLogCog(bot))