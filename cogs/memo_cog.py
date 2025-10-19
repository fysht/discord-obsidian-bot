import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
from datetime import datetime, timezone, timedelta, time
import json
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import asyncio

# --- 定数定義 ---
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))
LIST_CHANNEL_ID = int(os.getenv("LIST_CHANNEL_ID", 0))
try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
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

# --- obsidian_handler をインポート ---
try:
    from obsidian_handler import add_memo_async
except ImportError:
    logging.error("obsidian_handler.pyが見つかりません。メモ保存機能が無効になります。")
    async def add_memo_async(*args, **kwargs):
        logging.error("obsidian_handler is not available.")
        return

# --- UIコンポーネント ---
class ManualAddToListModal(discord.ui.Modal, title="リストに手動で追加"):
    def __init__(self, memo_cog_instance, item_to_add: str):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance; self.item_to_add = item_to_add
        self.context = discord.ui.TextInput(label="コンテキスト", placeholder="Work または Personal")
        self.category = discord.ui.TextInput(label="カテゴリ", placeholder="Task, Idea, Shopping, Bookmark")
        self.add_item(self.context); self.add_item(self.category) # add_item忘れ修正
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True); context_val = self.context.value.strip().capitalize(); category_val = self.category.value.strip().capitalize()
        if context_val in ["Work", "Personal"] and category_val in CATEGORY_MAP:
            success = await self.memo_cog.add_item_to_list_file(category_val, self.item_to_add, context_val)
            if success: await interaction.followup.send(f"✅ 追加成功", ephemeral=True)
            else: await interaction.followup.send("❌追加エラー", ephemeral=True)
        else: await interaction.followup.send("⚠️ 不正入力", ephemeral=True)

class AddToListView(discord.ui.View):
    def __init__(self, memo_cog_instance, message: discord.Message, category: str, item_to_add: str, context: str):
        super().__init__(timeout=60.0); self.memo_cog = memo_cog_instance; self.message = message; self.category = category; self.item_to_add = item_to_add; self.context = context; self.reply_message = None
    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(); success = await self.memo_cog.add_item_to_list_file(self.category, self.item_to_add, self.context)
        if self.reply_message:
            try:
                if success: await self.reply_message.edit(content=f"✅ 追加成功", view=None)
                else: await self.reply_message.edit(content="❌追加エラー", view=None)
                await asyncio.sleep(10); await self.reply_message.delete()
            except discord.HTTPException as e: logging.error(f"Failed edit/delete reply: {e}")
        self.stop()
    @discord.ui.button(label="いいえ (手動選択)", style=discord.ButtonStyle.secondary)
    async def cancel_and_select(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.reply_message: try: await self.reply_message.edit(content="手動選択中...", view=None)
                                except discord.HTTPException as e: logging.warning(f"Failed edit reply: {e}")
        await interaction.response.send_modal(ManualAddToListModal(self.memo_cog, self.item_to_add)); self.stop()
    @discord.ui.button(label="メモのみ", style=discord.ButtonStyle.danger, row=1)
    async def memo_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.reply_message: try: await self.reply_message.edit(content="📝 メモのみ記録", view=None); await asyncio.sleep(10); await self.reply_message.delete()
                                except discord.HTTPException as e: logging.error(f"Failed edit/delete reply (memo_only): {e}")
        self.stop()
    async def on_timeout(self):
        logging.info(f"AddToListView timed out for message {self.message.id}")
        if self.reply_message: try: await self.reply_message.edit(content="⏳ タイムアウト. メモのみ記録.", view=None); await asyncio.sleep(10); await self.reply_message.delete()
                                except discord.HTTPException as e: logging.warning(f"Failed edit/delete reply on timeout: {e}")
        self.stop()

class AddItemModal(discord.ui.Modal, title="リストに項目を追加"):
    def __init__(self, memo_cog_instance):
        super().__init__(timeout=None); self.memo_cog = memo_cog_instance
        self.context_input = discord.ui.TextInput(label="コンテキスト", placeholder="Work または Personal", required=True, max_length=10)
        self.category_input = discord.ui.TextInput(label="カテゴリ", placeholder="Task, Idea, Shopping, Bookmark", required=True, max_length=10)
        self.item_to_add = discord.ui.TextInput(label="追加する項目名", placeholder="例: 新しいプロジェクトの企画書を作成する", style=discord.TextStyle.short, required=True)
        self.add_item(self.context_input); self.add_item(self.category_input); self.add_item(self.item_to_add)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True); context = self.context_input.value.strip().capitalize(); category = self.category_input.value.strip().capitalize(); item = self.item_to_add.value.strip()
        if context not in ["Work", "Personal"]: await interaction.followup.send("⚠️ コンテキスト不正", ephemeral=True); return
        if category not in CATEGORY_MAP: await interaction.followup.send(f"⚠️ カテゴリ不正", ephemeral=True); return
        if not item: await interaction.followup.send("⚠️ 項目名空欄", ephemeral=True); return
        success = await self.memo_cog.add_item_to_list_file(category, item, context)
        if success: await interaction.followup.send(f"✅ 追加成功", ephemeral=True); list_channel = self.memo_cog.bot.get_channel(LIST_CHANNEL_ID);
                 if list_channel: await self.memo_cog.refresh_all_lists_post(list_channel)
                 else: logging.warning(f"List channel {LIST_CHANNEL_ID} not found.")
        else: await interaction.followup.send("❌追加エラー", ephemeral=True)

