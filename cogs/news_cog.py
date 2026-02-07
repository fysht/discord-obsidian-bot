import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import json
from datetime import datetime, time, timezone, timedelta
import zoneinfo
import asyncio
import aiohttp
import re
from typing import Optional, List

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

# Try importing update_frontmatter for Obsidian Sync
try:
    from utils.obsidian_utils import update_frontmatter
except ImportError:
    logging.warning("NewsCog: utils.obsidian_utils not found. update_frontmatter disabled.")
    def update_frontmatter(content, updates): return content

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# JMA (天気) 関連
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {
    "晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"
}

# Google Drive Settings
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'
BOT_FOLDER = ".bot"
NEWS_SCHEDULE_FILE = "news_schedule.json"
CUSTOM_MESSAGES_FILE = "custom_daily_messages.json"

class NewsCog(commands.Cog):
    """天気予報、カスタムメッセージ、習慣レポートを定時通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._load_environment_variables()
        self.is_ready = bool(self.news_channel_id and self.drive_folder_id)
        
        if self.is_ready:
            self.briefing_lock = asyncio.Lock()
            self.daily_news_briefing.add_exception_type(Exception)
            logging.info("✅ NewsCog initialized.")
        else:
            logging.error("NewsCog: 必須の環境変数が不足しています。")

    def _load_environment_variables(self):
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        self.jma_area_name = os.getenv("JMA_AREA_NAME", "南部")
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    # --- Drive Helpers ---
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
        res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _create_folder(self, service, parent_id, name):
        f = service.files().create(body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id').execute()
        return f.get('id')

    def _read_json(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return json.loads(fh.getvalue().decode('utf-8'))

    def _write_json(self, service, parent_id, name, data, file_id=None):
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False).encode('utf-8')), mimetype='application/json')
        if file_id: service.files().update(fileId=file_id, media_body=media).execute()
        else: service.files().create(body={'name': name, 'parents': [parent_id]}, media_body=media).execute()

    def _delete_file(self, service, file_id):
        try: service.files().delete(fileId=file_id).execute()
        except: pass

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return
        
        schedule_data = await self._load_schedule_from_drive()
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
    async def _load_schedule_from_drive(self) -> Optional[dict]:
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return None
        
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return None
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, NEWS_SCHEDULE_FILE)
        if f_id:
            data = await loop.run_in_executor(None, self._read_json, service, f_id)
            return {"hour": int(data.get('hour')), "minute": int(data.get('minute'))}
        return None

    async def _save_schedule_to_drive(self, hour: int, minute: int):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: b_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, BOT_FOLDER)
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, NEWS_SCHEDULE_FILE)
        data = {"hour": hour, "minute": minute}
        await loop.run_in_executor(None, self._write_json, service, b_folder, NEWS_SCHEDULE_FILE, data, f_id)

    async def _delete_schedule_from_drive(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return
        
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if b_folder:
            f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, NEWS_SCHEDULE_FILE)
            if f_id: await loop.run_in_executor(None, self._delete_file, service, f_id)

    # --- Weather Logic ---
    def _get_emoji_for_weather(self, weather_text: str) -> str:
        for key, emoji in WEATHER_EMOJI_MAP.items():
            if key in weather_text: return emoji
        return "❓"

    async def _get_jma_weather_forecast(self) -> tuple[discord.Embed, dict]:
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"
        embed = discord.Embed(title=f"🗓️ {datetime.now(JST).strftime('%Y年%m月%d日')} のお知らせ", color=discord.Color.blue())
        property_updates = {}
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    data = await response.json()

                area_weather = next((a for a in data[0]["timeSeries"][0]["areas"] if a["area"]["name"] == self.jma_area_name), None)
                if area_weather:
                    weather_summary = area_weather["weathers"][0].replace('\u3000', ' ')
                    weather_emoji = self._get_emoji_for_weather(weather_summary)
                    property_updates['weather'] = f"{weather_emoji} {weather_summary}"
                else:
                    weather_summary = "不明"; weather_emoji = "❓"

                area_temps = next((a for a in data[0]["timeSeries"][2]["areas"] if a["area"]["name"] == self.location_name), None)
                max_temp_str = "--"; min_temp_str = "--"

                if area_temps and "temps" in area_temps:
                    temps = area_temps["temps"]
                    valid_temps = []
                    for t in temps:
                        try:
                            if t and t != "--": valid_temps.append(float(t))
                        except ValueError: pass
                    
                    if valid_temps:
                        max_val = max(valid_temps); min_val = min(valid_temps)
                        max_temp_str = str(int(max_val)); min_temp_str = str(int(min_val))
                        property_updates['max_temp'] = int(max_val); property_updates['min_temp'] = int(min_val)

                val = f"{weather_emoji} {weather_summary}\n🌡️ 最高: {max_temp_str}℃ / 最低: {min_temp_str}℃"
                embed.add_field(name=f"今日の天気 ({self.location_name})", value=val, inline=False)
                
                area_pops = next((a["pops"] for a in data[0]["timeSeries"][1]["areas"] if a["area"]["name"] == self.jma_area_name), None)
                if area_pops:
                    time_defines_pop = data[0]["timeSeries"][1]["timeDefines"]
                    pop_text = ""
                    for i, time_str in enumerate(time_defines_pop):
                        dt = datetime.fromisoformat(time_str)
                        if dt.date() == datetime.now(JST).date():
                            pop_text += f"**{dt.strftime('%H時')}**: {area_pops[i]}% "
                    if pop_text: embed.add_field(name="☂️ 降水確率", value=pop_text.strip(), inline=False)
                return embed, property_updates

            except Exception as e:
                logging.error(f"天気取得エラー: {e}")
                embed.add_field(name="エラー", value="⚠️ 天気情報の取得に失敗しました。", inline=False)
                return embed, {}

    async def _save_weather_to_obsidian(self, updates: dict):
        if not updates: return
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        daily_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        f_id = await loop.run_in_executor(None, self._find_file, service, daily_folder, f"{today_str}.md")
        
        content = f"# Daily Note {today_str}\n"
        if f_id:
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
            done=False
            while not done: _, done = downloader.next_chunk()
            content = fh.getvalue().decode('utf-8')

        new_content = update_frontmatter(content, updates)
        
        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown')
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{today_str}.md", 'parents': [daily_folder]}, media_body=media).execute())

    # --- Custom Messages Logic ---
    async def _get_custom_messages(self) -> List[str]:
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return []
        
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return []
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, CUSTOM_MESSAGES_FILE)
        if f_id:
            return await loop.run_in_executor(None, self._read_json, service, f_id)
        return []

    async def _save_custom_messages(self, messages: List[str]):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: b_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, BOT_FOLDER)
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, CUSTOM_MESSAGES_FILE)
        await loop.run_in_executor(None, self._write_json, service, b_folder, CUSTOM_MESSAGES_FILE, messages, f_id)


    # --- Daily Briefing Logic ---
    async def run_daily_briefing(self, channel: discord.TextChannel):
        if not channel or self.briefing_lock.locked(): return

        async with self.briefing_lock:
            logging.info(f"Daily Briefing Start: {channel.name}")
            
            try:
                weather_embed, weather_updates = await self._get_jma_weather_forecast()
                await channel.send(embed=weather_embed)
                await self._save_weather_to_obsidian(weather_updates)
            except Exception as e:
                 logging.error(f"Weather Error: {e}")
                 await channel.send(f"⚠️ 天気予報エラー: `{e}`")

            try:
                msgs = await self._get_custom_messages()
                if msgs:
                    await channel.send("--- 📢 Daily Notices ---")
                    for msg in msgs:
                        await channel.send(f"・ {msg}")
            except Exception as e:
                logging.error(f"Custom Message Error: {e}")

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
            await self._save_schedule_to_drive(hour, minute)
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
            await self._delete_schedule_from_drive()
            await interaction.followup.send("✅ 定時実行を停止し、スケジュールを削除しました。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))