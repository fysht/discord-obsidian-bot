import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
from datetime import datetime, timezone, timedelta
import json
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import asyncio

MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))
TASK_LIST_CHANNEL_ID = int(os.getenv("TASK_LIST_CHANNEL_ID", "0"))
JST = timezone(timedelta(hours=+9), 'JST')

LISTS_PATH = "/Lists"
ADD_TO_LIST_EMOJI = '➕'
CATEGORY_MAP = {
    "Task": {"file": "Tasks.md", "prompt": "タスク"},
    "Idea": {"file": "Ideas.md", "prompt": "アイデア"},
    "Shopping": {"file": "Shopping List.md", "prompt": "買い物リスト"},
    "Bookmark": {"file": "Bookmarks.md", "prompt": "ブックマーク"},
}
CONTEXT_CHOICES = [
    app_commands.Choice(name="仕事", value="Work"),
    app_commands.Choice(name="プライベート", value="Personal")
]
CATEGORY_CHOICES = [
    app_commands.Choice(name="タスク", value="Task"),
    app_commands.Choice(name="アイデア", value="Idea"),
    app_commands.Choice(name="買い物リスト", value="Shopping"),
    app_commands.Choice(name="ブックマーク", value="Bookmark")
]

# --- 新しいUIコンポーネント ---

class AddItemModal(discord.ui.Modal, title="リストに項目を追加"):
    def __init__(self, memo_cog_instance):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance

    context_select = discord.ui.Select(placeholder="コンテキストを選択", options=[
        discord.SelectOption(label="仕事", value="Work"),
        discord.SelectOption(label="プライベート", value="Personal")
    ])
    category_select = discord.ui.Select(placeholder="カテゴリを選択", options=[
        discord.SelectOption(label="タスク", value="Task"),
        discord.SelectOption(label="アイデア", value="Idea"),
        discord.SelectOption(label="買い物リスト", value="Shopping"),
        discord.SelectOption(label="ブックマーク", value="Bookmark")
    ])
    item_to_add = discord.ui.TextInput(label="追加する項目名", placeholder="例: 新しいプロジェクトの企画書を作成する")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        context = self.context_select.values[0]
        category = self.category_select.values[0]
        item = self.item_to_add.value

        success = await self.memo_cog.add_item_to_list_file(category, item, context)
        if success:
            await interaction.followup.send(f"✅ **{context}** の **{CATEGORY_MAP[category]['prompt']}** に「{item}」を追加しました。", ephemeral=True)
            await self.memo_cog.refresh_all_lists_post(interaction.channel)
        else:
            await interaction.followup.send("❌リストへの追加中にエラーが発生しました。", ephemeral=True)

class CompleteItemModal(discord.ui.Modal, title="リストの項目を完了"):
    def __init__(self, memo_cog_instance, items_by_category: dict):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance
        self.items_by_category = items_by_category

        # 動的にSelectメニューを生成
        options = []
        for context, categories in items_by_category.items():
            for category, items in categories.items():
                if items:
                    # valueにコンテキストとカテゴリを含める
                    options.append(discord.SelectOption(
                        label=f"{context} - {CATEGORY_MAP[category]['prompt']}", 
                        value=f"{context}|{category}"
                    ))
        
        self.category_select = discord.ui.Select(placeholder="完了する項目が含まれるリストを選択...", options=options, custom_id="category_select")
        self.category_select.callback = self.on_category_select
        self.add_item(self.category_select)

        self.item_select = discord.ui.Select(placeholder="カテゴリを選択すると項目が表示されます", disabled=True, custom_id="item_select")
        self.add_item(self.item_select)

    async def on_category_select(self, interaction: discord.Interaction):
        await interaction.response.defer(update=True)
        context, category = interaction.data['values'][0].split('|')
        items = self.items_by_category.get(context, {}).get(category, [])
        
        if items:
            self.item_select.options = [discord.SelectOption(label=item[:100], value=item) for item in items]
            self.item_select.placeholder = "完了する項目を選択"
            self.item_select.disabled = False
        else:
            self.item_select.placeholder = "このリストに項目はありません"
            self.item_select.disabled = True
        
        await interaction.edit_original_response(view=self)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        context, category = self.category_select.values[0].split('|')
        item_to_remove = self.item_select.values[0]

        success = await self.memo_cog.remove_item_from_list_file(category, item_to_remove, context)
        if success:
            await interaction.followup.send(f"🗑️ 「{item_to_remove}」をリストから完了（削除）しました。", ephemeral=True)
            await self.memo_cog.refresh_all_lists_post(interaction.channel)
        else:
            await interaction.followup.send("❌ 削除中にエラーが発生しました。", ephemeral=True)