class CompleteItemModal(discord.ui.Modal, title="リストの項目を完了"):
    def __init__(self, memo_cog_instance, items_by_category: dict):
        super().__init__(timeout=None); self.memo_cog = memo_cog_instance; self.items_by_category = items_by_category; options = []
        if items_by_category:
            for context, categories in items_by_category.items():
                for category, items in categories.items():
                    if items: options.append(discord.SelectOption(label=f"{context} - {CATEGORY_MAP[category]['prompt']}", value=f"{context}|{category}"))
        placeholder = "完了する項目が含まれるリストを選択..." if options else "完了できる項目なし"; self.category_select = discord.ui.Select(placeholder=placeholder, options=options if options else [discord.SelectOption(label="項目なし", value="no_items")], custom_id="category_select", disabled=not options)
        self.category_select.callback = self.on_category_select; self.add_item(self.category_select)
        self.item_select = discord.ui.Select(placeholder="カテゴリ選択後に項目表示", disabled=True, custom_id="item_select"); self.add_item(self.item_select)
    async def on_category_select(self, interaction: discord.Interaction):
        if not interaction.data or 'values' not in interaction.data or not interaction.data['values']: logging.warning("on_category_select: No values"); await interaction.response.send_message("カテゴリ未選択", ephemeral=True, delete_after=5); return
        selected_value = interaction.data['values'][0]
        if selected_value == "no_items": await interaction.response.defer(update=True); return
        await interaction.response.defer(update=True); context, category = selected_value.split('|'); items = self.items_by_category.get(context, {}).get(category, [])
        if items: options_for_select = [discord.SelectOption(label=item[:100], value=item) for item in items][:25]; self.item_select.options = options_for_select; self.item_select.placeholder = "完了する項目を選択"; self.item_select.disabled = False
        else: self.item_select.options = []; self.item_select.placeholder = "項目なし"; self.item_select.disabled = True
        try: await interaction.edit_original_response(view=self)
        except discord.HTTPException as e: logging.error(f"Failed edit modal view: {e}")
    async def on_submit(self, interaction: discord.Interaction):
        if not self.category_select.values or not self.item_select.values: await interaction.response.send_message("カテゴリと項目未選択", ephemeral=True, delete_after=10); return
        await interaction.response.defer(ephemeral=True, thinking=True); context, category = self.category_select.values[0].split('|'); item_to_remove = self.item_select.values[0]
        success = await self.memo_cog.remove_item_from_list_file(category, item_to_remove, context)
        if success: await interaction.followup.send(f"🗑️ 「{item_to_remove}」完了", ephemeral=True); list_channel = self.memo_cog.bot.get_channel(LIST_CHANNEL_ID);
                  if list_channel: await self.memo_cog.refresh_all_lists_post(list_channel)
                  else: logging.warning(f"List channel {LIST_CHANNEL_ID} not found.")
        else: await interaction.followup.send("❌完了処理エラー", ephemeral=True)

