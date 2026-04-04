import os
import logging
import datetime
import discord
from discord.ext import commands, tasks
from discord import app_commands

from config import JST
from prompts import PROMPT_VOCAB_EXTRACTION


class EnglishLearningCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.midnight_vocab_extraction.is_running():
            self.midnight_vocab_extraction.start()
        if not self.daily_english_quiz.is_running():
            self.daily_english_quiz.start()

    def cog_unload(self):
        self.midnight_vocab_extraction.cancel()
        self.daily_english_quiz.cancel()

    async def _get_log_content(self, date_obj: datetime.date) -> str:
        """指定した日付の裏ログ(YYYY-MM-DD_EN.md)を取得する"""
        service = self.drive_service.get_service()
        if not service:
            return ""

        base_folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, "EnglishLearning"
        )
        if not base_folder_id:
            return ""
        logs_folder_id = await self.drive_service.find_file(
            service, base_folder_id, "Logs"
        )
        if not logs_folder_id:
            return ""

        date_str = date_obj.strftime("%Y-%m-%d")
        file_name = f"{date_str}_EN.md"

        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)
        if f_id:
            try:
                return await self.drive_service.read_text_file(service, f_id)
            except Exception:
                return ""
        return ""

    async def _save_vocabulary(self, vocab_text: str):
        """抽出した単語帳データをVocabulary.mdに追記する"""
        service = self.drive_service.get_service()
        if not service:
            return

        base_folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, "EnglishLearning"
        )
        if not base_folder_id:
            base_folder_id = await self.drive_service.create_folder(
                service, self.drive_folder_id, "EnglishLearning"
            )

        file_name = "Vocabulary.md"
        f_id = await self.drive_service.find_file(service, base_folder_id, file_name)

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        append_text = f"\n### {now_str}\n{vocab_text}\n"

        if f_id:
            content = await self.drive_service.read_text_file(service, f_id)
            if not content.endswith("\n"):
                content += "\n"
            content += append_text
            await self.drive_service.update_text(service, f_id, content)
        else:
            header = "# 📓 My Vocabulary List\n日常の思考から抽出した自分専用の単語帳です。\n\n"
            await self.drive_service.upload_text(
                service, base_folder_id, file_name, header + append_text
            )

    @tasks.loop(time=datetime.time(hour=23, minute=50, tzinfo=JST))
    async def midnight_vocab_extraction(self):
        """毎晩23:50にその日のログから重要単語を抽出して単語帳を作成"""
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            return

        today = datetime.datetime.now(JST).date()
        log_content = await self._get_log_content(today)

        if not log_content or "## 💬 English Log" not in log_content:
            return

        prompt = f"""
あなたはプロの英語コーチです。以下の「今日のユーザーの英語思考ログ」を分析し、ユーザーが今後も日常的に使いそうな「重要な英単語やフレーズ」を3〜5個抽出してください。
出力はMarkdownの表形式のみとしてください。（挨拶や解説は不要です）

【表のフォーマット】
| 英語 (English) | 日本語 (Japanese) | ユーザーの文脈に合わせた例文 (Example) |
|---|---|---|
| (単語) | (意味) | (例文) |

【今日のログ】
{log_content}
"""
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )
            vocab_table = response.text.strip()

            await self._save_vocabulary(vocab_table)

            partner_cog = self.bot.get_cog("PartnerCog")
            if partner_cog:
                context = f"今日の単語帳を抽出しました。\n{vocab_table}"
                await partner_cog.generate_and_send_routine_message(
                    context, PROMPT_VOCAB_EXTRACTION
                )

        except Exception as e:
            logging.error(f"Vocabulary Extraction Error: {e}")

    # ==========================================
    # ★大改造: 今日のリアルな会話からクイズを作る
    # ==========================================
    @tasks.loop(
        time=[
            datetime.time(hour=7, minute=30, tzinfo=JST),
            datetime.time(hour=21, minute=0, tzinfo=JST),
        ]
    )
    async def daily_english_quiz(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            return

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        # 過去のリアルな会話を直近50件取得して、ユーザーの発言だけを抽出
        recent_user_msgs = []
        async for msg in channel.history(limit=50):
            if msg.author.id != self.bot.user.id and not msg.content.startswith("/"):
                recent_user_msgs.append(msg.content)

        if not recent_user_msgs:
            recent_text = "（最近の会話なし）"
        else:
            # AIが読み込みすぎないように最新の20件程度に絞る
            recent_text = "\n".join(recent_user_msgs[:20])

        context_data = f"【最近のユーザーの発言（クイズの素材）】\n{recent_text}"

        # PartnerCog側で空気を読んで出題させる指示
        instruction = """上記の【最近のユーザーの発言】の中で使われている言葉や、日常の出来事をピックアップして、自然な日常会話で役立つ「瞬間英作文クイズ」を1つだけ出題してください。
正解は言わずに答えさせる形式にしてください。
※定時報告感は出さず、会話の流れから急にクイズを思いついたような、ポップな感じで出題してください！"""

        await partner_cog.generate_and_send_routine_message(context_data, instruction)

    @app_commands.command(
        name="test_vocab",
        description="【テスト用】今日の英語ログから単語帳を生成します。",
    )
    async def test_vocab(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.midnight_vocab_extraction()
        await interaction.followup.send("✅ 単語帳の生成タスクをテスト実行しました！")

    @app_commands.command(
        name="test_quiz",
        description="【テスト用】過去のログから瞬間英作文クイズを出題します。",
    )
    async def test_quiz(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.daily_english_quiz()
        await interaction.followup.send("✅ クイズ出題タスクをテスト実行しました！")


async def setup(bot: commands.Bot):
    await bot.add_cog(EnglishLearningCog(bot))
