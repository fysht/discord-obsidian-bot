import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import datetime
import zoneinfo
import asyncio
import yaml
import io
from io import StringIO

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from fitbit_client import FitbitClient

try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

class FitbitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = None
        
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.valid:
            self.drive_service = build('drive', 'v3', credentials=creds)
            self.fitbit_client = FitbitClient(
                os.getenv("FITBIT_CLIENT_ID"),
                os.getenv("FITBIT_CLIENT_SECRET"),
                self.drive_service,
                self.drive_folder_id,
                os.getenv("FITBIT_USER_ID", "-")
            )
            self.is_ready = True
        else:
            self.is_ready = False
            logging.error("FitbitCog: Drive APIの認証に失敗しました。")

    # --- 提案1: 睡眠スコア計算の精緻化 ---
    def _calculate_sleep_score(self, summary: dict) -> int:
        """Fitbitアプリのスコアを模倣した総合睡眠スコアを計算する"""
        total_asleep_min = summary.get('minutesAsleep', 0)
        total_in_bed_min = summary.get('timeInBed', 0)
        deep_min = summary.get('levels', {}).get('summary', {}).get('deep', 0)
        rem_min = summary.get('levels', {}).get('summary', {}).get('rem', 0)
        wake_min = summary.get('levels', {}).get('summary', {}).get('wake', 0)

        if total_asleep_min == 0: return 0

        # 1. 睡眠時間 (最大50点)
        duration_score = min(50, (total_asleep_min / 480) * 50)

        # 2. 睡眠の質 (最大25点)
        deep_percentage = (deep_min / total_asleep_min) * 100
        rem_percentage = (rem_min / total_asleep_min) * 100
        deep_score = 12.5 if deep_percentage >= 20 else 10 if deep_percentage >= 15 else 7.5 if deep_percentage >= 10 else 5
        rem_score = 12.5 if rem_percentage >= 25 else 10 if rem_percentage >= 20 else 7.5 if rem_percentage >= 15 else 5
        quality_score = deep_score + rem_score

        # 3. 回復度 (最大25点)
        restlessness_percentage = (wake_min / total_in_bed_min) * 100 if total_in_bed_min > 0 else 100
        restoration_score = 25 if restlessness_percentage <= 5 else 22 if restlessness_percentage <= 10 else 18 if restlessness_percentage <= 15 else 14 if restlessness_percentage <= 20 else 10

        return min(100, round(duration_score + quality_score + restoration_score))

    def _process_sleep_data(self, sleep_data: dict) -> dict:
        """複数の睡眠ログを統合し、サマリーデータと新しいスコアを作成する"""
        if not sleep_data or 'sleep' not in sleep_data or not sleep_data['sleep']: return None
        
        total_minutes_asleep = sum(log.get('minutesAsleep', 0) for log in sleep_data['sleep'])
        total_time_in_bed = sum(log.get('timeInBed', 0) for log in sleep_data['sleep'])
        
        stage_summary = {'deep': 0, 'light': 0, 'rem': 0, 'wake': 0}
        for log in sleep_data['sleep']:
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

    def _format_minutes(self, minutes: int) -> str:
        if not minutes: return "0分"
        h, m = divmod(minutes, 60)
        return f"{h}時間{m}分" if h > 0 else f"{m}分"

    # --- Obsidian関連ユーティリティ ---
    def _parse_note_content(self, content: str):
        try:
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return yaml.safe_load(StringIO(parts[1])) or {}, parts[2].lstrip()
        except yaml.YAMLError: pass
        return {}, content

    # --- 提案3: Obsidian (Google Drive) への自動保存機能 ---
    async def _save_data_to_obsidian(self, target_date: datetime.date, sleep_data: dict, activity_data: dict):
        if not self.drive_service: return
        loop = asyncio.get_running_loop()
        
        # DailyNotesフォルダを探す
        query = f"'{self.drive_folder_id}' in parents and name = 'DailyNotes' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        try:
            res = await loop.run_in_executor(None, lambda: self.drive_service.files().list(q=query, fields="files(id)").execute())
            folders = res.get('files', [])
            if not folders:
                meta = {'name': 'DailyNotes', 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}
                folder = await loop.run_in_executor(None, lambda: self.drive_service.files().create(body=meta, fields='id').execute())
                dn_folder_id = folder.get('id')
            else:
                dn_folder_id = folders[0]['id']

            file_name = f"{target_date.strftime('%Y-%m-%d')}.md"
            query = f"'{dn_folder_id}' in parents and name = '{file_name}' and trashed = false"
            res = await loop.run_in_executor(None, lambda: self.drive_service.files().list(q=query, fields="files(id)").execute())
            files = res.get('files', [])
            
            current_content = ""
            file_id = None
            if files:
                file_id = files[0]['id']
                try:
                    request = self.drive_service.files().get_media(fileId=file_id)
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: _, done = downloader.next_chunk()
                    current_content = fh.getvalue().decode('utf-8')
                except Exception as e:
                    logging.error(f"FitbitCog: ノート読み込みエラー {e}")
                    
            frontmatter, body = self._parse_note_content(current_content)
            
            # フロントマターの更新
            if sleep_data:
                levels = sleep_data.get('levels', {}).get('summary', {})
                frontmatter.update({
                    'sleep_score': sleep_data.get('sleep_score'),
                    'total_sleep_minutes': sleep_data.get('minutesAsleep'),
                    'time_in_bed_minutes': sleep_data.get('timeInBed'),
                    'deep_sleep_minutes': levels.get('deep'),
                    'rem_sleep_minutes': levels.get('rem'),
                    'light_sleep_minutes': levels.get('light')
                })
            if activity_data:
                summary = activity_data.get('summary', {})
                frontmatter.update({
                    'steps': summary.get('steps'),
                    'distance_km': next((d['distance'] for d in summary.get('distances', []) if d['activity'] == 'total'), None),
                    'calories_out': summary.get('caloriesOut'),
                    'resting_heart_rate': summary.get('restingHeartRate'),
                    'active_minutes_fairly': summary.get('fairlyActiveMinutes'),
                    'active_minutes_very': summary.get('veryActiveMinutes'),
                })

            metrics_sections = []
            if sleep_data:
                levels = sleep_data.get('levels', {}).get('summary', {})
                sleep_text = (
                    f"#### Sleep\n"
                    f"- **Score:** {sleep_data.get('sleep_score', 'N/A')} / 100\n"
                    f"- **Total Sleep:** {self._format_minutes(sleep_data.get('minutesAsleep'))}\n"
                    f"- **Time in Bed:** {self._format_minutes(sleep_data.get('timeInBed'))}\n"
                    f"- **Stages:** Deep {self._format_minutes(levels.get('deep'))}, "
                    f"REM {self._format_minutes(levels.get('rem'))}, "
                    f"Light {self._format_minutes(levels.get('light'))}"
                )
                metrics_sections.append(sleep_text)
            
            if activity_data:
                summary = activity_data.get('summary', {})
                activity_text = (
                    f"#### Activity\n"
                    f"- **Steps:** {summary.get('steps', 'N/A')} steps\n"
                    f"- **Distance:** {next((d['distance'] for d in summary.get('distances', []) if d['activity'] == 'total'), 'N/A')} km\n"
                    f"- **Calories Out:** {summary.get('caloriesOut', 'N/A')} kcal\n"
                    f"- **Active Minutes:** {self._format_minutes(summary.get('fairlyActiveMinutes', 0) + summary.get('veryActiveMinutes', 0))}"
                )
                metrics_sections.append(activity_text)

                hr_zones = summary.get('heartRateZones', {})
                heart_rate_text = (
                    f"#### Heart Rate\n"
                    f"- **Resting Heart Rate:** {summary.get('restingHeartRate', 'N/A')} bpm\n"
                    f"- **Fat Burn:** {self._format_minutes(hr_zones.get('Fat Burn', {}).get('minutes'))}\n"
                    f"- **Cardio:** {self._format_minutes(hr_zones.get('Cardio', {}).get('minutes'))}\n"
                    f"- **Peak:** {self._format_minutes(hr_zones.get('Peak', {}).get('minutes'))}"
                )
                metrics_sections.append(heart_rate_text)

            new_body = update_section(body, "\n\n".join(metrics_sections), "## Health Metrics")
            new_daily_content = f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n{new_body}"
            
            media = MediaIoBaseUpload(io.BytesIO(new_daily_content.encode('utf-8')), mimetype='text/markdown')
            if file_id:
                await loop.run_in_executor(None, lambda: self.drive_service.files().update(fileId=file_id, media_body=media).execute())
            else:
                await loop.run_in_executor(None, lambda: self.drive_service.files().create(body={'name': file_name, 'parents': [dn_folder_id]}, media_body=media).execute())
            logging.info(f"FitbitCog: {file_name} を更新しました。")
        except Exception as e:
            logging.error(f"FitbitCog: Obsidian保存中にエラー: {e}")

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
        
        # --- 提案4: PartnerCogやChannelのエラーハンドリング強化 ---
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: 
            logging.error("FitbitCog: PartnerCogが見つかりません。")
            return

        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        today_log = "（会話ログなし）"
        if channel:
            today_log = await partner_cog.fetch_todays_chat_log(channel)
        else:
            logging.warning("FitbitCog: MEMO_CHANNEL_IDが正しく設定されていないか、チャンネルが見つかりません。")

        if not sleep_summary:
            context_data = f"今日の睡眠データ：まだ同期されていません\n【最近の会話ログ】\n{today_log}"
            instruction = "「おはようございます！睡眠データがまだ同期されていないみたいです。アプリを開いてみてくださいね」と優しく伝えてください。その際、最近の会話の流れ（ログ）も少し意識して声をかけてください。"
        else:
            sleep_score = sleep_summary.get('sleep_score', 0)
            sleep_time = self._format_minutes(sleep_summary.get('minutesAsleep', 0))
            context_data = f"【昨晩の睡眠データ】\nスコア: {sleep_score} / 100\n合計睡眠時間: {sleep_time}\n【最近の会話ログ】\n{today_log}"
            instruction = "「睡眠データの速報です！」のような親しみやすい語りかけから始めてください。最近の会話の流れ（ログ）を意識しつつ、スコアや時間に対して労いやポジティブなコメントをし、今日も一日元気に過ごせるような一言を添えてください。"
        
        await partner_cog.generate_and_send_routine_message(context_data, instruction)

    @tasks.loop(time=datetime.time(hour=22, minute=15, tzinfo=JST))
    async def full_health_report(self):
        if not self.is_ready: return
        target_date = datetime.datetime.now(JST).date()
        raw_sleep_data, activity_data = await asyncio.gather(
            self.fitbit_client.get_sleep_data(target_date),
            self.fitbit_client.get_activity_summary(target_date)
        )
        sleep_summary = self._process_sleep_data(raw_sleep_data)
        
        # Obsidianへの保存を実行 (追加)
        await self._save_data_to_obsidian(target_date, sleep_summary, activity_data)
        
        # --- 提案4: PartnerCogやChannelのエラーハンドリング強化 ---
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: 
            logging.error("FitbitCog: PartnerCogが見つかりません。")
            return
        
        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        today_log = "（会話ログなし）"
        if channel:
            today_log = await partner_cog.fetch_todays_chat_log(channel)
        else:
            logging.warning("FitbitCog: MEMO_CHANNEL_IDが正しく設定されていないか、チャンネルが見つかりません。")
        
        sleep_text = f"スコア: {sleep_summary.get('sleep_score', 'N/A')}, 睡眠時間: {self._format_minutes(sleep_summary.get('minutesAsleep', 0))}" if sleep_summary else "データなし"
        activity_text = f"歩数: {activity_data.get('summary', {}).get('steps', 'N/A')}歩, 消費: {activity_data.get('summary', {}).get('caloriesOut', 'N/A')}kcal" if activity_data else "データなし"
        
        context_data = f"【本日の睡眠】\n{sleep_text}\n【本日の活動】\n{activity_text}\n【今日の会話ログ】\n{today_log}"
        instruction = "「今日もお疲れ様でした！」から始まる夜のメッセージを作成してください。今日の会話の流れ（ログ）を意識しつつ、今日の健康データ（歩数や睡眠）を振り返り、良かった点を褒め、明日への優しいアドバイスを1つだけ添えてください。"
        
        await partner_cog.generate_and_send_routine_message(context_data, instruction)

    # --- 提案2: 手動実行用スラッシュコマンド ---
    @app_commands.command(name="fitbit_morning", description="今日の睡眠レポートを手動で取得し、パートナーに報告させます。")
    async def get_morning_report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.sleep_report()
        await interaction.followup.send("☀️ 睡眠データの取得をリクエストしました！")

    @app_commands.command(name="fitbit_evening", description="今日の総合ヘルスレポートを手動で取得し、パートナーに報告させます。")
    async def get_evening_report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.full_health_report()
        await interaction.followup.send("🌙 総合ヘルスレポートの取得と保存をリクエストしました！")


async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))