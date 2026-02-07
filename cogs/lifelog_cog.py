import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import json
from datetime import datetime, date, timedelta, time
import zoneinfo
import re

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
DAILY_NOTE_HEADER = "## Life Logs"
READING_NOTES_FOLDER = "Reading Notes"
ACTIVE_LOGS_FILE = "active_lifelogs.json"
BOT_FOLDER = ".bot"

DURATION_REGEX = re.compile(r'\s+(\d+(?:\.\d+)?)(h|m|min|hour|時間|分)?$', re.IGNORECASE)
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

# --- UI Components (省略なしで記述しますが、変更点はなし) ---
class LifeLogMemoModal(discord.ui.Modal, title="作業メモの入力"):
    memo_text = discord.ui.TextInput(label="メモ", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    def __init__(self, cog): super().__init__(); self.cog = cog
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(); await self.cog.add_memo_to_task(interaction, self.memo_text.value)

class LifeLogConfirmTaskView(discord.ui.View):
    def __init__(self, cog, task_name, duration, original_message):
        super().__init__(timeout=60); self.cog=cog; self.task_name=task_name; self.duration=duration; self.original_message=original_message
    @discord.ui.button(label="開始", style=discord.ButtonStyle.success)
    async def start(self, interaction, button):
        if interaction.user.id!=self.original_message.author.id: return
        await interaction.response.defer(); await interaction.delete_original_response()
        await self.cog.switch_task(self.original_message, self.task_name, self.duration); self.stop()
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if interaction.user.id!=self.original_message.author.id: return
        await interaction.response.defer(); await interaction.delete_original_response(); self.stop()

class LifeLogTaskView(discord.ui.View):
    def __init__(self, cog): super().__init__(timeout=None); self.cog = cog
    @discord.ui.button(label="終了", style=discord.ButtonStyle.danger, custom_id="lifelog_finish")
    async def finish(self, interaction, button): await interaction.response.defer(ephemeral=True); await self.cog.finish_current_task(interaction.user, interaction)
    @discord.ui.button(label="メモ", style=discord.ButtonStyle.primary, custom_id="lifelog_memo")
    async def memo(self, interaction, button): await self.cog.prompt_memo_modal(interaction)

class LifeLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lifelog_channel_id = int(os.getenv("LIFELOG_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.monitor_tasks = {}
        self.is_ready = bool(self.drive_folder_id)

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
        file = service.files().create(body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id').execute()
        return file.get('id')

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

    def _read_text(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _update_text(self, service, file_id, content):
        service.files().update(fileId=file_id, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()
    
    def _create_text(self, service, parent_id, name, content):
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    async def on_ready(self):
        self.bot.add_view(LifeLogTaskView(self))
        if self.is_ready: await self._resume_active_task_monitoring()

    # --- Active Logs ---
    async def _get_active_logs(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return {}
        
        bot_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not bot_folder: return {}
        
        log_file = await loop.run_in_executor(None, self._find_file, service, bot_folder, ACTIVE_LOGS_FILE)
        if log_file:
            return await loop.run_in_executor(None, self._read_json, service, log_file)
        return {}

    async def _save_active_logs(self, data):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        bot_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not bot_folder: bot_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, BOT_FOLDER)

        log_file = await loop.run_in_executor(None, self._find_file, service, bot_folder, ACTIVE_LOGS_FILE)
        await loop.run_in_executor(None, self._write_json, service, bot_folder, ACTIVE_LOGS_FILE, data, log_file)

    # --- Task Logic ---
    async def add_memo_to_task(self, interaction, memo_text):
        uid = str(interaction.user.id)
        logs = await self._get_active_logs()
        if uid in logs:
            logs[uid].setdefault('memos', []).append(memo_text)
            await self._save_active_logs(logs)
            await interaction.followup.send("✅ メモ記録", ephemeral=True)
        else: await interaction.followup.send("⚠️ タスクなし", ephemeral=True)

    async def prompt_memo_modal(self, interaction): await interaction.response.send_modal(LifeLogMemoModal(self))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.channel.id != self.lifelog_channel_id: return
        content = message.content.strip()
        if not content: return
        if content.lower().startswith("m ") or content.startswith("ｍ "):
            await self._add_memo_from_message(message, content[2:].strip())
            return
        
        task_name, duration = self._parse_task_and_duration(content)
        view = LifeLogConfirmTaskView(self, task_name, duration, message)
        await message.reply(f"タスク「**{task_name}**」を開始しますか？ ({duration}分)", view=view)

    async def _add_memo_from_message(self, message, memo):
        uid = str(message.author.id)
        logs = await self._get_active_logs()
        if uid in logs:
            logs[uid].setdefault('memos', []).append(memo)
            await self._save_active_logs(logs)
            await message.add_reaction("📝")
        else: await message.add_reaction("❓")

    async def switch_task(self, message, new_task, duration):
        await self.finish_current_task(message.author, message, next_task_name=new_task)
        await self.start_new_task_context(message.channel, message.author, new_task, duration)

    async def start_new_task_context(self, channel, user, task, duration):
        uid = str(user.id)
        now = datetime.now(JST)
        end = now + timedelta(minutes=duration)
        msg = await channel.send(f"⏱️ **{task}** 開始 ({now.strftime('%H:%M')} - {end.strftime('%H:%M')})", view=LifeLogTaskView(self))
        logs = await self._get_active_logs()
        logs[uid] = {"task": task, "start_time": now.isoformat(), "planned_duration": duration, "message_id": msg.id, "channel_id": msg.channel.id, "memos": []}
        await self._save_active_logs(logs)

    async def finish_current_task(self, user, context, next_task_name=None):
        uid = str(user.id)
        logs = await self._get_active_logs()
        if uid not in logs:
            if context and isinstance(context, discord.Interaction): await context.followup.send("⚠️ タスクなし", ephemeral=True)
            return
        
        data = logs.pop(uid)
        await self._save_active_logs(logs)
        
        start = datetime.fromisoformat(data['start_time'])
        end = datetime.now(JST)
        duration = end - start
        dur_str = f"{int(duration.total_seconds()//60)}m"
        line = f"- {start.strftime('%H:%M')} - {end.strftime('%H:%M')} ({dur_str}) **{data['task']}**"
        
        if data.get('memos'):
            line += "\n" + "\n".join([f"\t- {m}" for m in data['memos']])
            
        await self._save_to_obsidian(start.strftime('%Y-%m-%d'), line)
        
        if isinstance(context, discord.Interaction) and not next_task_name:
            await context.followup.send(f"✅ 終了: {data['task']} ({dur_str})", ephemeral=True)

    async def _save_to_obsidian(self, date_str, line):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return
        
        daily_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: daily_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")
        
        file_name = f"{date_str}.md"
        file_id = await loop.run_in_executor(None, self._find_file, service, daily_folder, file_name)
        
        cur = ""
        if file_id: cur = await loop.run_in_executor(None, self._read_text, service, file_id)
        
        new = update_section(cur, line, DAILY_NOTE_HEADER)
        
        if file_id: await loop.run_in_executor(None, self._update_text, service, file_id, new)
        else: await loop.run_in_executor(None, self._create_text, service, daily_folder, file_name, new)

    def _parse_task_and_duration(self, content):
        m = DURATION_REGEX.search(content)
        if m:
            val = float(m.group(1))
            unit = m.group(2)
            mins = int(val * 60) if unit and unit.lower() in ['h', 'hour', '時間'] else int(val)
            return content[:m.start()].strip(), mins
        return content, 30

    async def _resume_active_task_monitoring(self): pass # 簡易化のため省略

async def setup(bot): await bot.add_cog(LifeLogCog(bot))