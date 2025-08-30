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
log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
logger = logging.getLogger(__name__)

# --- 定数 ---
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
        """Dropboxクライアントを初期化する"""
        return dropbox.Dropbox(
            app_key=self.dropbox_app_key,
            app_secret=self.dropbox_app_secret,
            oauth2_refresh_token=self.dropbox_refresh_token
        )

    def _initialize_ai_model(self) -> genai.GenerativeModel:
        """生成AIモデルを初期化する"""
        genai.configure(api_key=self.gemini_api_key)
        # モデル名はご自身の環境に合わせて調整してください
        return genai.GenerativeModel('gemini-2.5-pro')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """特定のチャンネルへのメッセージ投稿を監視するイベントリスナー"""
        # Bot自身からのメッセージや、対象チャンネル以外からの投稿は無視
        if (not self.is_ready or
            message.author.bot or
            str(message.channel.id) != self.channel_id):
            return

        full_content = ""
        source_type = ""

        # 1. メッセージ本文からテキストを取得
        if message.content:
            full_content = message.content
            source_type = "Text"
        # 2. 本文が空なら、添付ファイルをチェック
        elif message.attachments:
            for attachment in message.attachments:
                # テキストファイル（.txt）のみを対象とする
                if attachment.filename.endswith('.txt'):
                    try:
                        source_type = f"File: {attachment.filename}"
                        # 添付ファイルの内容をバイトデータとして読み込み
                        content_bytes = await attachment.read()
                        # UTF-8で文字列にデコード
                        full_content = content_bytes.decode('utf-8')
                        # 最初の有効なテキストファイルが見つかった時点でループを抜ける
                        break
                    except Exception:
                        logger.error(f"添付ファイルの読み込みに失敗しました: {attachment.filename}", exc_info=True)
                        await message.add_reaction("⚠️") # ファイル読み込み失敗のリアクション
                        return

        # 最終的に処理すべきテキストがなければ終了
        if not full_content:
            return

        logger.info(f"📄 Processing message from {message.author.name} (Source: {source_type})")

        try:
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
        
        # Obsidian形式のリンクを作成（末尾の改行は不要）
        link_to_add = f"- [[AI Logs/{filename[:-3]}|{title}]]"
        
        # 見出しのテキスト
        section_header = "## Logs"
        
        # 挿入するセクション全体のテキスト
        new_section_with_link = f"\n{section_header}\n{link_to_add}"

        try:
            # 既存のデイリーノートをダウンロード
            _, res = self.dbx.files_download(daily_note_path)
            content = res.content.decode('utf-8')
            
            # "## Logs" 見出しを検索するための正規表現パターン（大文字/小文字を区別せず、行頭を基準とする）
            log_section_pattern = re.compile(r'(^##\s+Logs\s*$)', re.MULTILINE | re.IGNORECASE)
            
            # パターンに一致する見出しがあれば、その直後にリンクを挿入
            match = log_section_pattern.search(content)
            if match:
                # 置換後のテキストを作成: (見出し) + (改行) + (新しいリンク)
                replacement = f"{match.group(1)}\n{link_to_add}"
                # re.subを使い、最初に見つかった見出し部分だけを置換する
                new_content = log_section_pattern.sub(replacement, content, count=1)
            else:
                # 見出しが見つからない場合は、ファイルの末尾に新しいセクションを追加
                new_content = content.strip() + new_section_with_link + "\n"

        except dropbox.exceptions.ApiError as e:
            # デイリーノートが存在しない場合
            if e.error.is_path() and e.error.get_path().is_not_found():
                # 新しくセクションとリンクを作成
                new_content = section_header + f"\n{link_to_add}\n"
            else:
                # その他のDropbox APIエラーの場合は例外を再送出
                raise

        # 更新されたコンテンツをDropboxにアップロード（ファイルを上書き）
        self.dbx.files_upload(
            new_content.encode('utf-8'),
            daily_note_path,
            mode=dropbox.files.WriteMode('overwrite'),
            mute=True
        )

async def setup(bot: commands.Bot):
    """Cogをボットに登録するためのセットアップ関数"""
    await bot.add_cog(GenerativeAiLogCog(bot))