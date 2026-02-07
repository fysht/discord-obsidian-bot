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

import google.generativeai as genai
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
            # FitbitClientの初期化 (Dropbox依存を削除した前提のダミーまたはNoneを渡す必要があるが、
            # FitbitClientの実装次第。ここではDropbox引数にNoneを渡してみる)
            self.fitbit_client = FitbitClient(
                self.fitbit_client_id, self.fitbit_client_secret, None, self.fitbit_user_id
            )
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            return True
        except Exception as e:
            logging.error(f"FitbitCogのクライアント初期化中にエラー: {e}", exc_info=True)
            return False

    # --- Google Drive Helpers ---
    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token: token.write(creds.to_json())
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

    # --- Logic ---
    def _calculate_sleep_score(self, summary: Dict[str, Any]) -> int:
        total_asleep_min = summary['minutesAsleep']
        total_in_bed_min = summary['timeInBed']
        deep_min = summary['levels']['summary'].get('deep', 0)
        rem_min = summary['levels']['summary'].get('rem', 0)
        wake_min = summary['levels']['summary'].get('wake', 0)

        duration_score = min(50, (total_asleep_min / 480) * 50)
        deep_percentage = (deep_min / total_asleep_min) * 100 if total_asleep_min > 0 else 0
        rem_percentage = (rem_min / total_asleep_min) * 100 if total_asleep_min > 0 else 0
        
        deep_score = 0
        if deep_percentage >= 20: deep_score = 12.5
        elif deep_percentage >= 15: deep_score = 10
        elif deep_percentage >= 10: deep_score = 7.5
        else: deep_score = 5

        rem_score = 0
        if rem_percentage >= 25: rem_score = 12.5
        elif rem_percentage >= 20: rem_score = 10
        elif rem_percentage >= 15: rem_score = 7.5
        else: rem_score = 5
        
        quality_score = deep_score + rem_score
        restlessness_percentage = (wake_min / total_in_bed_min) * 100 if total_in_bed_min > 0 else 100
        
        restoration_score = 0
        if restlessness_percentage <= 5: restoration_score = 25
        elif restlessness_percentage <= 10: restoration_score = 22
        elif restlessness_percentage <= 15: restoration_score = 18
        elif restlessness_percentage <= 20: restoration_score = 14
        else: restoration_score = 10

        total_score = round(duration_score + quality_score + restoration_score)
        return min(100, total_score)

    def _process_sleep_data(self, sleep_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not sleep_data or 'sleep' not in sleep_data or not sleep_data['sleep']:
            return None

        total_minutes_asleep = 0
        total_time_in_bed = 0
        stage_summary = {'deep': 0, 'light': 0, 'rem': 0, 'wake': 0}

        for log in sleep_data['sleep']:
            total_minutes_asleep += log.get('minutesAsleep', 0)
            total_time_in_bed += log.get('timeInBed', 0)
            if 'levels' in log and 'summary' in log['levels']:
                for stage, data in log['levels']['summary'].items():
                    if stage in stage_summary:
                        stage_summary[stage] += data.get('minutes', 0)

        summary = {
            'minutesAsleep': total_minutes_asleep,
            'timeInBed': total_time_in_bed,
            'levels': {'summary': stage_summary}
        }
        summary['sleep_score'] = self._calculate_sleep_score(summary)
        return summary

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

            if not sleep_summary:
                if channel: await channel.send(f" FitbitCog: {target_date} の睡眠データなし")
                return

            if channel:
                embed = discord.Embed(title=f"🌙 {target_date} 睡眠速報", color=discord.Color.purple())
                embed.add_field(name="スコア", value=f"**{sleep_summary.get('sleep_score')}**", inline=True)
                embed.add_field(name="時間", value=f"**{self._format_minutes(sleep_summary.get('minutesAsleep'))}**", inline=True)
                await channel.send(embed=embed)
        except Exception as e:
            logging.error(f"Sleep report error: {e}")

    @tasks.loop(time=FULL_HEALTH_REPORT_TIME)
    async def full_health_report(self):
        if not self.is_ready: return
        channel = self.bot.get_channel(self.report_channel_id)
        try:
            target_date = datetime.datetime.now(JST).date()
            raw_sleep, activity = await asyncio.gather(
                self.fitbit_client.get_sleep_data(target_date),
                self.fitbit_client.get_activity_summary(target_date)
            )
            sleep_summary = self._process_sleep_data(raw_sleep)
            if not sleep_summary and not activity: return
            
            advice = await self._generate_ai_advice(target_date, sleep_summary, activity)
            await self._save_data_to_obsidian(target_date, sleep_summary, activity)
            
            if channel:
                embed = await self._create_discord_embed(target_date, sleep_summary, activity, advice)
                await channel.send(embed=embed)
        except Exception as e:
            logging.error(f"Full report error: {e}")

    @tasks.loop(time=WEEKLY_HEALTH_REPORT_TIME)
    async def weekly_health_report(self):
        if not self.is_ready or datetime.datetime.now(JST).weekday() != 6: return
        channel = self.bot.get_channel(self.report_channel_id)
        today = datetime.datetime.now(JST).date()
        
        w_sleep, w_activity = [], []
        for i in range(7):
            d = today - datetime.timedelta(days=i)
            s, a = await asyncio.gather(self.fitbit_client.get_sleep_data(d), self.fitbit_client.get_activity_summary(d))
            if s: w_sleep.append(self._process_sleep_data(s))
            if a: w_activity.append(a)
        
        scores = [s['sleep_score'] for s in w_sleep if s and 'sleep_score' in s]
        avg_score = statistics.mean(scores) if scores else 0
        
        summary = f"週間平均睡眠スコア: {avg_score:.1f}点"
        advice = await self._generate_weekly_ai_advice(summary)

        if channel:
            embed = discord.Embed(title="📅 週間レポート", description=f"**AI Coach**\n{advice}", color=discord.Color.green())
            embed.add_field(name="サマリー", value=summary)
            await channel.send(embed=embed)

    @app_commands.command(name="get_evening_report")
    async def get_evening_report(self, interaction: discord.Interaction, date: str = None):
        await interaction.response.defer()
        if not self.is_ready: return
        try: target_date = datetime.datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.datetime.now(JST).date()
        except: return
        
        raw_sleep, activity = await asyncio.gather(
            self.fitbit_client.get_sleep_data(target_date), self.fitbit_client.get_activity_summary(target_date)
        )
        sleep_summary = self._process_sleep_data(raw_sleep)
        advice = await self._generate_ai_advice(target_date, sleep_summary, activity)
        await self._save_data_to_obsidian(target_date, sleep_summary, activity)
        
        embed = await self._create_discord_embed(target_date, sleep_summary, activity, advice, True)
        await interaction.followup.send(embed=embed)

    def _parse_note_content(self, content: str) -> (dict, str):
        try:
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3: return yaml.safe_load(StringIO(parts[1])) or {}, parts[2].lstrip()
        except: pass
        return {}, content

    async def _save_data_to_obsidian(self, target_date: datetime.date, sleep_data: dict, activity_data: dict):
        date_str = target_date.strftime('%Y-%m-%d')
        file_name = f"{date_str}.md"
        
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        # DailyNotesフォルダ検索
        daily_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        if not daily_folder_id:
            daily_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")

        # ファイル検索・読み込み
        file_id = await loop.run_in_executor(None, self._find_file, service, daily_folder_id, file_name)
        current_content = ""
        if file_id:
            current_content = await loop.run_in_executor(None, self._read_text, service, file_id)

        frontmatter, body = self._parse_note_content(current_content)
        
        # Frontmatter更新
        if sleep_data:
            frontmatter.update({
                'sleep_score': sleep_data.get('sleep_score'),
                'total_sleep_minutes': sleep_data.get('minutesAsleep'),
            })
        if activity_data:
            summary = activity_data.get('summary', {})
            frontmatter.update({
                'steps': summary.get('steps'),
                'calories_out': summary.get('caloriesOut'),
                'resting_heart_rate': summary.get('restingHeartRate'),
            })

        # Body更新
        metrics = []
        if sleep_data:
            metrics.append(f"#### Sleep\n- Score: {sleep_data.get('sleep_score')}\n- Time: {self._format_minutes(sleep_data.get('minutesAsleep'))}")
        if activity_data:
            s = activity_data.get('summary', {})
            metrics.append(f"#### Activity\n- Steps: {s.get('steps')}\n- RHR: {s.get('restingHeartRate')}")
            
        new_body = update_section(body, "\n\n".join(metrics), "## Health Metrics")
        new_content = f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n{new_body}"

        if file_id:
            await loop.run_in_executor(None, self._update_text, service, file_id, new_content)
        else:
            await loop.run_in_executor(None, self._create_text, service, daily_folder_id, file_name, new_content)

    async def _generate_ai_advice(self, target_date, sleep, activity) -> str:
        prompt = f"今日の健康データ: 睡眠{sleep.get('sleep_score') if sleep else 'なし'}, 歩数{activity.get('summary',{}).get('steps') if activity else 'なし'}。一言アドバイスをください。"
        try:
            res = await self.gemini_model.generate_content_async(prompt)
            return res.text.strip()
        except: return "アドバイス生成失敗"

    async def _generate_weekly_ai_advice(self, summary) -> str:
        try:
            res = await self.gemini_model.generate_content_async(f"週間健康データ: {summary}。来週のアドバイスを一言。")
            return res.text.strip()
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