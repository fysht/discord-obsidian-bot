import os
import discord
from discord.ext import commands, tasks
import logging
import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import google.generativeai as genai
import yaml
from io import StringIO
import asyncio

from fitbit_client import FitbitClient
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HEALTH_LOG_TIME = datetime.time(hour=22, minute=00, tzinfo=JST)

class FitbitCog(commands.Cog):
    """Fitbitのデータを取得し、Obsidianへの記録とAIによる健康アドバイスを行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.fitbit_client_id = os.getenv("FITBIT_CLIENT_ID")
        self.fitbit_client_secret = os.getenv("FITBIT_CLIENT_SECRET")
        self.fitbit_refresh_token = os.getenv("FITBIT_REFRESH_TOKEN")
        self.fitbit_user_id = os.getenv("FITBIT_USER_ID", "-")
        self.health_log_channel_id = int(os.getenv("HEALTH_LOG_CHANNEL_ID", 0))

        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        self.is_ready = self._validate_and_init_clients()
        if self.is_ready: logging.info("FitbitCog: 正常に初期化されました。")
        else: logging.error("FitbitCog: 環境変数が不足しているため、初期化に失敗しました。")

    def _validate_and_init_clients(self) -> bool:
        if not all([self.fitbit_client_id, self.fitbit_client_secret, self.fitbit_refresh_token,
                    self.health_log_channel_id, self.dropbox_refresh_token, self.gemini_api_key]):
            return False
        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret
            )
            self.fitbit_client = FitbitClient(
                self.fitbit_client_id, self.fitbit_client_secret, self.fitbit_refresh_token, self.dbx, self.fitbit_user_id
            )
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            return True
        except Exception as e:
            logging.error(f"FitbitCogのクライアント初期化中にエラー: {e}", exc_info=True)
            return False

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready and not self.daily_health_log.is_running():
            self.daily_health_log.start()
            logging.info(f"FitbitCog: ヘルスログタスクを {HEALTH_LOG_TIME} にスケジュールしました。")

    def cog_unload(self):
        self.daily_health_log.cancel()

    def _format_minutes(self, minutes: int) -> str:
        if minutes is None: return "N/A"
        h, m = divmod(minutes, 60)
        return f"{h}時間{m}分" if h > 0 else f"{m}分"

    @tasks.loop(time=HEALTH_LOG_TIME)
    async def daily_health_log(self):
        if not self.is_ready: return
        
        logging.info(f"FitbitCog: 定時タスクを実行します。対象日: {datetime.datetime.now(JST).date() - datetime.timedelta(days=1)}")
        channel = self.bot.get_channel(self.health_log_channel_id)
        
        try:
            target_date = datetime.datetime.now(JST).date() - datetime.timedelta(days=1)
            
            sleep_data, activity_data = await asyncio.gather(
                self.fitbit_client.get_sleep_data(target_date),
                self.fitbit_client.get_activity_summary(target_date)
            )

            if not sleep_data and not activity_data:
                logging.warning(f"FitbitCog: {target_date} の全データが取得できませんでした。")
                if channel:
                    await channel.send(f" FitbitCog: {target_date.strftime('%Y-%m-%d')} のデータがまだ同期されていないようです。後で再試行します。")
                return
            
            advice_text = await self._generate_ai_advice(target_date, sleep_data, activity_data)
            
            await self._save_data_to_obsidian(target_date, sleep_data, activity_data, advice_text)
            
            if channel:
                embed = self._create_discord_embed(target_date, sleep_data, activity_data, advice_text)
                await channel.send(embed=embed)
                logging.info(f"FitbitCog: {target_date} のヘルスログをDiscordに投稿しました。")

        except Exception as e:
            logging.error(f"FitbitCog: 定期タスクの実行中にエラーが発生しました: {e}", exc_info=True)
            if channel:
                await channel.send(f"FitbitCog: 定期タスクの実行中にエラーが発生しました。\n```\n{e}\n```")

    def _parse_note_content(self, content: str) -> (dict, str):
        try:
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return yaml.safe_load(StringIO(parts[1])) or {}, parts[2].lstrip()
        except yaml.YAMLError: pass
        return {}, content

    async def _save_data_to_obsidian(self, target_date: datetime.date, sleep_data: dict, activity_data: dict, advice_text: str):
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{target_date.strftime('%Y-%m-%d')}.md"
        
        try:
            _, res = self.dbx.files_download(daily_note_path)
            current_content = res.content.decode('utf-8')
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                current_content = ""
            else: raise

        frontmatter, body = self._parse_note_content(current_content)
        
        if sleep_data:
            levels = sleep_data.get('levels', {}).get('summary', {})
            frontmatter.update({
                'sleep_score': sleep_data.get('efficiency'),
                'total_sleep_minutes': sleep_data.get('minutesAsleep'),
                'deep_sleep_minutes': levels.get('deep', {}).get('minutes'),
                'rem_sleep_minutes': levels.get('rem', {}).get('minutes'),
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
                f"- **Score:** {sleep_data.get('efficiency', 'N/A')} / 100\n"
                f"- **Total Sleep:** {self._format_minutes(sleep_data.get('minutesAsleep'))}\n"
                f"- **Time in Bed:** {self._format_minutes(sleep_data.get('timeInBed'))}\n"
                f"- **Stages:** Deep {self._format_minutes(levels.get('deep', {}).get('minutes'))}, "
                f"REM {self._format_minutes(levels.get('rem', {}).get('minutes'))}, "
                f"Light {self._format_minutes(levels.get('light', {}).get('minutes'))}"
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

        if advice_text:
            ai_coach_text = (
                f"#### AI Health Coach\n"
                f"{advice_text}"
            )
            metrics_sections.append(ai_coach_text)
        
        new_body = update_section(body, "\n\n".join(metrics_sections), "## Health Metrics")
        
        new_daily_content = f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n{new_body}"
        
        self.dbx.files_upload(new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
        logging.info(f"FitbitCog: {daily_note_path} を更新しました。")

    async def _generate_ai_advice(self, target_date: datetime.date, sleep_data: dict, activity_data: dict) -> str:
        today_sleep_text = ""
        if sleep_data:
            today_sleep_text = (f"今日の睡眠: スコア {sleep_data.get('efficiency', 'N/A')}, "
                              f"合計睡眠時間 {self._format_minutes(sleep_data.get('minutesAsleep', 0))}")
        today_activity_text = ""
        if activity_data:
            summary = activity_data.get('summary', {})
            today_activity_text = (f"今日の活動: 歩数 {summary.get('steps', 'N/A')}歩, "
                                   f"安静時心拍数 {summary.get('restingHeartRate', 'N/A')}bpm")

        prompt = f"""