class AddToCalendarModal(discord.ui.Modal, title="カレンダーに登録"):
    def __init__(self, memo_cog_instance, tasks: list):
        super().__init__(timeout=None); self.memo_cog = memo_cog_instance
        if tasks: options = [discord.SelectOption(label=task[:100], value=task) for task in tasks][:25]; placeholder = "登録タスク選択..."; disabled = False
        else: options = [discord.SelectOption(label="タスクなし", value="no_task")]; placeholder = "登録できるタスクなし"; disabled = True
        self.task_select = discord.ui.Select(placeholder=placeholder, options=options, disabled=disabled); self.add_item(self.task_select)
        self.target_date = discord.ui.TextInput(label="日付 (YYYY-MM-DD, 空欄=今日)", required=False, placeholder=datetime.now(JST).strftime('%Y-%m-%d')); self.add_item(self.target_date)
    async def on_submit(self, interaction: discord.Interaction):
        if not self.task_select.values or self.task_select.values[0] == "no_task": await interaction.response.send_message("タスク未選択", ephemeral=True, delete_after=10); return
        await interaction.response.defer(ephemeral=True, thinking=True); selected_task = self.task_select.values[0]; target_date_str = self.target_date.value.strip(); target_date_obj = datetime.now(JST).date()
        if target_date_str: try: target_date_obj = datetime.strptime(target_date_str, '%Y-%m-%d').date()
                            except ValueError: await interaction.followup.send("⚠️ 日付形式不正", ephemeral=True); return
        journal_cog = self.memo_cog.bot.get_cog('JournalCog')
        if not journal_cog or not journal_cog.is_ready or not journal_cog.calendar_service: await interaction.followup.send("⚠️ カレンダー機能利用不可", ephemeral=True); return
        try: event_body = {'summary': selected_task, 'start': {'date': target_date_obj.isoformat()}, 'end': {'date': target_date_obj.isoformat()}}; await asyncio.to_thread(journal_cog.calendar_service.events().insert(calendarId=journal_cog.google_calendar_id, body=event_body).execute); await interaction.followup.send(f"✅ 「{selected_task}」を {target_date_obj.strftime('%Y-%m-%d')} の終日予定として登録", ephemeral=True)
        except Exception as e: logging.error(f"カレンダー登録エラー ({selected_task}): {e}", exc_info=True); await interaction.followup.send(f"❌ カレンダー登録エラー: `{e}`", ephemeral=True)

class ListManagementView(discord.ui.View):
    def __init__(self, memo_cog_instance):
        super().__init__(timeout=None); self.memo_cog = memo_cog_instance; self.add_item_button.custom_id = "list_mgmt_add_item"; self.complete_item_button.custom_id = "list_mgmt_complete_item"; self.add_to_calendar_button.custom_id = "list_mgmt_add_to_calendar"
    @discord.ui.button(label="項目を追加", style=discord.ButtonStyle.success, emoji="➕", custom_id="list_mgmt_add_item")
    async def add_item_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.memo_cog.dbx_available: await interaction.response.send_message("⚠️ Dropbox利用不可", ephemeral=True); return
        await interaction.response.send_modal(AddItemModal(self.memo_cog))
    @discord.ui.button(label="項目を完了", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="list_mgmt_complete_item")
    async def complete_item_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.memo_cog.dbx_available: await interaction.response.send_message("⚠️ Dropbox利用不可", ephemeral=True); return
        items = await self.memo_cog.get_all_list_items_structured(); await interaction.response.send_modal(CompleteItemModal(self.memo_cog, items))
    @discord.ui.button(label="カレンダーに登録", style=discord.ButtonStyle.secondary, emoji="📅", custom_id="list_mgmt_add_to_calendar")
    async def add_to_calendar_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.memo_cog.dbx_available: await interaction.response.send_message("⚠️ Dropbox利用不可", ephemeral=True); return
        work_tasks = await self.memo_cog.get_list_items("Task", "Work"); personal_tasks = await self.memo_cog.get_list_items("Task", "Personal"); all_tasks = work_tasks + personal_tasks
        if not all_tasks: await interaction.response.send_message("登録できるタスクなし", ephemeral=True, delete_after=10); return
        await interaction.response.send_modal(AddToCalendarModal(self.memo_cog, all_tasks))

