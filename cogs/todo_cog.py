import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import re
from datetime import time, datetime
import zoneinfo

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TASKS_FOLDER = "Tasks"
TASK_FILE_NAME = "TaskLog.md"
TASK_ADD_REACTION = "☑️"

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class TaskAddModal(discord.ui.Modal, title="タスクの追加"):
    task_content = discord.ui.TextInput(label="タスク内容", placeholder="例: プレゼン資料作成", style=discord.TextStyle.short, required=True, max_length=200)
    def __init__(self, cog, view_to_refresh=None):
        super().__init__(); self.cog=cog; self.view_to_refresh=view_to_refresh
    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.add_task_logic(self.task_content.value)
        await interaction.followup.send(f"✅ タスクを追加しました: {self.task_content.value}", ephemeral=True)
        if self.view_to_refresh: await self.view_to_refresh.refresh_embed(interaction)

class TaskSelectView(discord.ui.View):
    def __init__(self, cog, tasks: list[str], mode: str, parent_view=None):
        super().__init__(timeout=60); self.cog=cog; self.mode=mode; self.parent_view=parent_view
        options = []
        for t in tasks[:25]:
            clean_text = re.sub(r'^\s*-\s*\[.\]\s*', '', t).strip()
            label = clean_text[:95] + "..." if len(clean_text) > 95 else clean_text
            options.append(discord.SelectOption(label=label, value=t))
        if not options: options.append(discord.SelectOption(label="タスクがありません", value="none"))
        select = discord.ui.Select(placeholder="タスクを選択...", min_values=1, max_values=min(len(options),25), options=options)
        select.callback = self.select_callback; self.add_item(select)

    async def select_callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        selected = interaction.data["values"]
        if "none" in selected: return
        if self.mode == "complete":
            await self.cog.complete_tasks_logic(selected)
            msg = f"✅ {len(selected)}件のタスクを完了にしました。"
        else:
            await self.cog.delete_tasks_logic(selected)
            msg = f"🗑️ {len(selected)}件のタスクを削除しました。"
        await interaction.followup.send(msg, ephemeral=True)
        if self.parent_view: await self.parent_view.refresh_embed(interaction)
        self.stop()

class TaskDashboardView(discord.ui.View):
    def __init__(self, cog): super().__init__(timeout=None); self.cog=cog; self.message=None
    async def refresh_embed(self, interaction=None):
        content, embed = await self.cog.create_task_list_embed()
        if self.message:
            try: await self.message.edit(content=content, embed=embed, view=self)
            except: pass
    @discord.ui.button(label="追加", style=discord.ButtonStyle.success, emoji="➕", custom_id="task_add_btn")
    async def add_btn(self, interaction, button): await interaction.response.send_modal(TaskAddModal(self.cog, self))
    @discord.ui.button(label="完了", style=discord.ButtonStyle.primary, emoji="✅", custom_id="task_complete_btn")
    async def complete_btn(self, interaction, button):
        tasks = await self.cog.get_active_tasks()
        if not tasks: return await interaction.response.send_message("未完了のタスクはありません。", ephemeral=True)
        await interaction.response.send_message("完了にするタスクを選択:", view=TaskSelectView(self.cog, tasks, "complete", self), ephemeral=True)
    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="task_delete_btn")
    async def delete_btn(self, interaction, button):
        tasks = await self.cog.get_active_tasks()
        if not tasks: return await interaction.response.send_message("削除可能なタスクはありません。", ephemeral=True)
        await interaction.response.send_message("削除するタスクを選択:", view=TaskSelectView(self.cog, tasks, "delete", self), ephemeral=True)
    @discord.ui.button(label="更新", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="task_refresh_btn")
    async def refresh_btn(self, interaction, button): await interaction.response.defer(); await self.refresh_embed()

class TodoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.daily_task_notification.start()

    def cog_unload(self):
        self.daily_task_notification.cancel()

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

    async def _get_task_file_id(self, service):
        loop = asyncio.get_running_loop()
        tasks_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, TASKS_FOLDER)
        if not tasks_folder: tasks_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, TASKS_FOLDER)
        
        task_file = await loop.run_in_executor(None, self._find_file, service, tasks_folder, TASK_FILE_NAME)
        return task_file, tasks_folder

    async def _download_task_file(self) -> str:
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return ""
        
        file_id, _ = await self._get_task_file_id(service)
        if not file_id: return ""
        
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    async def _upload_task_file(self, content: str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return
        
        file_id, folder_id = await self._get_task_file_id(service)
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        
        if file_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=file_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': TASK_FILE_NAME, 'parents': [folder_id], 'mimeType': 'text/markdown'}, media_body=media).execute())

    async def get_active_tasks(self) -> list[str]:
        content = await self._download_task_file()
        tasks = []
        for line in content.split('\n'):
            if re.match(r'^\s*-\s*\[ \]', line): tasks.append(line.strip())
        return tasks

    async def create_task_list_embed(self):
        tasks = await self.get_active_tasks()
        if not tasks:
            desc = "現在、未完了のタスクはありません。"; color = discord.Color.green()
        else:
            formatted = [f"• {re.sub(r'^\s*-\s*\[ \]\s*', '', t)}" for t in tasks]
            desc = "\n".join(formatted)
            if len(desc) > 4000: desc = desc[:3900] + "\n... (他多数)"
            color = discord.Color.blue()
        embed = discord.Embed(title="📋 Master Task List", description=desc, color=color, timestamp=datetime.now(JST))
        embed.set_footer(text=f"Sync: {TASKS_FOLDER}/{TASK_FILE_NAME}")
        return "☀️ **Good Morning!** 今日のタスク一覧です。", embed

    async def add_task_logic(self, content):
        current = await self._download_task_file()
        new_line = f"- [ ] {content}"
        new_content = current + f"\n{new_line}" if current and not current.endswith('\n') else current + f"{new_line}\n"
        await self._upload_task_file(new_content)

    async def complete_tasks_logic(self, target_lines):
        current = await self._download_task_file()
        new_lines = [re.sub(r'\[ \]', '[x]', line, count=1) if line.strip() in target_lines else line for line in current.split('\n')]
        await self._upload_task_file("\n".join(new_lines))

    async def delete_tasks_logic(self, target_lines):
        current = await self._download_task_file()
        new_lines = [line for line in current.split('\n') if line.strip() not in target_lines]
        await self._upload_task_file("\n".join(new_lines))

    @tasks.loop(time=time(hour=6, minute=0, tzinfo=JST))
    async def daily_task_notification(self):
        if not self.news_channel_id: return
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel: return
        content, embed = await self.create_task_list_embed()
        view = TaskDashboardView(self)
        msg = await channel.send(content=content, embed=embed, view=view)
        view.message = msg

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != self.memo_channel_id or payload.member.bot or str(payload.emoji) != TASK_ADD_REACTION: return
        channel = self.bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
            if message.content.strip():
                await self.add_task_logic(message.content.strip())
                await message.add_reaction("🆗")
        except: pass

    @app_commands.command(name="todo", description="タスク一覧を表示・操作します。")
    async def show_todo_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        content, embed = await self.create_task_list_embed()
        view = TaskDashboardView(self)
        msg = await interaction.followup.send(content=content, embed=embed, view=view)
        view.message = msg

async def setup(bot: commands.Bot):
    await bot.add_cog(TodoCog(bot))