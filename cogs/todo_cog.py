import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import json
import asyncio
from datetime import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TODO_JSON_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/todo_list.json"
DAILY_NOTE_TODO_HEADER = "## Completed Tasks"

# --- リアクションとカテゴリのマッピング ---
REACTION_MAP = {
    "🛒": "Buy",   # 買い物
    "📝": "Task",  # タスク
    "🤔": "Think"  # 検討
}

# --- タスク追加用モーダル ---
class TodoAddModal(discord.ui.Modal):
    def __init__(self, cog, view, category):
        super().__init__(title=f"{category}の追加")
        self.cog = cog
        self.view = view
        self.category = category
        
        self.item_input = discord.ui.TextInput(
            label="内容",
            placeholder="例: 洗剤を買う、夏休みの旅行計画",
            style=discord.TextStyle.short,
            required=True
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        content = self.item_input.value
        await self.cog.add_todo(interaction.user, content, self.category)
        await self.view.update_message(interaction)
        await interaction.followup.send(f"✅ 追加しました: {content}", ephemeral=True)

# --- メインView ---
class TodoListView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def update_message(self, interaction: discord.Interaction):
        """メッセージの内容（Embed）を最新のJSONに基づいて更新する"""
        embed = await self.cog.create_todo_embed()
        try:
            if isinstance(interaction, discord.Interaction):
                if interaction.message:
                    await interaction.message.edit(embed=embed, view=self)
        except Exception as e:
            logging.error(f"TodoListView update error: {e}")

    @discord.ui.button(label="タスク追加", style=discord.ButtonStyle.primary, emoji="📝", custom_id="todo_add_task")
    async def add_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TodoAddModal(self.cog, self, "Task"))

    @discord.ui.button(label="買い物追加", style=discord.ButtonStyle.success, emoji="🛒", custom_id="todo_add_buy")
    async def add_buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TodoAddModal(self.cog, self, "Buy"))
        
    @discord.ui.button(label="検討事項追加", style=discord.ButtonStyle.secondary, emoji="🤔", custom_id="todo_add_think")
    async def add_think(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TodoAddModal(self.cog, self, "Think"))

    @discord.ui.button(label="完了/削除", style=discord.ButtonStyle.danger, emoji="✅", custom_id="todo_complete")
    async def complete_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        todos = await self.cog._load_todos()
        if not todos:
            await interaction.response.send_message("完了するタスクがありません。", ephemeral=True)
            return
            
        view = TodoCompleteSelectView(self.cog, self, todos)
        await interaction.response.send_message("完了・削除するタスクを選択してください:", view=view, ephemeral=True)


# --- 完了選択用View ---
class TodoCompleteSelectView(discord.ui.View):
    def __init__(self, cog, parent_view, todos):
        super().__init__(timeout=60)
        self.cog = cog
        self.parent_view = parent_view
        
        options = []
        for i, todo in enumerate(todos[:25]):
            label = f"[{todo['category']}] {todo['content']}"
            if len(label) > 100: label = label[:97] + "..."
            options.append(discord.SelectOption(label=label, value=str(i)))

        select = discord.ui.Select(
            placeholder="完了したタスクを選択...",
            min_values=1,
            max_values=min(len(todos), 25),
            options=options
        )
        select.callback = self.callback
        self.add_item(select)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected_indices = [int(v) for v in interaction.data["values"]]
        selected_indices.sort(reverse=True)
        
        completed_items = await self.cog.complete_todos(selected_indices)
        await self.parent_view.update_message(interaction)
        
        msg_lines = [f"✅ {item['content']}" for item in completed_items]
        await interaction.followup.send(f"以下のタスクを完了し、Obsidianに記録しました:\n" + "\n".join(msg_lines), ephemeral=True)


