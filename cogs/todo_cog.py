import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
import re
from datetime import time, datetime
import zoneinfo

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TASK_FILE_PATH = "/Tasks/TaskLog.md" # タスクファイルのパス
TASK_ADD_REACTION = "☑️" # メモをタスク化するリアクション

# ==========================================
# UI Components
# ==========================================

class TaskAddModal(discord.ui.Modal, title="タスクの追加"):
    task_content = discord.ui.TextInput(
        label="タスク内容",
        placeholder="例: プレゼン資料作成",
        style=discord.TextStyle.short,
        required=True,
        max_length=200
    )

    def __init__(self, cog, view_to_refresh=None):
        super().__init__()
        self.cog = cog
        self.view_to_refresh = view_to_refresh

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.add_task_logic(self.task_content.value)
        await interaction.followup.send(f"✅ タスクを追加しました: {self.task_content.value}", ephemeral=True)
        if self.view_to_refresh:
            await self.view_to_refresh.refresh_embed(interaction)

class TaskSelectView(discord.ui.View):
    """完了または削除するタスクを選択するView"""
    def __init__(self, cog, tasks: list[str], mode: str, parent_view=None):
        super().__init__(timeout=60)
        self.cog = cog
        self.mode = mode # "complete" or "delete"
        self.parent_view = parent_view
        
        options = []
        for t in tasks[:25]: # Selectの上限は25
            # Markdownのチェックボックスを除去して表示
            clean_text = re.sub(r'^\s*-\s*\[.\]\s*', '', t).strip()
            label = clean_text[:95] + "..." if len(clean_text) > 95 else clean_text
            options.append(discord.SelectOption(label=label, value=t))

        if not options:
            options.append(discord.SelectOption(label="タスクがありません", value="none"))

        select = discord.ui.Select(
            placeholder="タスクを選択してください...",
            min_values=1,
            max_values=min(len(options), 25),
            options=options
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected_tasks = interaction.data["values"]
        
        if "none" in selected_tasks:
            return

        if self.mode == "complete":
            await self.cog.complete_tasks_logic(selected_tasks)
            msg = f"✅ {len(selected_tasks)}件のタスクを完了にしました。"
        else:
            await self.cog.delete_tasks_logic(selected_tasks)
            msg = f"🗑️ {len(selected_tasks)}件のタスクを削除しました。"

        await interaction.followup.send(msg, ephemeral=True)
        if self.parent_view:
            await self.parent_view.refresh_embed(interaction)
        self.stop()

class TaskDashboardView(discord.ui.View):
    """タスク一覧の下に表示する操作ボタン"""
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        self.message = None

    async def refresh_embed(self, interaction: discord.Interaction = None):
        """一覧を最新化して更新する"""
        content, embed = await self.cog.create_task_list_embed()
        if self.message:
            try:
                await self.message.edit(content=content, embed=embed, view=self)
            except: pass
        elif interaction:
             # ボタン押下時などでメッセージが見つからない場合（あまりないが念のため）
             pass

    @discord.ui.button(label="追加", style=discord.ButtonStyle.success, emoji="➕", custom_id="task_add_btn")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TaskAddModal(self.cog, self))

    @discord.ui.button(label="完了", style=discord.ButtonStyle.primary, emoji="✅", custom_id="task_complete_btn")
    async def complete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        tasks = await self.cog.get_active_tasks()
        if not tasks:
            await interaction.response.send_message("未完了のタスクはありません。", ephemeral=True)
            return
        await interaction.response.send_message("完了にするタスクを選択してください:", view=TaskSelectView(self.cog, tasks, "complete", self), ephemeral=True)

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="task_delete_btn")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        tasks = await self.cog.get_active_tasks()
        if not tasks:
            await interaction.response.send_message("削除可能なタスクはありません。", ephemeral=True)
            return
        await interaction.response.send_message("削除するタスクを選択してください:", view=TaskSelectView(self.cog, tasks, "delete", self), ephemeral=True)

    @discord.ui.button(label="更新", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="task_refresh_btn")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh_embed()

# ==========================================
# Cog Class
# ==========================================

class TodoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._load_env_vars()
        self.dbx = None
        
        # Dropbox Init
        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
            except Exception as e:
                logging.error(f"TodoCog: Dropbox Init Error: {e}")

        # Start Loop
        self.daily_task_notification.start()

    def _load_env_vars(self):
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.task_file_full_path = f"{self.dropbox_vault_path}{TASK_FILE_PATH}"

    def cog_unload(self):
        self.daily_task_notification.cancel()

    # --- Dropbox Helpers ---

    async def _download_task_file(self) -> str:
        if not self.dbx: return ""
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, self.task_file_full_path)
            return res.content.decode('utf-8')
        except ApiError: return "" # ファイルがない場合は空

    async def _upload_task_file(self, content: str):
        if not self.dbx: return
        try:
            await asyncio.to_thread(
                self.dbx.files_upload,
                content.encode('utf-8'),
                self.task_file_full_path,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"TodoCog Upload Error: {e}")

    async def get_active_tasks(self) -> list[str]:
        """未完了タスクのリストを取得（行そのまま）"""
        content = await self._download_task_file()
        tasks = []
        for line in content.split('\n'):
            # "- [ ]" で始まる行
            if re.match(r'^\s*-\s*\[ \]', line):
                tasks.append(line.strip())
        return tasks

    async def create_task_list_embed(self):
        """タスク一覧のEmbedを作成"""
        tasks = await self.get_active_tasks()
        
        if not tasks:
            desc = "現在、未完了のタスクはありません。今日も一日頑張りましょう！"
            color = discord.Color.green()
        else:
            # 表示用に整形
            formatted_tasks = []
            for t in tasks:
                clean = re.sub(r'^\s*-\s*\[ \]\s*', '', t)
                formatted_tasks.append(f"• {clean}")
            
            desc = "\n".join(formatted_tasks)
            if len(desc) > 4000: desc = desc[:3900] + "\n... (他多数)"
            color = discord.Color.blue()

        embed = discord.Embed(
            title="📋 Master Task List",
            description=desc,
            color=color,
            timestamp=datetime.now(JST)
        )
        embed.set_footer(text=f"Sync: {TASK_FILE_PATH}")
        return "☀️ **Good Morning!** 今日のタスク一覧です。", embed

    # --- Task Logic ---

    async def add_task_logic(self, content: str):
        """タスクを追加する"""
        current = await self._download_task_file()
        new_line = f"- [ ] {content}"
        # 末尾に追加（空行調整）
        if current and not current.endswith('\n'):
            new_content = current + f"\n{new_line}"
        else:
            new_content = current + f"{new_line}\n"
        await self._upload_task_file(new_content)

    async def complete_tasks_logic(self, target_lines: list[str]):
        """指定されたタスク（行全体が一致）を完了にする"""
        current = await self._download_task_file()
        lines = current.split('\n')
        new_lines = []
        for line in lines:
            if line.strip() in target_lines:
                # [ ] -> [x]
                new_line = re.sub(r'\[ \]', '[x]', line, count=1)
                new_lines.append(new_line)
            else:
                new_lines.append(line)
        await self._upload_task_file("\n".join(new_lines))

    async def delete_tasks_logic(self, target_lines: list[str]):
        """指定されたタスク（行全体が一致）を削除する"""
        current = await self._download_task_file()
        lines = current.split('\n')
        new_lines = [line for line in lines if line.strip() not in target_lines]
        await self._upload_task_file("\n".join(new_lines))

    # --- External Logic for OCR (Handwritten) ---

    async def process_ocr_tasks(self, tasks_data: list[dict]) -> dict:
        """
        手書きOCRから渡されたタスクデータを処理する。
        tasks_data format: [{"status": "x" or ">" or "-", "text": "内容"}, ...]
        
        Returns:
            dict: 処理結果のサマリー {"completed": [], "migrated": [], "notes": []}
        """
        current_content = await self._download_task_file()
        lines = current_content.split('\n')
        new_lines = list(lines) # 変更用
        
        results = {"completed": [], "migrated": [], "notes": []}

        for item in tasks_data:
            symbol = item.get("status")
            text = item.get("text", "").strip()
            if not text: continue

            if symbol == "x": # 完了
                # 部分一致で完了にする（手書きはデジタルと完全に一致しないことがあるため）
                found = False
                for i, line in enumerate(new_lines):
                    if re.match(r'^\s*-\s*\[ \]', line) and text in line:
                        new_lines[i] = re.sub(r'\[ \]', '[x]', line, count=1)
                        results["completed"].append(text)
                        found = True
                        break
                # 見つからない場合はログだけ残すか、無視する

            elif symbol == ">": # 引き継ぎ (Master Listにあるべき)
                # 既存リストになければ追加
                found = False
                for line in new_lines:
                    if re.match(r'^\s*-\s*\[ \]', line) and text in line:
                        found = True
                        break
                if not found:
                    new_lines.append(f"- [ ] {text}")
                    results["migrated"].append(f"{text} (Added)")
                else:
                    results["migrated"].append(f"{text} (Kept)")

            elif symbol == "-": # メモ
                # これはタスクリストには反映せず、JournalCog側でログとして扱うため、ここでは返り値に含めるだけ
                results["notes"].append(text)

        await self._upload_task_file("\n".join(new_lines))
        return results

    # --- Scheduled Loop ---

    @tasks.loop(time=time(hour=6, minute=0, tzinfo=JST))
    async def daily_task_notification(self):
        """毎朝6時にNewsチャンネルにタスク一覧を投稿"""
        if not self.news_channel_id: return
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel: return

        content, embed = await self.create_task_list_embed()
        view = TaskDashboardView(self)
        msg = await channel.send(content=content, embed=embed, view=view)
        view.message = msg # Viewにメッセージを持たせて更新可能にする

    # --- Events ---

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """メモチャンネルでリアクションがついたらタスク追加"""
        if payload.channel_id != self.memo_channel_id: return
        if payload.member.bot: return
        if str(payload.emoji) != TASK_ADD_REACTION: return

        channel = self.bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
            task_content = message.content.strip()
            
            if task_content:
                await self.add_task_logic(task_content)
                await message.add_reaction("🆗") # 完了リアクション
                # ユーザーに通知（任意）
                # await channel.send(f"✅ 「{task_content[:20]}...」をタスクに追加しました。", delete_after=5)
        except Exception as e:
            logging.error(f"Reaction Task Add Error: {e}")

    # --- Slash Commands (Manual) ---

    @app_commands.command(name="todo", description="タスク一覧を表示・操作します。")
    async def show_todo_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        content, embed = await self.create_task_list_embed()
        view = TaskDashboardView(self)
        msg = await interaction.followup.send(content=content, embed=embed, view=view)
        view.message = msg

async def setup(bot: commands.Bot):
    await bot.add_cog(TodoCog(bot))