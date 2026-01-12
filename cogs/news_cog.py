import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import json
from datetime import datetime, time, timezone, timedelta
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import asyncio
import aiohttp
import re
from typing import Optional, List

# Try importing update_section for Obsidian Sync
try:
    from utils.obsidian_utils import update_section
except ImportError:
    # Fallback if utils not available
    def update_section(content, text, header):
        return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# JMA (天気) 関連
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {
    "晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"
}

# Dropbox Settings
BASE_PATH = os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')
NEWS_SCHEDULE_PATH = f"{BASE_PATH}/.bot/news_schedule.json"
CUSTOM_MESSAGES_PATH = f"{BASE_PATH}/.bot/custom_daily_messages.json"

class NewsCog(commands.Cog):
    """天気予報、カスタムメッセージ、習慣レポートを定時通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()

        if not self._are_credentials_valid():
            logging.error("NewsCog: 必須の環境変数が不足。Cogを無効化します。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            )
            self.briefing_lock = asyncio.Lock()
            self.is_ready = True
            
            self.daily_news_briefing.add_exception_type(Exception)
            logging.info("✅ NewsCogが正常に初期化されました。")

        except Exception as e:
            logging.error(f"❌ NewsCogの初期化中にエラー: {e}", exc_info=True)

    def _load_environment_variables(self):
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        self.jma_area_name = os.getenv("JMA_AREA_NAME", "南部")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _are_credentials_valid(self) -> bool:
        return all([
            self.news_channel_id,
            self.dropbox_app_key,
            self.dropbox_app_secret,
            self.dropbox_refresh_token,
        ])

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return
        await self.bot.wait_until_ready()
        
        schedule_data = await self._load_schedule_from_db()
        if schedule_data:
            hour = schedule_data['hour']
            minute = schedule_data['minute']
            saved_time = time(hour=hour, minute=minute, tzinfo=JST)
            self.daily_news_briefing.change_interval(time=saved_time)
            if not self.daily_news_briefing.is_running():
                self.daily_news_briefing.start()
            logging.info(f"定時ニュースブリーフィングタスクを開始しました (毎日 {saved_time} JST)")
        else:
            logging.info("定時ニューススケジュールが設定されていません。")

    def cog_unload(self):
        self.daily_news_briefing.cancel()

    # --- Schedule Helpers ---
    async def _load_schedule_from_db(self) -> Optional[dict]:
        if not self.dbx: return None
        try:
            _, res = self.dbx.files_download(NEWS_SCHEDULE_PATH)
            data = json.loads(res.content.decode('utf-8'))
            return {"hour": int(data.get('hour')), "minute": int(data.get('minute'))}
        except Exception:
            return None

    async def _save_schedule_to_db(self, hour: int, minute: int):
        if not self.dbx: raise Exception("Dropbox client not initialized")
        data = {"hour": hour, "minute": minute}
        content = json.dumps(data, indent=2).encode('utf-8')
        self.dbx.files_upload(content, NEWS_SCHEDULE_PATH, mode=WriteMode('overwrite'))

    async def _delete_schedule_from_db(self):
        if not self.dbx: raise Exception("Dropbox client not initialized")
        try:
            self.dbx.files_delete_v2(NEWS_SCHEDULE_PATH)
        except ApiError as e:
            if not (isinstance(e.error, dropbox.exceptions.PathLookupError) and e.error.is_not_found()):
                raise

    # --- Weather Logic ---
    def _get_emoji_for_weather(self, weather_text: str) -> str:
        for key, emoji in WEATHER_EMOJI_MAP.items():
            if key in weather_text: return emoji
        return "❓"

    async def _get_jma_weather_forecast(self) -> tuple[discord.Embed, str]:
        """天気を取得し、Discord用EmbedとObsidian保存用テキストを返す"""
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"
        
        embed = discord.Embed(
            title=f"🗓️ {datetime.now(JST).strftime('%Y年%m月%d日')} のお知らせ",
            color=discord.Color.blue()
        )
        
        # Obsidian用のテキスト構築用
        obsidian_lines = []
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    data = await response.json()

                area_weather = next((a for a in data[0]["timeSeries"][0]["areas"] if a["area"]["name"] == self.jma_area_name), None)
                area_temp = next((a for a in data[0]["timeSeries"][2]["areas"] if a["area"]["name"] == self.location_name), None)

                if area_weather and area_temp:
                    weather_summary = area_weather["weathers"][0]
                    weather_emoji = self._get_emoji_for_weather(weather_summary)
                    max_temp = area_temp.get("temps", ["--"])[1]
                    min_temp = area_temp.get("temps", ["--"])[0]
                    
                    val = f"{weather_emoji} {weather_summary}\n🌡️ 最高: {max_temp}℃ / 最低: {min_temp}℃"
                    embed.add_field(name=f"今日の天気 ({self.location_name})", value=val, inline=False)
                    
                    # Obsidian用テキスト
                    obsidian_lines.append(f"- **Forecast**: {weather_emoji} {weather_summary}")
                    obsidian_lines.append(f"- **Temp**: H:{max_temp}℃ / L:{min_temp}℃")
                else:
                    embed.add_field(name="天気", value="⚠️ 取得失敗", inline=False)
                    obsidian_lines.append("- **Weather**: Retrieval Failed")

                # 時間別降水確率・気温（Discord表示のみ維持）
                time_defines_pop = data[0]["timeSeries"][1]["timeDefines"]
                area_pops = next((a["pops"] for a in data[0]["timeSeries"][1]["areas"] if a["area"]["name"] == self.jma_area_name), None)
                time_defines_temp = data[0]["timeSeries"][2]["timeDefines"]
                area_temps = next((a["temps"] for a in data[0]["timeSeries"][2]["areas"] if a["area"]["name"] == self.location_name), None)

                if area_pops and area_temps:
                    pop_text, temp_text = "", ""
                    for i, time_str in enumerate(time_defines_pop):
                        dt = datetime.fromisoformat(time_str)
                        if dt.date() == datetime.now(JST).date():
                            pop_text += f"**{dt.strftime('%H時')}**: {area_pops[i]}% "
                    for i, time_str in enumerate(time_defines_temp):
                        dt = datetime.fromisoformat(time_str)
                        if dt.date() == datetime.now(JST).date():
                            temp_text += f"**{dt.strftime('%H時')}**: {area_temps[i]}℃ "
                    
                    if pop_text: embed.add_field(name="☂️ 降水確率", value=pop_text.strip(), inline=False)
                    if temp_text: embed.add_field(name="🕒 時間別気温", value=temp_text.strip(), inline=False)

                return embed, "\n".join(obsidian_lines)

            except Exception as e:
                logging.error(f"天気取得エラー: {e}")
                embed.add_field(name="エラー", value="⚠️ 天気情報の取得に失敗しました。", inline=False)
                return embed, ""

    async def _save_weather_to_obsidian(self, text: str):
        """Obsidianのデイリーノートに天気を保存"""
        if not text: return
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{today_str}.md"
        
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                content = res.content.decode('utf-8')
            except ApiError:
                content = f"# Daily Note {today_str}\n"

            # '## Weather' セクションに追記または作成
            new_content = update_section(content, text, "## Weather")
            
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"Obsidianに天気情報を保存しました: {daily_note_path}")
        except Exception as e:
            logging.error(f"Obsidian天気保存エラー: {e}")

    # --- Custom Messages Logic ---
    async def _get_custom_messages(self) -> List[str]:
        if not self.dbx: return []
        try:
            _, res = self.dbx.files_download(CUSTOM_MESSAGES_PATH)
            data = json.loads(res.content.decode('utf-8'))
            return data if isinstance(data, list) else []
        except ApiError:
            return []
        except Exception as e:
            logging.error(f"カスタムメッセージ読み込みエラー: {e}")
            return []

    async def _save_custom_messages(self, messages: List[str]):
        if not self.dbx: return
        try:
            data = json.dumps(messages, ensure_ascii=False, indent=2).encode('utf-8')
            self.dbx.files_upload(data, CUSTOM_MESSAGES_PATH, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"カスタムメッセージ保存エラー: {e}")

    # --- Daily Briefing Logic ---
    async def run_daily_briefing(self, channel: discord.TextChannel):
        if not channel or self.briefing_lock.locked(): return

        async with self.briefing_lock:
            logging.info(f"Daily Briefing Start: {channel.name}")
            
            # 1. Weather (Discord Notification + Obsidian Sync)
            try:
                weather_embed, weather_text = await self._get_jma_weather_forecast()
                await channel.send(embed=weather_embed)
                await self._save_weather_to_obsidian(weather_text)
            except Exception as e:
                 logging.error(f"Weather Error: {e}")
                 await channel.send(f"⚠️ 天気予報エラー: `{e}`")

            # 2. Custom Daily Messages
            try:
                msgs = await self._get_custom_messages()
                if msgs:
                    await channel.send("--- 📢 Daily Notices ---")
                    for msg in msgs:
                        await channel.send(f"・ {msg}")
            except Exception as e:
                logging.error(f"Custom Message Error: {e}")

            # 3. Weekly Habit Stats (Requested to keep)
            try:
                habit_cog = self.bot.get_cog("HabitCog")
                if habit_cog:
                    habit_embed = await habit_cog.get_weekly_stats_embed()
                    await channel.send(embed=habit_embed)
            except Exception as e:
                logging.error(f"Habit Stats Error: {e}")

            logging.info("Daily Briefing Completed")

    @tasks.loop()
    async def daily_news_briefing(self):
        if not self.daily_news_briefing.time: return
        channel = self.bot.get_channel(self.news_channel_id)
        if channel:
            await self.run_daily_briefing(channel)

    # --- Commands ---
    briefing_group = app_commands.Group(name="briefing", description="ニュースブリーフィング管理")
    message_group = app_commands.Group(name="daily_message", description="毎日の定型通知メッセージを管理")

    @message_group.command(name="add", description="毎日の通知メッセージを追加します")
    async def msg_add(self, interaction: discord.Interaction, message: str):
        await interaction.response.defer(ephemeral=True)
        msgs = await self._get_custom_messages()
        msgs.append(message)
        await self._save_custom_messages(msgs)
        await interaction.followup.send(f"✅ メッセージを追加しました:\n{message}", ephemeral=True)

    @message_group.command(name="list", description="登録されている通知メッセージを確認します")
    async def msg_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msgs = await self._get_custom_messages()
        if not msgs:
            await interaction.followup.send("登録されているメッセージはありません。", ephemeral=True)
            return
        
        text = "\n".join([f"{i+1}. {m}" for i, m in enumerate(msgs)])
        await interaction.followup.send(f"📋 **登録メッセージ一覧:**\n{text}", ephemeral=True)

    @message_group.command(name="remove", description="通知メッセージを削除します")
    async def msg_remove(self, interaction: discord.Interaction, index: int):
        await interaction.response.defer(ephemeral=True)
        msgs = await self._get_custom_messages()
        
        if 1 <= index <= len(msgs):
            removed = msgs.pop(index - 1)
            await self._save_custom_messages(msgs)
            await interaction.followup.send(f"🗑️ 削除しました: {removed}", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ 指定された番号 ({index}) は無効です。", ephemeral=True)

    @briefing_group.command(name="run_now", description="ブリーフィングを手動実行します")
    async def news_run_now(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"<#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.send_message("✅ 手動実行を開始します...", ephemeral=True)
        await self.run_daily_briefing(interaction.channel)

    @briefing_group.command(name="set_schedule", description="ブリーフィングの定時実行時刻 (JST) を設定します")
    @app_commands.describe(schedule_time="実行時刻 (HH:MM形式, JST)")
    async def news_set_schedule(self, interaction: discord.Interaction, schedule_time: str):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"<#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return
        
        match = re.match(r'^([0-2]?[0-9]):([0-5]?[0-9])$', schedule_time.strip())
        if not match:
            await interaction.response.send_message("❌ 時刻は `HH:MM` 形式で入力してください。", ephemeral=True)
            return

        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await interaction.response.send_message("❌ 時刻の範囲が不正です。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self._save_schedule_to_db(hour, minute)
            new_time = time(hour=hour, minute=minute, tzinfo=JST)
            self.daily_news_briefing.change_interval(time=new_time)
            if not self.daily_news_briefing.is_running():
                self.daily_news_briefing.start()
            await interaction.followup.send(f"✅ 定時実行時刻を **{hour:02d}:{minute:02d} (JST)** に設定しました。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

    @briefing_group.command(name="cancel_schedule", description="定時実行を停止・削除します")
    async def news_cancel_schedule(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"<#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        try:
            if self.daily_news_briefing.is_running():
                self.daily_news_briefing.stop()
            await self._delete_schedule_from_db()
            await interaction.followup.send("✅ 定時実行を停止し、スケジュールを削除しました。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))