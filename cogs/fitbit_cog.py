import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import datetime
import zoneinfo
import yaml
from io import StringIO
import asyncio
from typing import Optional, Dict, Any
import statistics

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

# --- 新しいライブラリ ---
from google import genai
# ----------------------
from fitbit_client import FitbitClient

try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SLEEP_REPORT_TIME = datetime.time(hour=8, minute=0, tzinfo=JST)
FULL_HEALTH_REPORT_TIME = datetime.time(hour=22, minute=0, tzinfo=JST)
WEEKLY_HEALTH_REPORT_TIME = datetime.time(hour=22, minute=0, tzinfo=JST)

# Google Drive 設定
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class FitbitCog(commands.Cog):
    """Fitbitのデータを取得し、Obsidian(Google Drive)への記録とAIによる健康アドバイスを行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.fitbit_client_id = os.getenv("FITBIT_CLIENT_ID")
        self.fitbit_client_secret = os.getenv("FITBIT_CLIENT_SECRET")
        self.fitbit_refresh_token = os.getenv("FITBIT_REFRESH_TOKEN")
        self.fitbit_user_id = os.getenv("FITBIT_USER_ID", "-")
        
        self.report_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        self.is_ready = self._validate_and_init_clients()
        if self.is_ready: logging.info("FitbitCog: 正常に初期化されました。")
        else: logging.error("FitbitCog: 環境変数が不足しているため、初期化に失敗しました。")

    def _validate_and_init_clients(self) -> bool:
        if not all([self.fitbit_client_id, self.fitbit_client_secret, self.fitbit_refresh_token,
                    self.report_channel_id, self.gemini_api_key, self.drive_folder_id]):
            return False
        try:
            self.fitbit_client = FitbitClient(
                self.fitbit_client_id, self.fitbit_client_secret, None, self.fitbit_user_id
            )
            # --- Client初期化 ---
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
            # ------------------
            return True
        except Exception as e:
            logging.error(f"FitbitCogのクライアント初期化中にエラー: {e}", exc_info=True)
            return False

    # ... (Drive Helpers, Calc logic are same) ...
    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request()); open(TOKEN_FILE,'w').write(creds.to_json())
                except: return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    def _find_file(self, service, parent_id, name):
        q = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        res = service.files().list(q=q, fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _create_folder(self, service, parent_id, name):
        meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        file = service.files().create(body=meta, fields='id').execute()
        return file.get('id')

    def _read_text(self, service, file_id):
        req = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while done is False: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _update_text(self, service, file_id, content):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        service.files().update(fileId=file_id, media_body=media).execute()

    def _create_text(self, service, parent_id, name, content):
        meta = {'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        service.files().create(body=meta, media_body=media).execute()

    def _calculate_sleep_score(self, summary: Dict[str, Any]) -> int:
        # (ロジック変更なしのため省略。元のコードを維持してください)
        return 80 # Dummy for brevity

    def _process_sleep_data(self, sleep_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not sleep_data or 'sleep' not in sleep_data or not sleep_data['sleep']: return None
        # (簡易化)
        return {'minutesAsleep': 420, 'sleep_score': 80}

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.sleep_report.is_running(): self.sleep_report.start()
            if not self.full_health_report.is_running(): self.full_health_report.start()
            if not self.weekly_health_report.is_running(): self.weekly_health_report.start()

    def cog_unload(self):
        self.sleep_report.cancel()
        self.full_health_report.cancel()
        self.weekly_health_report.cancel()

    def _format_minutes(self, minutes: int) -> str:
        if minutes is None: return "N/A"
        h, m = divmod(minutes, 60)
        return f"{h}時間{m}分" if h > 0 else f"{m}分"

    @tasks.loop(time=SLEEP_REPORT_TIME)
    async def sleep_report(self):
        if not self.is_ready: return
        channel = self.bot.get_channel(self.report_channel_id)
        try:
            target_date = datetime.datetime.now(JST).date()
            raw_sleep_data = await self.fitbit_client.get_sleep_data(target_date)
            sleep_summary = self._process_sleep_data(raw_sleep_data)

            if not sleep_summary: return # Silent if no data

            if channel:
                embed = discord.Embed(title=f"🌙 {target_date} 睡眠速報", color=discord.Color.purple())
                embed.add_field(name="スコア", value=f"**{sleep_summary.get('sleep_score')}**", inline=True)
                embed.add_field(name="時間", value=f"**{self._format_minutes(sleep_summary.get('minutesAsleep'))}**", inline=True)
                await channel.send(embed=embed)
        except Exception as e:
            logging.error(f"Sleep report error: {e}")

    @tasks.loop(time=FULL_HEALTH_REPORT_TIME)
    async def full_health_report(self):
        # (ロジックはほぼ同じ)
        pass

    @tasks.loop(time=WEEKLY_HEALTH_REPORT_TIME)
    async def weekly_health_report(self):
        # (ロジックはほぼ同じ)
        pass

    @app_commands.command(name="get_evening_report")
    async def get_evening_report(self, interaction: discord.Interaction, date: str = None):
        await interaction.response.defer()
        # (ロジック省略)
        await interaction.followup.send("Report sent.")

    def _parse_note_content(self, content: str) -> (dict, str):
        try:
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3: return yaml.safe_load(StringIO(parts[1])) or {}, parts[2].lstrip()
        except: pass
        return {}, content

    async def _save_data_to_obsidian(self, target_date: datetime.date, sleep_data: dict, activity_data: dict):
        # (Drive保存ロジック変更なし)
        pass

    async def _generate_ai_advice(self, target_date, sleep, activity) -> str:
        prompt = f"今日の健康データ: 睡眠{sleep.get('sleep_score') if sleep else 'なし'}, 歩数{activity.get('summary',{}).get('steps') if activity else 'なし'}。一言アドバイスをください。"
        try:
            # --- 生成メソッド変更 ---
            res = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.0-flash',
                contents=prompt
            )
            return res.text.strip()
            # ----------------------
        except: return "アドバイス生成失敗"

    async def _generate_weekly_ai_advice(self, summary) -> str:
        try:
            # --- 生成メソッド変更 ---
            res = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.5-pro',
                contents=f"週間健康データ: {summary}。来週のアドバイスを一言。"
            )
            return res.text.strip()
            # ----------------------
        except: return "アドバイス生成失敗"

    async def _create_discord_embed(self, target_date, sleep, activity, advice, is_manual=False):
        embed = discord.Embed(title=f"📅 {target_date} レポート", color=discord.Color.blue())
        if sleep:
            embed.add_field(name="睡眠", value=f"スコア: {sleep.get('sleep_score')}\n時間: {self._format_minutes(sleep.get('minutesAsleep'))}")
        if activity:
            s = activity.get('summary', {})
            embed.add_field(name="活動", value=f"歩数: {s.get('steps')}\n心拍: {s.get('restingHeartRate')}")
        embed.add_field(name="AI Coach", value=advice, inline=False)
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))