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

# --- 定数定義 ---
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))
LIST_CHANNEL_ID = int(os.getenv("LIST_CHANNEL_ID", "0"))
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

# --- UIコンポーネント ---

# < AIによる自動提案機能のためのUI >
class ManualAddToListModal(discord.ui.Modal, title="リストに手動で追加"):
    def __init__(self, memo_cog_instance, item_to_add: str):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance
        self.item_to_add = item_to_add

    context = discord.ui.TextInput(label="コンテキスト", placeholder="Work または Personal")
    category = discord.ui.TextInput(label="カテゴリ", placeholder="Task, Idea, Shopping, Bookmark")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        context_val = self.context.value.strip().capitalize()
        category_val = self.category.value.strip().capitalize()

        if context_val in ["Work", "Personal"] and category_val in CATEGORY_MAP:
            success = await self.memo_cog.add_item_to_list_file(category_val, self.item_to_add, context_val)
            if success:
                await interaction.followup.send(f"✅ **{context_val}** の **{CATEGORY_MAP[category_val]['prompt']}** に「{self.item_to_add}」を追加しました。", ephemeral=True)
            else:
                await interaction.followup.send("❌リストへの追加中にエラーが発生しました。", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ 不正なコンテキストまたはカテゴリです。", ephemeral=True)

class AddToListView(discord.ui.View):
    def __init__(self, memo_cog_instance, message: discord.Message, category: str, item_to_add: str, context: str):
        super().__init__(timeout=60.0)
        self.memo_cog = memo_cog_instance
        self.message = message
        self.category = category
        self.item_to_add = item_to_add
        self.context = context
        self.reply_message = None

    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        success = await self.memo_cog.add_item_to_list_file(self.category, self.item_to_add, self.context)

        if success:
            await self.reply_message.edit(
                content=f"✅ **{self.context}** の **{CATEGORY_MAP[self.category]['prompt']}** に「{self.item_to_add}」を追加しました。",
                view=None
            )
        else:
            await self.reply_message.edit(content="❌リストへの追加中にエラーが発生しました。", view=None)
        
        await asyncio.sleep(10)
        await self.reply_message.delete()
        self.stop()

    @discord.ui.button(label="いいえ (手動選択)", style=discord.ButtonStyle.secondary)
    async def cancel_and_select(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.reply_message.edit(content="手動で追加先を選択してください...", view=None)
        await interaction.response.send_modal(ManualAddToListModal(self.memo_cog, self.item_to_add))
        self.stop()

    @discord.ui.button(label="メモのみ", style=discord.ButtonStyle.danger, row=1)
    async def memo_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.reply_message.edit(content="📝 メモのみ記録しました。", view=None)
        await asyncio.sleep(10)
        await self.reply_message.delete()
        self.stop()

    async def on_timeout(self):
        if self.reply_message:
            try:
                await self.reply_message.edit(content="タイムアウトしたため、メモのみ記録しました。", view=None)
                await asyncio.sleep(10)
                await self.reply_message.delete()
            except discord.NotFound:
                pass
        self.stop()

# < リスト管理用の新しいUI >
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

        options = []
        for context, categories in items_by_category.items():
            for category, items in categories.items():
                if items:
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
        self.last_list_message_id = None

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

        if LIST_CHANNEL_ID != 0:
            self.post_all_lists.start()

    def cog_unload(self):
        if self.post_all_lists.is_running():
            self.post_all_lists.cancel()

    async def get_all_list_items_structured(self) -> dict:
        all_items = {}
        for choice in CONTEXT_CHOICES:
            context_value = choice.value
            all_items[context_value] = {}
            for cat_choice in CATEGORY_CHOICES:
                category_value = cat_choice.value
                items = await self.get_list_items(category_value, context_value)
                all_items[context_value][category_value] = items
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
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != MEMO_CHANNEL_ID or str(payload.emoji) != ADD_TO_LIST_EMOJI:
            return
        if payload.user_id == self.bot.user.id:
            return
        try:
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if message.author.bot or not message.content:
                return
            await self.categorize_and_propose_action(message)
            user = self.bot.get_user(payload.user_id)
            await message.remove_reaction(payload.emoji, user)
        except (discord.NotFound, discord.Forbidden):
            pass
        except Exception as e:
            logging.error(f"リアクションによるリスト追加処理中にエラー: {e}", exc_info=True)

    async def categorize_and_propose_action(self, message: discord.Message):
        prompt = f"""
        以下のメモの内容を分析し、「コンテキスト」「カテゴリ」「リストに追加すべき項目名」を判断してください。
        # 指示
        1.  **コンテキスト**: このメモが「Work（仕事）」に関するものか、「Personal（プライベート）」に関するものか判断してください。
        2.  **カテゴリ**: 最も適切なカテゴリを一つだけ選んでください。 (Task, Idea, Shopping, Bookmark, Other)
        3.  **項目名**: リストに追加するのに最適な短い名詞または動詞句にしてください。
        # 出力形式 (JSONのみ)
        ```json
        {{"context": "Work" or "Personal", "category": "選んだカテゴリ名", "item": "リストに追加すべき具体的な項目名"}}
        ```
        ---
        メモ: {message.content}
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            json_match = re.search(r'```json\n(\{.*?\})\n```', response.text, re.DOTALL)
            json_text = json_match.group(1) if json_match else response.text
            result_json = json.loads(json_text)
            context = result_json.get("context")
            category = result_json.get("category")
            item = result_json.get("item")
            if context in ["Work", "Personal"] and category in CATEGORY_MAP and item:
                prompt_text = CATEGORY_MAP[category]['prompt']
                view = AddToListView(self, message, category, item, context)
                reply_message = await message.reply(f"このメモを **{context}** の **{prompt_text}** に追加しますか？\n`{item}`", view=view)
                view.reply_message = reply_message
        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            logging.warning(f"メモの分類結果の解析に失敗: {e}\nAI Response: {getattr(response, 'text', 'N/A')}")
        except Exception as e:
            logging.error(f"メモの分類中に予期せぬエラー: {e}", exc_info=True)

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
        if not channel:
            channel = self.bot.get_channel(LIST_CHANNEL_ID)
        if not channel: 
            logging.warning("リスト投稿用のチャンネルが見つかりません。")
            return
        
        logging.info("全リストの投稿/更新を実行します。")
        
        embed = discord.Embed(title=f"📅 {datetime.now(JST).strftime('%Y-%m-%d')} のリスト一覧", color=discord.Color.orange())
        
        has_items = False
        for choice in CONTEXT_CHOICES:
            context_value = choice.value
            context_name = choice.name
            field_value = ""
            for cat_choice in CATEGORY_CHOICES:
                category_value = cat_choice.value
                category_name = cat_choice.name
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
                message = await channel.send(embed=embed, view=view)
                self.last_list_message_id = message.id
        except (discord.NotFound, discord.Forbidden):
            message = await channel.send(embed=embed, view=view)
            self.last_list_message_id = message.id
        except Exception as e:
            logging.error(f"リスト投稿の更新中にエラー: {e}")


    @post_all_lists.before_loop
    async def before_post_task_list(self):
        now = datetime.now(JST)
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if next_run < now:
            next_run += timedelta(days=1)
        await discord.utils.sleep_until(next_run)
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(MemoCog(bot))