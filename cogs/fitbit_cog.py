import os
import discord
from discord.ext import commands, tasks
import logging
import datetime
import zoneinfo
import asyncio
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from fitbit_client import FitbitClient

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

class FitbitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.valid:
            drive_service = build('drive', 'v3', credentials=creds)
            self.fitbit_client = FitbitClient(
                os.getenv("FITBIT_CLIENT_ID"),
                os.getenv("FITBIT_CLIENT_SECRET"),
                drive_service,
                self.drive_folder_id,
                os.getenv("FITBIT_USER_ID", "-")
            )
            self.is_ready = True
        else:
            self.is_ready = False
            logging.error("FitbitCog: Drive APIの認証に失敗しました。")

    def _calculate_sleep_score(self, summary):
        total_asleep = summary.get('minutesAsleep', 0)
        return min(100, round((total_asleep / 480) * 100)) if total_asleep > 0 else 0

    def _process_sleep_data(self, sleep_data):
        if not sleep_data or 'sleep' not in sleep_data or not sleep_data['sleep']: return None
        total_asleep = sum(log.get('minutesAsleep', 0) for log in sleep_data['sleep'])
        total_in_bed = sum(log.get('timeInBed', 0) for log in sleep_data['sleep'])
        summary = {'minutesAsleep': total_asleep, 'timeInBed': total_in_bed}
        summary['sleep_score'] = self._calculate_sleep_score(summary)
        return summary

    def _format_minutes(self, minutes: int) -> str:
        if not minutes: return "0分"
        h, m = divmod(minutes, 60)
        return f"{h}時間{m}分" if h > 0 else f"{m}分"

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.sleep_report.is_running(): self.sleep_report.start()
            if not self.full_health_report.is_running(): self.full_health_report.start()

    def cog_unload(self):
        self.sleep_report.cancel()
        self.full_health_report.cancel()

    @tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=JST))
    async def sleep_report(self):
        if not self.is_ready: return
        target_date = datetime.datetime.now(JST).date()
        raw_sleep_data = await self.fitbit_client.get_sleep_data(target_date)
        sleep_summary = self._process_sleep_data(raw_sleep_data)
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        if not sleep_summary:
            context_data = "今日の睡眠データ：まだ同期されていません"
            instruction = "「おはようございます！睡眠データがまだ同期されていないみたいです。アプリを開いてみてくださいね」と優しく伝えてください。"
        else:
            sleep_score = sleep_summary.get('sleep_score', 0)
            sleep_time = self._format_minutes(sleep_summary.get('minutesAsleep', 0))
            context_data = f"【昨晩の睡眠データ】\nスコア: {sleep_score} / 100\n合計睡眠時間: {sleep_time}"
            instruction = "「睡眠データの速報です！」のような親しみやすい語りかけから始めてください。スコアや時間に対して労いやポジティブなコメントをし、今日も一日元気に過ごせるような一言を添えてください。"
        await partner_cog.generate_and_send_routine_message(context_data, instruction)

    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def full_health_report(self):
        if not self.is_ready: return
        target_date = datetime.datetime.now(JST).date()
        raw_sleep_data, activity_data = await asyncio.gather(
            self.fitbit_client.get_sleep_data(target_date),
            self.fitbit_client.get_activity_summary(target_date)
        )
        sleep_summary = self._process_sleep_data(raw_sleep_data)
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return
        
        sleep_text = f"スコア: {sleep_summary.get('sleep_score', 'N/A')}, 睡眠時間: {self._format_minutes(sleep_summary.get('minutesAsleep', 0))}" if sleep_summary else "データなし"
        activity_text = f"歩数: {activity_data.get('summary', {}).get('steps', 'N/A')}歩, 消費: {activity_data.get('summary', {}).get('caloriesOut', 'N/A')}kcal" if activity_data else "データなし"
        context_data = f"【本日の睡眠】\n{sleep_text}\n【本日の活動】\n{activity_text}"
        instruction = "「今日もお疲れ様でした！」から始まる夜のメッセージを作成してください。今日の健康データ（歩数や睡眠）を振り返り、良かった点を褒め、明日への優しいアドバイスを1つだけ添えてください。"
        await partner_cog.generate_and_send_routine_message(context_data, instruction)

async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))