class AddToCalendarModal(discord.ui.Modal, title="カレンダーに登録"):
    def __init__(self, memo_cog_instance, tasks: list):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance
        
        self.task_select = discord.ui.Select(placeholder="カレンダーに登録するタスクを選択...", options=[
            discord.SelectOption(label=task[:100], value=task) for task in tasks
        ])
        self.add_item(self.task_select)
        
        self.target_date = discord.ui.TextInput(label="日付 (YYYY-MM-DD形式、空欄で今日)", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected_task = self.task_select.values[0]
        
        target_date_str = self.target_date.value
        try:
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date() if target_date_str else datetime.now(JST).date()
        except ValueError:
            await interaction.followup.send("日付の形式が正しくありません。`YYYY-MM-DD`形式で入力してください。", ephemeral=True)
            return

        calendar_cog = self.memo_cog.bot.get_cog('CalendarCog')
        if not calendar_cog or not calendar_cog.is_ready:
            await interaction.followup.send("カレンダー機能が利用できません。", ephemeral=True)
            return
        
        try:
            # CalendarCogのタスク登録機能を呼び出し
            await calendar_cog.schedule_task_from_memo(selected_task, target_date)
            await interaction.followup.send(f"✅ 「{selected_task}」のカレンダー登録を試みました。", ephemeral=True)
        except Exception as e:
            logging.error(f"カレンダー登録中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 処理中にエラーが発生しました: `{e}`", ephemeral=True)


class ListManagementView(discord.ui.View):
    def __init__(self, memo_cog_instance):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance

    @discord.ui.button(label="項目を追加", style=discord.ButtonStyle.success, emoji="➕")
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddItemModal(self.memo_cog))

    @discord.ui.button(label="項目を完了", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def complete_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = await self.memo_cog.get_all_list_items_structured()
        await interaction.response.send_modal(CompleteItemModal(self.memo_cog, items))

    @discord.ui.button(label="カレンダーに登録", style=discord.ButtonStyle.secondary, emoji="📅")
    async def add_to_calendar(self, interaction: discord.Interaction, button: discord.ui.Button):
        work_tasks = await self.memo_cog.get_list_items("Task", "Work")
        personal_tasks = await self.memo_cog.get_list_items("Task", "Personal")
        all_tasks = work_tasks + personal_tasks
        if not all_tasks:
            await interaction.response.send_message("登録できるタスクがありません。", ephemeral=True, delete_after=10)
            return
        await interaction.response.send_modal(AddToCalendarModal(self.memo_cog, all_tasks))


class MemoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.last_list_message_id = None # メッセージIDを一つだけ保持

        self.dbx = dropbox.Dropbox(
            oauth2_refresh_token=self.dropbox_refresh_token,
            app_key=self.dropbox_app_key,
            app_secret=self.dropbox_app_secret
        )
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
        else:
            self.gemini_model = None

        if TASK_LIST_CHANNEL_ID != 0:
            self.post_all_lists.start()

    def cog_unload(self):
        if self.post_all_lists.is_running():
            self.post_all_lists.cancel()
            
    async def get_all_list_items_structured(self) -> dict:
        """すべてのリスト項目を構造化して取得"""
        all_items = {}
        for context_name, _ in CONTEXT_CHOICES:
            all_items[context_name] = {}
            for category_name, _ in CATEGORY_CHOICES:
                items = await self.get_list_items(category_name, context_name)
                all_items[context_name][category_name] = items
        return all_items


    def _sort_tasks_with_deadline(self, tasks: list) -> list:
        def get_deadline(task_text):
            match = re.search(r'(\d{1,2})/(\d{1,2})$', task_text)
            if match:
                month, day = map(int, match.groups())
                today = datetime.now(JST)
                year = today.year
                if today.month > month:
                    year += 1
                return datetime(year, month, day)
            return datetime(9999, 12, 31)

        return sorted(tasks, key=get_deadline)

    async def get_list_items(self, category: str, context: str) -> list[str]:
        list_info = CATEGORY_MAP.get(category)
        if not list_info: return []
        file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{context}/{list_info['file']}"
        try:
            _, res = self.dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            items = re.findall(r"-\s*\[\s*\]\s*(.+)", content)
            return [item.strip() for item in items]
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return []
            logging.error(f"Dropboxファイルのダウンロードに失敗: {e}")
            return []

    async def add_item_to_list_file(self, category: str, item: str, context: str) -> bool:
        list_info = CATEGORY_MAP.get(category)
        if not list_info: return False
        file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{context}/{list_info['file']}"
        try:
            try:
                _, res = self.dbx.files_download(file_path)
                content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    content = f"# {list_info['prompt']}\n\n"
                else: raise
            line_to_add = f"- [ ] {item}"
            if content.strip().endswith("\n") or not content.strip():
                new_content = content.strip() + "\n" + line_to_add + "\n"
            else:
                new_content = content.strip() + "\n\n" + line_to_add + "\n"
            self.dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
            logging.info(f"{file_path} に '{item}' を追記しました。")
            return True
        except Exception as e:
            logging.error(f"リストファイルへの追記中にエラー: {e}")
            return False
            
    async def remove_item_from_list_file(self, category: str, item_to_remove: str, context: str) -> bool:
        list_info = CATEGORY_MAP.get(category)
        if not list_info: return False
        file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{context}/{list_info['file']}"
        try:
            _, res = self.dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            escaped_item = re.escape(item_to_remove)
            pattern = re.compile(r"(-\s*\[\s*\]\s*)(" + escaped_item + r")", re.MULTILINE)
            new_content, count = pattern.subn(r"- [x] \2", content)
            if count > 0:
                self.dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
                logging.info(f"{file_path} の '{item_to_remove}' を完了済みにしました。")
                return True
            else:
                logging.warning(f"削除対象の項目が見つかりませんでした: {item_to_remove}")
                return False
        except Exception as e:
            logging.error(f"リストファイルの更新中にエラー: {e}")
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return
        if message.reference:
            return
        if self.gemini_model:
            await self.categorize_and_propose_action(message)
    
    # ... (on_raw_reaction_add, categorize_and_propose_action は変更なし) ...

    list_group = app_commands.Group(name="list", description="タスク、アイデアなどのリストを管理します。")
    @list_group.command(name="show", description="指定したカテゴリのリストを表示します。")
    @app_commands.describe(context="どちらのリストを表示しますか？", category="表示したいリストのカテゴリを選択してください。")
    @app_commands.choices(context=CONTEXT_CHOICES, category=CATEGORY_CHOICES)
    async def show_list(self, interaction: discord.Interaction, context: app_commands.Choice[str], category: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        items = await self.get_list_items(category.value, context.value)

        if category.value == "Task":
            items = self._sort_tasks_with_deadline(items)

        embed = discord.Embed(title=f"📋 {context.name}の{CATEGORY_MAP[category.value]['prompt']}", color=discord.Color.blue())
        embed.description = "\n".join([f"- {item}" for item in items]) if items else "このリストにはまだ何もありません。"
        await interaction.followup.send(embed=embed)
        
    @tasks.loop(hours=24)
    async def post_all_lists(self):
        await self.refresh_all_lists_post()

    async def refresh_all_lists_post(self, channel=None):
        """全てのリストを投稿または更新する"""
        if not channel:
            channel = self.bot.get_channel(TASK_LIST_CHANNEL_ID)
        if not channel: 
            logging.warning("リスト投稿用のチャンネルが見つかりません。")
            return
        
        logging.info("全リストの投稿/更新を実行します。")
        
        embed = discord.Embed(title=f"📅 {datetime.now(JST).strftime('%Y-%m-%d')} のリスト一覧", color=discord.Color.orange())
        
        has_items = False
        for context_value, context_name in CONTEXT_CHOICES:
            field_value = ""
            for category_value, category_name in CATEGORY_CHOICES:
                items = await self.get_list_items(category_value, context_value)
                if category_value == "Task":
                    items = self._sort_tasks_with_deadline(items)
                
                if items:
                    has_items = True
                    field_value += f"**{category_name}**\n" + "\n".join([f"- {item}" for item in items]) + "\n\n"
            
            if field_value:
                embed.add_field(name=f"--- {context_name} ---", value=field_value, inline=False)

        if not has_items:
            embed.description = "すべてのリストは現在空です。"
            
        view = ListManagementView(self)

        try:
            if self.last_list_message_id:
                message = await channel.fetch_message(self.last_list_message_id)
                await message.edit(embed=embed, view=view)
            else:
                # 古いメッセージを削除する処理は削除
                message = await channel.send(embed=embed, view=view)
                self.last_list_message_id = message.id
        except (discord.NotFound, discord.Forbidden):
            # メッセージが見つからない場合は新規投稿
            message = await channel.send(embed=embed, view=view)
            self.last_list_message_id = message.id
        except Exception as e:
            logging.error(f"リスト投稿の更新中にエラー: {e}")


    @post_all_lists.before_loop
    async def before_post_task_list(self):
        # 毎朝8時に実行するように調整
        now = datetime.now(JST)
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if next_run < now:
            next_run += timedelta(days=1)
        await discord.utils.sleep_until(next_run)
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(MemoCog(bot))