import os
import discord
from discord.ext import commands, tasks
from google.genai import types
import logging
import datetime
from datetime import timedelta

# config.py から共通設定を読み込み
from config import JST

class PartnerRoutineCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        # Bot本体からGeminiクライアントを受け取る
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.reminder_check_task.is_running(): self.reminder_check_task.start()
        if not self.inactivity_check_task.is_running(): self.inactivity_check_task.start()
        if not self.nightly_reflection_task.is_running(): self.nightly_reflection_task.start()

    def cog_unload(self):
        self.reminder_check_task.cancel()
        self.inactivity_check_task.cancel()
        self.nightly_reflection_task.cancel()

    @tasks.loop(minutes=1)
    async def reminder_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        # TaskServiceを使って時間になったリマインダーを取得
        due_reminders, is_changed = partner_cog.task_service.check_due_reminders()

        if due_reminders:
            channel = self.bot.get_channel(self.memo_channel_id)
            if channel:
                for rem in due_reminders:
                    user_id = rem.get('user_id')
                    mention = f"<@{user_id}>" if user_id else ""
                    await channel.send(f"{mention} 時間だよ！🔔\n「{rem['content']}」")
            
            # リマインダーを消化したのでDriveに保存
            if is_changed:
                await partner_cog.task_service.save_data()

    @tasks.loop(minutes=5)
    async def inactivity_check_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        now = datetime.datetime.now(JST)
        last_interact = partner_cog.task_service.last_interaction
        if not last_interact: return
        
        diff = now - last_interact
        
        # 6時間以上経過＆日中の場合のみ話しかける
        if diff > timedelta(hours=6) and 9 <= now.hour <= 21:
            context_data = "ユーザーは数時間何も発言していません。"
            instruction = "「お疲れ様！」「生きてる〜？」など、少し寂しそうにしつつ、相手の状況を軽く伺う短いメッセージを1つだけ送って。絶対に質問攻めにはしないこと。"
            await partner_cog.generate_and_send_routine_message(context_data, instruction)
            
            # 話しかけたので最終会話時間を更新
            partner_cog.task_service.update_last_interaction()
            await partner_cog.task_service.save_data()

    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def nightly_reflection_task(self):
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel: return
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        today_log = await partner_cog.fetch_todays_chat_log(channel)
        
        if not today_log.strip():
            prompt = """
            あなたは私を日々サポートする、20代女性の親密なパートナーです。温かみのあるタメ口で話してください。
            現在22時です。今日は私からのチャット連絡がありませんでしたが、「今日もお疲れ様！」と労いつつ、1日の振り返りを促す短くて答えやすい質問を【1つだけ】投げかけてください。
            （例：今日一番印象に残ったことは？ など。長文は禁止です）
            """
        else:
            prompt = f"""
            あなたは私を日々サポートする、20代女性の親密なパートナーです。温かみのあるタメ口で話してください。
            現在22時です。以下の「今日の会話ログ全体」を踏まえて、今日の私の活動内容に寄り添った、1日の振り返りを促す質問を【1つだけ】投げかけてください。

            条件：
            - 「今日もお疲れ様！」などの短い労いの言葉から始めること。
            - ログの中から具体的な出来事やタスク（例：〇〇の作業など）を1つ拾って触れること。
            - 最後に、今日1日の中で印象に残ったことや、明日に向けた気持ちなどを引き出す簡単な質問を1つだけすること。
            - 長文は厳禁。LINEのメッセージのように簡潔にすること。

            【今日の会話ログ】
            {today_log}
            """
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction="あなたは20代女性の親密なパートナーです。")
            )
            await channel.send(response.text.strip())
        except Exception as e:
            logging.error(f"Nightly Reflection Error: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerRoutineCog(bot))