あなたは私の成長をサポートする優秀なヘルスコーチです。
以下の本日の睡眠と活動データを元に、私の健康状態を分析し、改善のための具体的でポジティブなアドバイスをしてください。

# 今日のデータ
- {today_sleep_text}
- {today_activity_text}

# 指示
- 睡眠と活動の両方の観点から、良い点をまず褒めてください。
- 改善できる点を1〜2点、具体的なアクションと共に提案してください。
- 全体的にポジティブで、実行したくなるようなトーンでお願いします。
- アドバイス本文のみを生成してください。
"""
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"FitbitCog: Gemini APIからのアドバイス生成中にエラー: {e}")
            return "AIによるアドバイスの生成中にエラーが発生しました。"

    def _create_discord_embed(self, target_date: datetime.date, sleep_data: dict, activity_data: dict, advice: str) -> discord.Embed:
        title = f"📅 {target_date.strftime('%Y年%m月%d日')}のヘルスレポート"
        
        embed = discord.Embed(title=title, description=advice, color=discord.Color.blue())

        if sleep_data:
            embed.add_field(name="🌙 睡眠スコア", value=f"**{sleep_data.get('efficiency', 0)}** 点", inline=True)
            embed.add_field(name="⏰ 合計睡眠時間", value=f"**{self._format_minutes(sleep_data.get('minutesAsleep', 0))}**", inline=True)
        
        if activity_data:
            summary = activity_data.get('summary', {})
            embed.add_field(name="👟 歩数", value=f"**{summary.get('steps', 0)}** 歩", inline=True)
            embed.add_field(name="🔥 消費カロリー", value=f"**{summary.get('caloriesOut', 0)}** kcal", inline=True)
        
        embed.set_footer(text="Powered by Fitbit & Gemini")
        embed.timestamp = datetime.datetime.now(JST)
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))