# === Cog 本体 ===
class MemoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.last_list_message_id = None
        try:
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret, timeout=60)
            self.dbx.users_get_current_account(); logging.info("MemoCog: Dropbox client initialized."); self.dbx_available = True
        except Exception as e: logging.error(f"MemoCog: Dropbox init failed: {e}"); self.dbx = None; self.dbx_available = False
        if self.gemini_api_key:
            try: genai.configure(api_key=self.gemini_api_key); self.gemini_model = genai.GenerativeModel("gemini-1.5-flash"); logging.info("MemoCog: Gemini model initialized."); self.gemini_available = True
            except Exception as e: logging.error(f"MemoCog: Gemini init failed: {e}"); self.gemini_model = None; self.gemini_available = False
        else: self.gemini_model = None; self.gemini_available = False; logging.warning("MemoCog: GEMINI_API_KEY not set.")
        if LIST_CHANNEL_ID != 0 and self.dbx_available: self.post_all_lists.start()
        elif LIST_CHANNEL_ID != 0: logging.warning("MemoCog: Dropbox unavailable. List posting disabled.")

    def cog_unload(self):
        if hasattr(self, 'post_all_lists') and self.post_all_lists.is_running(): self.post_all_lists.cancel()

    async def get_all_list_items_structured(self) -> dict:
        if not self.dbx_available: return {}
        all_items = {};
        for choice in CONTEXT_CHOICES: context_value = choice.value; all_items[context_value] = {};
            for cat_choice in CATEGORY_CHOICES: category_value = cat_choice.value; items = await self.get_list_items(category_value, context_value); all_items[context_value][category_value] = items
        return all_items

    def _sort_tasks_with_deadline(self, tasks: list) -> list:
        def get_deadline(task_text):
            match = re.search(r'(\d{1,2})/(\d{1,2})(?!\d)', task_text)
            if match:
                try: month, day = map(int, match.groups()); today = datetime.now(JST); year = today.year; deadline_date = datetime(year, month, day);
                     if deadline_date.date() < today.date(): year += 1; return JST.localize(datetime(year, month, day))
                except ValueError: return JST.localize(datetime(9999, 12, 31))
            return JST.localize(datetime(9999, 12, 31))
        try: return sorted(tasks, key=get_deadline)
        except Exception as e: logging.error(f"Error sorting tasks: {e}"); return tasks

    async def get_list_items(self, category: str, context: str) -> list[str]:
        if not self.dbx_available: return []
        list_info = CATEGORY_MAP.get(category); file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{context}/{list_info['file']}"
        if not list_info: return []
        try: _, res = await asyncio.to_thread(self.dbx.files_download, file_path); content = res.content.decode('utf-8'); items = re.findall(r"-\s*\[\s*\]\s*(.+)", content); return [item.strip() for item in items]
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): return []
            logging.error(f"Dropbox download failed ({file_path}): {e}"); return []
        except Exception as e: logging.error(f"Error reading list file {file_path}: {e}"); return []

    async def add_item_to_list_file(self, category: str, item: str, context: str) -> bool:
        if not self.dbx_available: return False
        list_info = CATEGORY_MAP.get(category); file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{context}/{list_info['file']}"
        if not list_info: return False
        try:
            content = "";
            try: _, res = await asyncio.to_thread(self.dbx.files_download, file_path); content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): content = f"# {list_info['prompt']}\n\n"; logging.info(f"Creating new list file: {file_path}")
                else: logging.error(f"Dropbox download error ({file_path}): {e}"); raise
            line_to_add = f"- [ ] {item}";
            if content.strip().endswith("\n") or not content.strip(): new_content = content.strip() + "\n" + line_to_add + "\n"
            else: new_content = content.strip() + "\n\n" + line_to_add + "\n"
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
            logging.info(f"{file_path}に'{item}'を追記"); return True
        except Exception as e: logging.error(f"リスト追記エラー ({file_path}): {e}", exc_info=True); return False

    async def remove_item_from_list_file(self, category: str, item_to_remove: str, context: str) -> bool:
        if not self.dbx_available: return False
        list_info = CATEGORY_MAP.get(category); file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{context}/{list_info['file']}"
        if not list_info: return False
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, file_path); content = res.content.decode('utf-8')
            escaped_item = re.escape(item_to_remove.strip()); pattern = re.compile(r"^(\s*-\s*\[\s*\]\s*)(" + escaped_item + r")\s*$", re.MULTILINE)
            replacement = r"\1[x] \2"; new_content, count = pattern.subn(replacement, content)
            if count > 0:
                await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
                logging.info(f"{file_path}の'{item_to_remove}'を完了済みに"); return True
            else: logging.warning(f"完了対象項目が見つかりません ({file_path}): '{item_to_remove}'"); return False
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): logging.warning(f"リストファイルが見つかりません ({file_path})"); return False
            else: logging.error(f"Dropbox APIエラー ({file_path}): {e}", exc_info=True); return False
        except Exception as e: logging.error(f"リスト更新エラー ({file_path}): {e}", exc_info=True); return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID: return
        if message.reference or message.interaction: return
        if message.attachments and not message.content:
             is_voice = any(att.content_type.startswith('audio/') for att in message.attachments)
             is_image = any(att.content_type.startswith('image/') for att in message.attachments)
             if is_voice or is_image: return
        content = message.content.strip();
        if not content: return
        logging.info(f"Received text message (ID: {message.id}): {content[:50]}...")
        try:
            await add_memo_async(content=message.content, author=f"{message.author} ({message.author.id})", created_at=message.created_at.isoformat(), message_id=message.id)
            await message.add_reaction("📄"); logging.info(f"Text memo saved (ID: {message.id})")
            if self.gemini_available: await self.categorize_and_propose_action(message)
            else: logging.info(f"Skipping AI categorization for message {message.id}")
        except Exception as e:
            logging.error(f"Error processing text memo {message.id}: {e}", exc_info=True)
            try: bot_user = self.bot.user;
                 if bot_user: await message.remove_reaction("📄", bot_user)
                 await message.add_reaction("❌")
            except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != MEMO_CHANNEL_ID or str(payload.emoji) != ADD_TO_LIST_EMOJI: return
        if payload.user_id == self.bot.user.id: return
        channel = self.bot.get_channel(payload.channel_id);
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
            content = message.content.strip()
            if message.author.bot or not content:
                user = await self.bot.fetch_user(payload.user_id);
                if user: await message.remove_reaction(payload.emoji, user); return
            logging.info(f"Manual categorization triggered for message: {message.id}")
            if self.gemini_available: await self.categorize_and_propose_action(message)
            else: await channel.send("⚠️ AI分類機能利用不可", delete_after=10)
            user = await self.bot.fetch_user(payload.user_id)
            if user: await message.remove_reaction(payload.emoji, user)
        except discord.NotFound: logging.warning(f"Reaction target message not found: {payload.message_id}")
        except discord.Forbidden: logging.warning("Permissions error on reaction.")
        except Exception as e: logging.error(f"Error handling reaction: {e}", exc_info=True)

    async def categorize_and_propose_action(self, message: discord.Message):
        if not self.gemini_available: return
        prompt = f"""以下メモ分析指示(JSON出力):
        1. Context: "Work" or "Personal".
        2. Category: "Task", "Idea", "Shopping", "Bookmark", "Other". If unsure, use "Other".
        3. Item: Short noun/verb phrase for list if Category is not "Other". Empty string ("") if Category is "Other".
        Output Format (JSON only): ```json{{"context": "...", "category": "...", "item": "..."}}```
        ---
        Memo: {message.content}"""
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', response.text, re.DOTALL)
            if not json_match: json_match = re.search(r'(\{.*?\})', response.text, re.DOTALL)
            if not json_match: logging.warning(f"No JSON in classification response: {response.text}"); return
            result_json = json.loads(json_match.group(1)); context = result_json.get("context"); category = result_json.get("category"); item = result_json.get("item", "").strip()
            if context in ["Work", "Personal"] and category in CATEGORY_MAP:
                if not item and category != "Other": logging.warning(f"AI invalid item for '{category}': {message.content}"); return
                if category != "Other": prompt_text = CATEGORY_MAP[category]['prompt']; view = AddToListView(self, message, category, item, context); reply = await message.reply(f"リスト追加提案: **{context}**の**{prompt_text}**に「`{item}`」を追加しますか？", view=view, mention_author=False); view.reply_message = reply
            elif category == "Other": logging.info(f"Memo classified as 'Other': {message.content}")
            else: logging.warning(f"AI classification invalid context/category: {result_json}")
        except json.JSONDecodeError as e: logging.warning(f"JSON parsing failed: {e}\nResponse: {getattr(response, 'text', 'N/A')}")
        except Exception as e: logging.error(f"Unexpected error during categorization: {e}", exc_info=True)

    # --- list_group コマンド群 ---
    list_group = app_commands.Group(name="list", description="タスク、アイデアなどのリストを管理します。")
    @list_group.command(name="show", description="指定したカテゴリのリストを表示します。")
    @app_commands.describe(context="リストのコンテキスト", category="表示したいリストのカテゴリ")
    @app_commands.choices(context=CONTEXT_CHOICES, category=CATEGORY_CHOICES)
    async def show_list(self, interaction: discord.Interaction, context: app_commands.Choice[str], category: app_commands.Choice[str]):
        if not self.dbx_available: await interaction.response.send_message("⚠️ Dropbox利用不可", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        items = await self.get_list_items(category.value, context.value)
        if category.value == "Task": items = self._sort_tasks_with_deadline(items)
        embed = discord.Embed(title=f"📋 {context.name}の{CATEGORY_MAP[category.value]['prompt']}", color=discord.Color.blue())
        if items: desc = "\n".join([f"- {item}" for item in items]); embed.description = desc[:4000] + "..." if len(desc) > 4096 else desc
        else: embed.description = "このリストにはまだ何もありません。"
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- 定期実行タスク ---
    @tasks.loop(hours=24)
    async def post_all_lists(self):
        if not self.dbx_available: logging.warning("post_all_lists: Dropbox unavailable."); return
        await self.refresh_all_lists_post()

    async def refresh_all_lists_post(self, channel=None):
        if not self.dbx_available: logging.warning("refresh_all_lists_post: Dropbox unavailable."); return
        if not channel: channel = self.bot.get_channel(LIST_CHANNEL_ID)
        if not channel: logging.warning(f"リスト投稿チャンネル(ID: {LIST_CHANNEL_ID})が見つかりません。"); return
        logging.info(f"Refreshing list post in channel {channel.name} ({channel.id})")
        embed = discord.Embed(title=f"📅 {datetime.now(JST).strftime('%Y-%m-%d')} のリスト一覧", color=discord.Color.orange(), timestamp=datetime.now(JST))
        has_items = False; all_items_structured = await self.get_all_list_items_structured()
        if not all_items_structured and self.dbx_available: embed.description = "リスト情報取得失敗"; logging.error("Failed get structured list items.")
        elif not all_items_structured and not self.dbx_available: embed.description = "Dropbox接続エラー"
        else:
            for context_value, categories in all_items_structured.items():
                context_name = next((c.name for c in CONTEXT_CHOICES if c.value == context_value), context_value)
                field_value = ""; field_has_content = False
                for cat_choice in CATEGORY_CHOICES:
                    category_value = cat_choice.value; category_name = cat_choice.name; items = categories.get(category_value, [])
                    if category_value == "Task": items = self._sort_tasks_with_deadline(items)
                    if items:
                        has_items = True; field_has_content = True
                        current_category_text = f"**{category_name}**\n" + "\n".join([f"- {item}" for item in items]) + "\n\n"
                        if len(field_value) + len(current_category_text) > 1020:
                            if field_value: embed.add_field(name=f"--- {context_name} (続き) ---", value=field_value.strip(), inline=False)
                            field_value = current_category_text[:1020] + "..." if len(current_category_text) > 1024 else current_category_text
                        else: field_value += current_category_text
                # --- field_has_content の条件とデフォルト値 ---
                # field_value が空でも、コンテキスト名自体は表示したい場合がある
                # field_has_content はそのコンテキストに何かしら項目があったかを示す
                # value は空の場合 '項目なし' とする
                embed.add_field(name=f"--- {context_name} ---", value=field_value.strip() if field_value else "項目なし", inline=False)
                # --- ここまで修正 ---
        if not has_items and all_items_structured: embed.description = "すべてのリストは現在空です。"
        view = ListManagementView(self)
        try:
            last_message = None
            async for msg in channel.history(limit=1):
                 last_message = msg
            if self.last_list_message_id and last_message and last_message.id == self.last_list_message_id:
                message = await channel.fetch_message(self.last_list_message_id)
                await message.edit(embed=embed, view=view); logging.info(f"List post edited (ID: {self.last_list_message_id})")
            else:
                deleted_count = 0; async for old_msg in channel.history(limit=50):
                    if old_msg.author == self.bot.user and old_msg.embeds and "リスト一覧" in old_msg.embeds[0].title:
                        try: await old_msg.delete(); deleted_count += 1; await asyncio.sleep(1)
                        except discord.HTTPException: pass
                if deleted_count > 0: logging.info(f"{deleted_count} old list posts deleted.")
                message = await channel.send(embed=embed, view=view); self.last_list_message_id = message.id
                logging.info(f"New list post created (ID: {self.last_list_message_id})")
        except (discord.NotFound, discord.Forbidden) as e:
            logging.warning(f"List post edit/delete failed ({e}). Attempting new post.")
            try:
                async for old_msg in channel.history(limit=100):
                     if old_msg.author == self.bot.user and old_msg.embeds and "リスト一覧" in old_msg.embeds[0].title:
                         try: await old_msg.delete(); await asyncio.sleep(1)
                         except discord.HTTPException: pass
                message = await channel.send(embed=embed, view=view); self.last_list_message_id = message.id
                logging.info(f"New list post created after error (ID: {self.last_list_message_id})")
            except discord.HTTPException as send_e: logging.error(f"Failed to create new list post: {send_e}"); self.last_list_message_id = None
        except Exception as e: logging.error(f"Unexpected error during list post refresh: {e}", exc_info=True); self.last_list_message_id = None

    @post_all_lists.before_loop
    async def before_post_task_list(self):
        """ループ開始前にBotの準備と初回実行時間を待つ"""
        await self.bot.wait_until_ready()
        logging.info("MemoCog: Waiting for post_all_lists loop.")
        now = datetime.now(JST); target_time = time(hour=8, minute=0, tzinfo=JST) # 朝8時
        target_dt = datetime.combine(now.date(), target_time)
        if target_dt < now: target_dt += timedelta(days=1)
        wait_seconds = (target_dt - now).total_seconds()
        logging.info(f"MemoCog: Waiting {wait_seconds:.2f} seconds for first run of post_all_lists at {target_dt}.")
        await asyncio.sleep(wait_seconds)

# --- setup 関数 ---
async def setup(bot):
    # MEMO_CHANNEL_IDのチェックを追加
    if MEMO_CHANNEL_ID == 0:
        logging.error("MEMO_CHANNEL_IDが設定されていません。MemoCogをロードしません。")
        return
    # MemoCogインスタンスを作成して追加
    await bot.add_cog(MemoCog(bot))