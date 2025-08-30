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
SECTION_ORDER = [
    "## WebClips",
    "## YouTube Summaries",
    "## AI Logs",
    "## Zero-Second Thinking",
    "## Memo"
]


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
        except Exception as e:
            logger.error(f"❌ Generative AI Log Cogの初期化中にエラーが発生しました: {e}", exc_info=True)

    def _load_environment_variables(self):
        """環境変数をインスタンス変数に読み込む"""
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
        return genai.GenerativeModel('gemini-2.5-pro')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """特定のチャンネルへのメッセージ投稿を監視するイベントリスナー"""
        if (not self.is_ready or
            message.author.bot or
            str(message.channel.id) != self.channel_id):
            return

        full_content = ""
        source_type = ""

        if message.content:
            full_content = message.content
            source_type = "Text"
        elif message.attachments:
            for attachment in message.attachments:
                if attachment.filename.endswith('.txt'):
                    try:
                        source_type = f"File: {attachment.filename}"
                        content_bytes = await attachment.read()
                        full_content = content_bytes.decode('utf-8')
                        break
                    except Exception:
                        logger.error(f"添付ファイルの読み込みに失敗しました: {attachment.filename}", exc_info=True)
                        await message.add_reaction("⚠️")
                        return

        if not full_content:
            return

        logger.info(f"📄 Processing message from {message.author.name} (Source: {source_type})")

        try:
            separator = "\n---\n"
            question_part = ""
            answer_part = ""

            if separator in full_content:
                parts = full_content.split(separator, 1)
                question_part = parts[0].strip()
                answer_part = parts[1].strip()
            else:
                question_part = "（質問なし）"
                answer_part = full_content.strip()

            # タイトルと要約、記事を並行して生成
            title_summary_task = self._generate_title_and_summary(full_content)
            article_task = self._generate_article(full_content)
            
            ai_response, article_text = await asyncio.gather(title_summary_task, article_task)
            
            title = ai_response.get("title", "Untitled Log")
            summary = ai_response.get("summary", "No summary generated.")

            now = datetime.now(JST)
            sanitized_title = self._sanitize_filename(title)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            filename = f"{timestamp}-{sanitized_title}.md"
            
            markdown_content = self._create_markdown_content(
                title=title, summary=summary, question=question_part, 
                answer=answer_part, article=article_text, date=now
            )

            dropbox_path = f"{self.dropbox_vault_path}/AI Logs/{filename}"
            self._upload_to_dropbox(dropbox_path, markdown_content)
            logger.info(f"⬆️ Successfully uploaded to Dropbox: {dropbox_path}")

            await self._add_link_to_daily_note(filename, title, now)
            logger.info("🔗 Successfully added link to the daily note.")
            
            await message.add_reaction("✅")

        except Exception as e:
            logger.error(f"❌ An error occurred while processing the message: {e}", exc_info=True)
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
            logger.warning("AI response did not contain valid JSON. Falling back to title generation only.")
            prompt_title_only = f"以下のテキストに最適なタイトルを、タイトル本文のみで生成してください:\n\n{content}"
            response_title_only = await self.ai_model.generate_content_async(prompt_title_only)
            fallback_title = response_title_only.text.strip()
            return {"title": fallback_title, "summary": "（要約の自動生成に失敗しました）"}

        return json.loads(cleaned_text.group(0))

    async def _generate_article(self, content: str) -> str:
        """会話ログからnote投稿用の記事を生成する"""
        prompt = f"""
        以下のAIアシスタントとの会話ログを元に、一人称視点のブログ記事（noteなどを想定）を作成してください。
        - 読者が興味を持ち、理解しやすいように、会話の専門的な内容をかみ砕いて説明してください。
        - 最終的な結論や得られた知見が明確に伝わるように構成してください。
        - 前置きやAIとしての返答は含めず、記事の本文のみを生成してください。
        ---
        会話ログ:
        {content}
        ---
        """
        try:
            response = await self.ai_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logger.error(f"記事の生成に失敗しました: {e}")
            return "（記事の自動生成に失敗しました）"


    def _sanitize_filename(self, filename: str) -> str:
        """ファイル名として不適切な文字をハイフンに置換し、長さを制限する"""
        sanitized = re.sub(r'[\\/*?:"<>|]', '-', filename)
        return sanitized[:100]

    def _create_markdown_content(self, title: str, summary: str, question: str, answer: str, article: str, date: datetime) -> str:
        """Obsidian保存用のMarkdownコンテンツを整形して生成する"""
        date_str = date.strftime('%Y-%m-%d')
        return (
            f"# {title}\n\n"
            f"- **Source:** Discord AI Log\n"
            f"- **作成日:** {date_str}\n\n"
            f"[[{date_str}]]\n\n"
            f"---\n\n"
            f"## Summary\n{summary}\n\n"
            f"---\n\n"
            f"## Question\n{question}\n\n"
            f"---\n\n"
            f"## Answer\n{answer}\n\n"
            f"---\n\n"
            f"## Article\n{article}\n"
        )

    def _upload_to_dropbox(self, path: str, content: str):
        """指定されたDropboxパスにコンテンツをアップロードする"""
        self.dbx.files_upload(
            content.encode('utf-8'),
            path,
            mode=dropbox.files.WriteMode('add'),
            mute=True
        )

    def _update_daily_note_with_ordered_section(self, current_content: str, link_to_add: str, section_header: str) -> str:
        """定義された順序に基づいてデイリーノートのコンテンツを更新する"""
        lines = current_content.split('\n')
        
        # セクションが既に存在するか確認
        try:
            header_index = lines.index(section_header)
            insert_index = header_index + 1
            while insert_index < len(lines) and (lines[insert_index].strip().startswith('- ') or not lines[insert_index].strip()):
                insert_index += 1
            lines.insert(insert_index, link_to_add)
            return "\n".join(lines)
        except ValueError:
            # セクションが存在しない場合、正しい位置に新規作成
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

    async def _add_link_to_daily_note(self, filename: str, title: str, date: datetime):
        """その日のデイリーノートに、作成したログへのリンクを追記する"""
        daily_note_date_str = date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date_str}.md"
        link_to_add = f"- [[AI Logs/{filename[:-3]}|{title}]]"
        section_header = "## AI Logs"

        try:
            _, res = self.dbx.files_download(daily_note_path)
            content = res.content.decode('utf-8')
        except dropbox.exceptions.ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                content = ""
            else:
                raise

        new_content = self._update_daily_note_with_ordered_section(content, link_to_add, section_header)

        self.dbx.files_upload(
            new_content.encode('utf-8'),
            daily_note_path,
            mode=dropbox.files.WriteMode('overwrite'),
            mute=True
        )

async def setup(bot: commands.Bot):
    """Cogをボットに登録するためのセットアップ関数"""
    await bot.add_cog(GenerativeAiLogCog(bot))