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
from discord import app_commands
from typing import Optional, Dict, Any

from fitbit_client import FitbitClient
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SLEEP_REPORT_TIME = datetime.time(hour=8, minute=0, tzinfo=JST)
FULL_HEALTH_REPORT_TIME = datetime.time(hour=22, minute=0, tzinfo=JST)

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

    def _process_sleep_data(self, sleep_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """複数の睡眠ログを統合し、サマリーデータを作成する"""
        if not sleep_data or 'sleep' not in sleep_data or not sleep_data['sleep']:
            return None

        total_minutes_asleep = 0
        total_time_in_bed = 0
        total_weighted_efficiency = 0
        stage_summary = {'deep': 0, 'light': 0, 'rem': 0, 'wake': 0}

        for log in sleep_data['sleep']:
            minutes_asleep = log.get('minutesAsleep', 0)
            total_minutes_asleep += minutes_asleep
            total_time_in_bed += log.get('timeInBed', 0)
            
            # 睡眠時間で重み付けした効率スコアを計算
            efficiency = log.get('efficiency', 0)
            total_weighted_efficiency += efficiency * minutes_asleep
            
            if 'levels' in log and 'summary' in log['levels']:
                for stage, data in log['levels']['summary'].items():
                    if stage in stage_summary:
                        stage_summary[stage] += data.get('minutes', 0)

        # 加重平均で総合スコアを算出
        overall_efficiency = round(total_weighted_efficiency / total_minutes_asleep) if total_minutes_asleep > 0 else 0
        
        return {
            'efficiency': overall_efficiency,
            'minutesAsleep': total_minutes_asleep,
            'timeInBed': total_time_in_bed,
            'levels': {'summary': stage_summary}
        }

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.sleep_report.is_running():
                self.sleep_report.start()
                logging.info(f"FitbitCog: 睡眠レポートタスクを {SLEEP_REPORT_TIME} にスケジュールしました。")
            if not self.full_health_report.is_running():
                self.full_health_report.start()
                logging.info(f"FitbitCog: 統合ヘルスレポートタスクを {FULL_HEALTH_REPORT_TIME} にスケジュールしました。")

    def cog_unload(self):
        self.sleep_report.cancel()
        self.full_health_report.cancel()

    def _format_minutes(self, minutes: int) -> str:
        if minutes is None: return "N/A"
        h, m = divmod(minutes, 60)
        return f"{h}時間{m}分" if h > 0 else f"{m}分"

    @tasks.loop(time=SLEEP_REPORT_TIME)
    async def sleep_report(self):
        """朝にその日の睡眠データだけを速報として通知する"""
        if not self.is_ready: return
        
        logging.info(f"FitbitCog: 睡眠レポートタスクを実行します。")
        channel = self.bot.get_channel(self.health_log_channel_id)
        
        try:
            target_date = datetime.datetime.now(JST).date()
            raw_sleep_data = await self.fitbit_client.get_sleep_data(target_date)
            sleep_summary = self._process_sleep_data(raw_sleep_data)

            if not sleep_summary:
                logging.warning(f"FitbitCog: {target_date} の睡眠データが取得できませんでした。")
                if channel:
                    await channel.send(f" FitbitCog: {target_date.strftime('%Y-%m-%d')} の睡眠データがまだ同期されていないようです。")
                return

            if channel:
                embed = discord.Embed(
                    title=f"🌙 {target_date.strftime('%Y年%m月%d日')}の睡眠レポート (速報)",
                    color=discord.Color.purple()
                )
                embed.add_field(name="睡眠スコア", value=f"**{sleep_summary.get('efficiency', 0)}** 点", inline=True)
                embed.add_field(name="合計睡眠時間", value=f"**{self._format_minutes(sleep_summary.get('minutesAsleep', 0))}**", inline=True)
                embed.set_footer(text="活動データを含む1日のまとめは夜に通知されます。")
                await channel.send(embed=embed)
                logging.info(f"FitbitCog: {target_date} の睡眠レポートをDiscordに投稿しました。")

        except Exception as e:
            logging.error(f"FitbitCog: 睡眠レポートタスクの実行中にエラーが発生しました: {e}", exc_info=True)
            if channel:
                await channel.send(f"FitbitCog: 睡眠レポートタスクの実行中にエラーが発生しました。\n```\n{e}\n```")

    @tasks.loop(time=FULL_HEALTH_REPORT_TIME)
    async def full_health_report(self):
        """夜に1日の健康データをまとめて通知・保存する"""
        if not self.is_ready: return

        logging.info(f"FitbitCog: 統合ヘルスレポートタスクを実行します。")
        channel = self.bot.get_channel(self.health_log_channel_id)

        try:
            target_date = datetime.datetime.now(JST).date()
            
            raw_sleep_data, activity_data = await asyncio.gather(
                self.fitbit_client.get_sleep_data(target_date),
                self.fitbit_client.get_activity_summary(target_date)
            )
            
            sleep_summary = self._process_sleep_data(raw_sleep_data)

            if not sleep_summary and not activity_data:
                logging.warning(f"FitbitCog: {target_date} の全データが取得できませんでした。")
                return
            
            advice_text = await self._generate_ai_advice(target_date, sleep_summary, activity_data)
            
            await self._save_data_to_obsidian(target_date, sleep_summary, activity_data, advice_text)
            
            if channel:
                embed = await self._create_discord_embed(target_date, sleep_summary, activity_data, advice_text)
                await channel.send(embed=embed)
                logging.info(f"FitbitCog: {target_date} の統合ヘルスレポートをDiscordに投稿しました。")

        except Exception as e:
            logging.error(f"FitbitCog: 統合ヘルスレポートタスクの実行中にエラーが発生しました: {e}", exc_info=True)
            if channel:
                await channel.send(f"FitbitCog: 統合ヘルスレポートタスクの実行中にエラーが発生しました。\n```\n{e}\n```")

    @app_commands.command(name="get_morning_report", description="指定日の睡眠レポートを手動で取得します。")
    @app_commands.describe(date="取得したい日付 (YYYY-MM-DD形式、省略で今日)")
    async def get_morning_report(self, interaction: discord.Interaction, date: str = None):
        """朝の睡眠レポートを手動で取得するスラッシュコマンド"""
        await interaction.response.defer(ephemeral=True)
        if not self.is_ready:
            await interaction.followup.send("Fitbit Cogが初期化されていません。")
            return
        
        try:
            target_date = datetime.datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.datetime.now(JST).date()
        except ValueError:
            await interaction.followup.send("日付の形式が正しくありません。YYYY-MM-DD形式で入力してください。")
            return

        channel = self.bot.get_channel(self.health_log_channel_id)
        raw_sleep_data = await self.fitbit_client.get_sleep_data(target_date)
        sleep_summary = self._process_sleep_data(raw_sleep_data)

        if not sleep_summary:
            msg = f"FitbitCog: {target_date.strftime('%Y-%m-%d')} の睡眠データがまだ同期されていないようです。"
            if channel: await channel.send(msg)
            await interaction.followup.send(msg)
            return

        if channel:
            embed = discord.Embed(
                title=f"🌙 {target_date.strftime('%Y年%m月%d日')}の睡眠レポート (手動取得)",
                color=discord.Color.purple()
            )
            embed.add_field(name="睡眠スコア", value=f"**{sleep_summary.get('efficiency', 0)}** 点", inline=True)
            embed.add_field(name="合計睡眠時間", value=f"**{self._format_minutes(sleep_summary.get('minutesAsleep', 0))}**", inline=True)
            await channel.send(embed=embed)
            await interaction.followup.send(f"{target_date.strftime('%Y-%m-%d')}の睡眠レポートを送信しました。")
        else:
            await interaction.followup.send("レポートを送信するチャンネルが見つかりません。")


    @app_commands.command(name="get_evening_report", description="指定日の総合ヘルスレポートを手動で取得します。")
    @app_commands.describe(date="取得したい日付 (YYYY-MM-DD形式、省略で今日)")
    async def get_evening_report(self, interaction: discord.Interaction, date: str = None):
        """夜の総合レポートを手動で取得するスラッシュコマンド"""
        await interaction.response.defer(ephemeral=True)
        if not self.is_ready:
            await interaction.followup.send("Fitbit Cogが初期化されていません。")
            return

        try:
            target_date = datetime.datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.datetime.now(JST).date()
        except ValueError:
            await interaction.followup.send("日付の形式が正しくありません。YYYY-MM-DD形式で入力してください。")
            return
        
        channel = self.bot.get_channel(self.health_log_channel_id)
        
        raw_sleep_data, activity_data = await asyncio.gather(
            self.fitbit_client.get_sleep_data(target_date),
            self.fitbit_client.get_activity_summary(target_date)
        )
        sleep_summary = self._process_sleep_data(raw_sleep_data)

        if not sleep_summary and not activity_data:
            msg = f"FitbitCog: {target_date} の全データが取得できませんでした。"
            if channel: await channel.send(msg)
            await interaction.followup.send(msg)
            return
        
        advice_text = await self._generate_ai_advice(target_date, sleep_summary, activity_data)
        
        await self._save_data_to_obsidian(target_date, sleep_summary, activity_data, advice_text)
        
        if channel:
            embed = await self._create_discord_embed(target_date, sleep_summary, activity_data, advice_text, is_manual=True)
            await channel.send(embed=embed)
            await interaction.followup.send(f"{target_date.strftime('%Y-%m-%d')}の総合ヘルスレポートを送信・保存しました。")
        else:
            await interaction.followup.send("レポートを送信するチャンネルが見つかりません。")

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
                f"- **Score:** {sleep_data.get('efficiency', 'N/A')} / 100\n"
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
        あなたは私の成長をサポートするヘルスコーチです。
        以下のデータを元に、私の健康状態を分析し、改善のためのアドバイスをしてください。

        # 今日のデータ
        - {today_sleep_text}
        - {today_activity_text}

        # 指示
        - **挨拶や前置きは一切含めないでください。**
        - **最も重要なポイントに絞って簡潔に記述してください。**
        - 良い点を1つ、改善できる点を1つ、具体的なアクションと共に提案してください。
        - アドバイスの本文のみを生成してください。
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"FitbitCog: Gemini APIからのアドバイス生成中にエラー: {e}")
            return "AIによるアドバイスの生成中にエラーが発生しました。"
    
    async def _summarize_text(self, text: str, max_length: int = 1000) -> str:
        """テキストが長すぎる場合にAIで要約する"""
        try:
            prompt = f"以下のテキストを、Discordで表示するために{max_length}文字以内で簡潔に要約してください:\n\n---\n{text}"
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"テキストの要約に失敗: {e}")
            return text[:max_length] + "..."

    async def _create_discord_embed(self, target_date: datetime.date, sleep_data: dict, activity_data: dict, advice: str, is_manual: bool = False) -> discord.Embed:
        title = f"📅 {target_date.strftime('%Y年%m月%d日')}のヘルスレポート"
        if is_manual:
            title += " (手動取得)"

        embed = discord.Embed(title=title, color=discord.Color.blue())

        # Sleep
        if sleep_data:
            levels = sleep_data.get('levels', {}).get('summary', {})
            sleep_text = (
                f"**スコア**: **{sleep_data.get('efficiency', 'N/A')}** / 100\n"
                f"**合計睡眠時間**: {self._format_minutes(sleep_data.get('minutesAsleep'))}\n"
                f"**ベッドにいた時間**: {self._format_minutes(sleep_data.get('timeInBed'))}\n"
                f"**ステージ**: 深い {self._format_minutes(levels.get('deep'))}, "
                f"レム {self._format_minutes(levels.get('rem'))}, "
                f"浅い {self._format_minutes(levels.get('light'))}"
            )
            embed.add_field(name="🌙 睡眠", value=sleep_text, inline=False)
        
        # Activity & Heart Rate
        if activity_data:
            summary = activity_data.get('summary', {})
            activity_text = (
                f"**歩数**: {summary.get('steps', 'N/A')}歩\n"
                f"**距離**: {next((d['distance'] for d in summary.get('distances', []) if d['activity'] == 'total'), 'N/A')} km\n"
                f"**消費カロリー**: {summary.get('caloriesOut', 'N/A')} kcal\n"
                f"**アクティブな時間**: {self._format_minutes(summary.get('fairlyActiveMinutes', 0) + summary.get('veryActiveMinutes', 0))}"
            )
            embed.add_field(name="🏃 アクティビティ", value=activity_text, inline=True)

            hr_zones = summary.get('heartRateZones', {})
            heart_rate_text = (
                f"**安静時心拍数**: {summary.get('restingHeartRate', 'N/A')} bpm\n"
                f"**脂肪燃焼**: {self._format_minutes(hr_zones.get('Fat Burn', {}).get('minutes'))}\n"
                f"**有酸素運動**: {self._format_minutes(hr_zones.get('Cardio', {}).get('minutes'))}\n"
                f"**ピーク**: {self._format_minutes(hr_zones.get('Peak', {}).get('minutes'))}"
            )
            embed.add_field(name="❤️ 心拍数", value=heart_rate_text, inline=True)

        # AI Coach
        advice_text = advice
        if len(advice_text) > 1024:
            advice_text = await self._summarize_text(advice, 1024)
            
        embed.add_field(name="🤖 AI Health Coach", value=advice_text, inline=False)
        
        embed.set_footer(text="Powered by Fitbit & Gemini")
        embed.timestamp = datetime.datetime.now(JST)
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))