class TodoCog(commands.Cog):
    """簡易ToDoリスト管理Cog"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # MEMO_CHANNEL_ID を流用して監視する
        self.target_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.dbx = None
        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
            except Exception as e:
                logging.error(f"TodoCog Init Error: {e}")

    async def _load_todos(self) -> list:
        if not self.dbx: return []
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, TODO_JSON_PATH)
            return json.loads(res.content.decode('utf-8'))
        except (ApiError, json.JSONDecodeError):
            return []

    async def _save_todos(self, todos: list):
        if not self.dbx: return
        try:
            data = json.dumps(todos, ensure_ascii=False, indent=2).encode('utf-8')
            await asyncio.to_thread(self.dbx.files_upload, data, TODO_JSON_PATH, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"TodoCog Save Error: {e}")

    async def add_todo(self, user: discord.User, content: str, category: str):
        """ToDoを追加する共通メソッド"""
        todos = await self._load_todos()
        new_todo = {
            "content": content,
            "category": category,
            "created_at": datetime.now(JST).isoformat(),
            "user": user.display_name
        }
        todos.append(new_todo)
        await self._save_todos(todos)

    async def complete_todos(self, indices: list[int]) -> list:
        todos = await self._load_todos()
        completed = []
        for i in indices:
            if 0 <= i < len(todos):
                item = todos.pop(i)
                completed.append(item)
        await self._save_todos(todos)
        
        if completed:
            await self._archive_to_obsidian(completed)
        return completed

    async def _archive_to_obsidian(self, items: list):
        if not self.dbx: return
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        lines = []
        for item in items:
            lines.append(f"- [x] {now.strftime('%H:%M')} [{item['category']}] {item['content']}")
        text_to_add = "\n".join(lines)

        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                content = res.content.decode('utf-8')
            except ApiError:
                content = "" # ★ 修正: 初期値を空文字に変更

            new_content = update_section(content, text_to_add, DAILY_NOTE_TODO_HEADER)
            await asyncio.to_thread(
                self.dbx.files_upload, 
                new_content.encode('utf-8'), 
                daily_note_path, 
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"TodoArchive Error: {e}")

    async def create_todo_embed(self) -> discord.Embed:
        """ToDoリストのEmbedを作成する（汎用）"""
        todos = await self._load_todos()
        embed = discord.Embed(title="📝 やることリスト (ToDo)", color=discord.Color.teal())
        
        tasks = [t for t in todos if t['category'] == 'Task']
        buys = [t for t in todos if t['category'] == 'Buy']
        thinks = [t for t in todos if t['category'] == 'Think']
        
        def format_list(items):
            if not items: return "なし"
            return "\n".join([f"▫️ {item['content']}" for item in items])

        embed.add_field(name="🛒 買い物 (Buy)", value=format_list(buys), inline=False)
        embed.add_field(name="📝 タスク (Task)", value=format_list(tasks), inline=False)
        embed.add_field(name="🤔 検討 (Think)", value=format_list(thinks), inline=False)
        embed.set_footer(text=f"合計: {len(todos)} 件")
        return embed

    async def get_todos_formatted(self) -> discord.Embed:
        """外部Cog（NewsCogなど）から呼び出すためのメソッド"""
        return await self.create_todo_embed()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if payload.channel_id != self.target_channel_id:
            return

        emoji_str = str(payload.emoji)
        if emoji_str not in REACTION_MAP:
            return

        category = REACTION_MAP[emoji_str]
        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        if message.author.bot or not message.content.strip():
            return
        
        content = message.content.strip()
        
        user = await self.bot.fetch_user(payload.user_id)
        await self.add_todo(user, content, category)
        logging.info(f"Message added to ToDo via reaction {emoji_str}: [{category}] {content}")
        
        try:
            await message.add_reaction("✅")
        except:
            pass

    @app_commands.command(name="todo", description="ToDoリストパネルを表示します。")
    async def show_todo(self, interaction: discord.Interaction):
        if interaction.channel_id != self.target_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.target_channel_id}> でのみ実行できます。", ephemeral=True)
            return
        await interaction.response.defer()
        embed = await self.create_todo_embed()
        view = TodoListView(self)
        await interaction.followup.send(embed=embed, view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(TodoCog(bot))