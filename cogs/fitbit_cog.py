import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import datetime
import asyncio
import yaml

from fitbit_client import FitbitClient

# --- リファクタリング: 定数とユーティリティのクリーンなインポート ---
from config import JST
from utils.obsidian_utils import update_section

class FitbitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        # --- リファクタリング: Bot本体のサービスを使い回す ---
        self.drive_service = bot.drive_service
        
        if self.drive_service:
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
            logging.error("FitbitCog: Driveサービスが初期化されていません。")

    def _calculate_sleep_score(self, summary: dict) -> int:
        total_asleep_min = summary.get('minutesAsleep', 0)
        total_in_bed_min = summary.get('timeInBed', 0)
        deep_min = summary.get('levels', {}).get('summary', {}).get('deep', 0)
        rem_min = summary.get('levels', {}).get('summary', {}).get('rem', 0)
        wake_min = summary.get('levels', {}).get('summary', {}).get('wake', 0)

        if total_asleep_min == 0: return 0

        duration_score = min(50, (total_asleep_min / 480) * 50)

        deep_percentage = (deep_min / total_asleep_min) * 100
        rem_percentage = (rem_min / total_asleep_min) * 100
        deep_score = 12.5 if deep_percentage >= 20 else 10 if deep_percentage >= 15 else 7.5 if deep_percentage >= 10 else 5
        rem_score = 12.5 if rem_percentage >= 25 else 10 if rem_percentage >= 20 else 7.5 if rem_percentage >= 15 else 5
        quality_score = deep_score + rem_score

        restlessness_percentage = (wake_min / total_in_bed_min) * 100 if total_in_bed_min > 0 else 100
        restoration_score = 25 if restlessness_percentage <= 5 else 22 if restlessness_percentage <= 10 else 18 if restlessness_percentage <= 15 else 14 if restlessness_percentage <= 20 else 10

        return min(100, round(duration_score + quality_score + restoration_score))

    def _process_sleep_data(self, sleep_data: dict) -> dict:
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

    def _parse_note_content(self, content: str):
        try:
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return yaml.safe_load(parts[1]) or {}, parts[2].lstrip()
        except yaml.YAMLError: pass
        return {}, content

    async def _save_data_to_obsidian(self, target_date: datetime.date, sleep_data: dict, activity_data: dict):
        if not self.drive_service: return
        service = self.drive_service.get_service()
        if not service: return

        try:
            # 統合された DriveService のヘルパーメソッドを利用してシンプルに！
            dn_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
            if not dn_folder_id:
                dn_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")

            file_name = f"{target_date.strftime('%Y-%m-%d')}.md"
            file_id = await self.drive_service.find_file(service, dn_folder_id, file_name)
            
            current_content = ""
            if file_id:
                current_content = await self.drive_service.read_text_file(service, file_id)
                    
            frontmatter, body = self._parse_note_content(current_content)
            
            # --- フロントマターの更新 ---
            if sleep_data:
                levels = sleep_data.get('levels', {}).get('summary', {})
                frontmatter.update({
                    'sleep_score': sleep_data.get('sleep_score'),
                    'total_sleep_minutes': sleep_data.get('minutesAsleep'),
                    'time_in_bed_minutes': sleep_data.get('timeInBed'),
                    'deep_sleep_minutes': levels.get('deep'),
                    'rem_sleep_minutes': levels.get('rem'),
                    'light_sleep_minutes': levels.get('light'),
                    'wake_sleep_minutes': levels.get('wake')
                })
            
            if activity_data:
                summary = activity_data.get('summary', {})
                raw_hr_zones = summary.get('heartRateZones', [])
                hr_zones = {z['name']: z for z in raw_hr_zones} if isinstance(raw_hr_zones, list) else raw_hr_zones
                
                frontmatter.update({
                    'steps': summary.get('steps'),
                    'distance_km': next((d['distance'] for d in summary.get('distances', []) if d['activity'] == 'total'), None),
                    'calories_out': summary.get('caloriesOut'),
                    'resting_heart_rate': summary.get('restingHeartRate'),
                    'active_minutes_very': summary.get('veryActiveMinutes'),
                    'active_minutes_fairly': summary.get('fairlyActiveMinutes'),
                    'active_minutes_lightly': summary.get('lightlyActiveMinutes'),
                    'sedentary_minutes': summary.get('sedentaryMinutes'),
                    'hr_zone_fat_burn_minutes': hr_zones.get('Fat Burn', {}).get('minutes'),
                    'hr_zone_cardio_minutes': hr_zones.get('Cardio', {}).get('minutes'),
                    'hr_zone_peak_minutes': hr_zones.get('Peak', {}).get('minutes')
                })

            frontmatter = {k: v for k, v in frontmatter.items() if v is not None}

            # --- 本文(Body)への追記 ---
            metrics_sections = []
            if sleep_data:
                levels = sleep_data.get('levels', {}).get('summary', {})
                sleep_text = (
                    f"#### 🌙 Sleep\n"
                    f"- **Score:** {sleep_data.get('sleep_score', 'N/A')} / 100\n"
                    f"- **Total Sleep:** {self._format_minutes(sleep_data.get('minutesAsleep'))}\n"
                    f"- **Time in Bed:** {self._format_minutes(sleep_data.get('timeInBed'))}\n"
                    f"- **Stages:** Deep {self._format_minutes(levels.get('deep'))}, "
                    f"REM {self._format_minutes(levels.get('rem'))}, "
                    f"Light {self._format_minutes(levels.get('light'))}, "
                    f"Wake {self._format_minutes(levels.get('wake'))}"
                )
                metrics_sections.append(sleep_text)
            
            if activity_data:
                summary = activity_data.get('summary', {})
                activity_text = (
                    f"#### 🏃‍♂️ Activity\n"
                    f"- **Steps:** {summary.get('steps', 'N/A')} steps\n"
                    f"- **Distance:** {next((d['distance'] for d in summary.get('distances', []) if d['activity'] == 'total'), 'N/A')} km\n"
                    f"- **Calories Out:** {summary.get('caloriesOut', 'N/A')} kcal\n"
                    f"- **Active Minutes:** {self._format_minutes(summary.get('fairlyActiveMinutes', 0) + summary.get('veryActiveMinutes', 0))}"
                )
                metrics_sections.append(activity_text)

                raw_hr_zones = summary.get('heartRateZones', [])
                hr_zones = {z['name']: z for z in raw_hr_zones} if isinstance(raw_hr_zones, list) else raw_hr_zones
                heart_rate_text = (
                    f"#### ❤️ Heart Rate\n"
                    f"- **Resting Heart Rate:** {summary.get('restingHeartRate', 'N/A')} bpm\n"
                    f"- **Fat Burn:** {self._format_minutes(hr_zones.get('Fat Burn', {}).get('minutes'))}\n"
                    f"- **Cardio:** {self._format_minutes(hr_zones.get('Cardio', {}).get('minutes'))}\n"
                    f"- **Peak:** {self._format_minutes(hr_zones.get('Peak', {}).get('minutes'))}"
                )
                metrics_sections.append(heart_rate_text)

            new_body = update_section(body.strip(), "\n\n".join(metrics_sections), "## 📊 Health Metrics")
            new_daily_content = f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n{new_body}"
            
            if file_id:
                await self.drive_service.update_text(service, file_id, new_daily_content)
            else:
                await self.drive_service.upload_text(service, dn_folder_id, file_name, new_daily_content)
                
            logging.info(f"FitbitCog: {file_name} を更新しました（フロントマターと本文の両方に保存）。")
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
        
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        today_log = "（会話ログなし）"
        if channel:
            today_log = await partner_cog.fetch_todays_chat_log(channel)

        if not sleep_summary:
            context_data = f"今日の睡眠データ：まだ同期されていません\n【最近の会話ログ】\n{today_log}"
            instruction = "親密な20代女性のパートナーとして、LINEのような温かみのあるタメ口で話して。朝の挨拶は6時に済ませているので不要です。「そういえば、睡眠データがまだ同期されてないみたいだから、時間があるときにアプリを開いてみてね」と短く優しく伝えてください。事務的なAIっぽい報告はNGです。"
        else:
            sleep_score = sleep_summary.get('sleep_score', 0)
            sleep_time = self._format_minutes(sleep_summary.get('minutesAsleep', 0))
            context_data = f"【昨晩の睡眠データ】\nスコア: {sleep_score} / 100\n合計睡眠時間: {sleep_time}\n【最近の会話ログ】\n{today_log}"
            instruction = "親密な20代女性のパートナーとして、LINEのような温かみのあるタメ口で話して。朝の挨拶（おはよう、今日も頑張ろう等）は6時に済ませているので絶対に省いてください。昨晩の睡眠データ（スコアや時間）を見て、「よく眠れたみたいだね！」「ちょっと睡眠短かったね、無理しないでね」など、体調を気遣う一言だけを自然に添えて報告して。事務的な報告botにならないように注意してください。"
        
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
        
        await self._save_data_to_obsidian(target_date, sleep_summary, activity_data)
        
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return
        
        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        today_log = "（会話ログなし）"
        if channel:
            today_log = await partner_cog.fetch_todays_chat_log(channel)
        
        sleep_text = f"スコア: {sleep_summary.get('sleep_score', 'N/A')}, 睡眠時間: {self._format_minutes(sleep_summary.get('minutesAsleep', 0))}" if sleep_summary else "データなし"
        activity_text = f"歩数: {activity_data.get('summary', {}).get('steps', 'N/A')}歩, 消費: {activity_data.get('summary', {}).get('caloriesOut', 'N/A')}kcal" if activity_data else "データなし"
        
        context_data = f"【本日の睡眠】\n{sleep_text}\n【本日の活動】\n{activity_text}"
        instruction = "親密な20代女性のパートナーとして、LINEのような温かみのあるタメ口で話して。22時に一日の振り返り（お疲れ様などの挨拶）は済ませているので、挨拶は省き「今日のFitbitデータまとまったよ！」と軽く報告して。歩数や消費カロリーなどの数値を短く褒めたり、健康を気遣う一言だけを添えてください。絶対に事務的なAIにならず、恋人との短いLINEのようにしてください。"
        
        await partner_cog.generate_and_send_routine_message(context_data, instruction)

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