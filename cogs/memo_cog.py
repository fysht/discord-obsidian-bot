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

# --- 他のCogのインポートは不要 ---

# --- 定数定義 ---
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))
LIST_CHANNEL_ID = int(os.getenv("LIST_CHANNEL_ID", 0))
# JSTをzoneinfoで定義
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
        super().__init__(timeout=60.0) # タイムアウトを60秒に設定
        self.memo_cog = memo_cog_instance
        self.message = message
        self.category = category
        self.item_to_add = item_to_add
        self.context = context
        self.reply_message = None

    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer() # thinking=False
        success = await self.memo_cog.add_item_to_list_file(self.category, self.item_to_add, self.context)

        # 応答メッセージが存在し、削除されていないか確認
        if self.reply_message:
            try:
                if success:
                    await self.reply_message.edit(
                        content=f"✅ **{self.context}** の **{CATEGORY_MAP[self.category]['prompt']}** に「{self.item_to_add}」を追加しました。",
                        view=None # Viewを削除
                    )
                else:
                    await self.reply_message.edit(content="❌リストへの追加中にエラーが発生しました。", view=None)

                await asyncio.sleep(10) # 10秒表示
                await self.reply_message.delete()
            except discord.NotFound:
                logging.warning(f"Reply message {self.reply_message.id} not found for editing/deleting.")
            except discord.HTTPException as e:
                 logging.error(f"Failed to edit/delete reply message {self.reply_message.id}: {e}")

        self.stop() # Viewを停止

    @discord.ui.button(label="いいえ (手動選択)", style=discord.ButtonStyle.secondary)
    async def cancel_and_select(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 応答メッセージが存在し、削除されていないか確認
        if self.reply_message:
             try:
                 await self.reply_message.edit(content="手動で追加先を選択してください...", view=None)
             except discord.HTTPException as e:
                  logging.warning(f"Failed to edit reply message before manual modal: {e}")
        await interaction.response.send_modal(ManualAddToListModal(self.memo_cog, self.item_to_add))
        self.stop()

    @discord.ui.button(label="メモのみ", style=discord.ButtonStyle.danger, row=1)
    async def memo_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.reply_message:
            try:
                await self.reply_message.edit(content="📝 メモのみ記録しました。", view=None)
                await asyncio.sleep(10)
                await self.reply_message.delete()
            except discord.NotFound:
                logging.warning(f"Reply message {self.reply_message.id} not found for editing/deleting (memo_only).")
            except discord.HTTPException as e:
                 logging.error(f"Failed to edit/delete reply message {self.reply_message.id} (memo_only): {e}")
        self.stop()

    async def on_timeout(self):
        logging.info(f"AddToListView timed out for message {self.message.id}")
        if self.reply_message:
            try:
                # タイムアウトメッセージを表示してから削除
                await self.reply_message.edit(content="⏳ 応答がなかったため、メモのみ記録しました。", view=None)
                await asyncio.sleep(10)
                await self.reply_message.delete()
            except discord.NotFound:
                pass # すでに削除されている場合は何もしない
            except discord.HTTPException as e:
                 logging.warning(f"Failed to edit/delete reply message on timeout: {e}")
        self.stop() # Viewを停止


class AddItemModal(discord.ui.Modal, title="リストに項目を追加"):
    def __init__(self, memo_cog_instance):
        super().__init__(timeout=None) # タイムアウトなし
        self.memo_cog = memo_cog_instance

        self.context_input = discord.ui.TextInput(
            label="コンテキスト", placeholder="Work または Personal", required=True, max_length=10
        )
        self.category_input = discord.ui.TextInput(
            label="カテゴリ", placeholder="Task, Idea, Shopping, Bookmark", required=True, max_length=10
        )
        self.item_to_add = discord.ui.TextInput(
            label="追加する項目名", placeholder="例: 新しいプロジェクトの企画書を作成する", style=discord.TextStyle.short, required=True
        )
        self.add_item(self.context_input)
        self.add_item(self.category_input)
        self.add_item(self.item_to_add)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True) # thinking=True
        context = self.context_input.value.strip().capitalize()
        category = self.category_input.value.strip().capitalize()
        item = self.item_to_add.value.strip()

        # 入力値のバリデーション
        if context not in ["Work", "Personal"]:
             await interaction.followup.send("⚠️ コンテキストは 'Work' または 'Personal' で入力してください。", ephemeral=True)
             return
        if category not in CATEGORY_MAP:
             await interaction.followup.send(f"⚠️ カテゴリは {', '.join(CATEGORY_MAP.keys())} のいずれかで入力してください。", ephemeral=True)
             return
        if not item:
             await interaction.followup.send("⚠️ 追加する項目名を入力してください。", ephemeral=True)
             return

        success = await self.memo_cog.add_item_to_list_file(category, item, context)
        if success:
            await interaction.followup.send(f"✅ **{context}** の **{CATEGORY_MAP[category]['prompt']}** に「{item}」を追加しました。", ephemeral=True)
            list_channel = self.memo_cog.bot.get_channel(LIST_CHANNEL_ID)
            if list_channel:
                 await self.memo_cog.refresh_all_lists_post(list_channel)
            else:
                 logging.warning(f"List channel {LIST_CHANNEL_ID} not found for refresh.")
        else:
            await interaction.followup.send("❌リストへの追加中にエラーが発生しました。", ephemeral=True)

class CompleteItemModal(discord.ui.Modal, title="リストの項目を完了"):
    def __init__(self, memo_cog_instance, items_by_category: dict):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance
        self.items_by_category = items_by_category

        options = []
        # items_by_categoryが空でないことを確認
        if items_by_category:
            for context, categories in items_by_category.items():
                for category, items in categories.items():
                    # itemsが空でないことを確認
                    if items:
                        options.append(discord.SelectOption(
                            label=f"{context} - {CATEGORY_MAP[category]['prompt']}",
                            value=f"{context}|{category}"
                        ))

        # オプションがない場合のプレースホルダーを設定
        placeholder_text = "完了する項目が含まれるリストを選択..." if options else "完了できる項目がありません"
        self.category_select = discord.ui.Select(
            placeholder=placeholder_text,
            options=options if options else [discord.SelectOption(label="項目なし", value="no_items")], # ダミーオプション
            custom_id="category_select",
            disabled=not options # オプションがなければ無効化
        )
        self.category_select.callback = self.on_category_select
        self.add_item(self.category_select)

        self.item_select = discord.ui.Select(placeholder="カテゴリを選択すると項目が表示されます", disabled=True, custom_id="item_select")
        self.add_item(self.item_select)

    async def on_category_select(self, interaction: discord.Interaction):
        # response.defer() の前に interaction.data を確認
        if not interaction.data or 'values' not in interaction.data or not interaction.data['values']:
             # interaction.response.defer(update=True) # deferする前にデータをチェック
             logging.warning("on_category_select: No values selected.")
             # 必要であればユーザーにメッセージを送る
             await interaction.response.send_message("カテゴリが選択されていません。", ephemeral=True, delete_after=5)
             return

        selected_value = interaction.data['values'][0]
        if selected_value == "no_items":
             await interaction.response.defer(update=True) # deferを実行
             return # ダミーオプション選択時は何もしない

        await interaction.response.defer(update=True) # deferを実行
        context, category = selected_value.split('|')
        items = self.items_by_category.get(context, {}).get(category, [])

        if items:
            # オプションが多すぎる場合 (Discordの制限は25個)
            options_for_select = [discord.SelectOption(label=item[:100], value=item) for item in items][:25]
            self.item_select.options = options_for_select
            self.item_select.placeholder = "完了する項目を選択"
            self.item_select.disabled = False
        else:
            self.item_select.options = [] # オプションをクリア
            self.item_select.placeholder = "このリストに項目はありません"
            self.item_select.disabled = True

        try:
             # edit_original_responseではなくedit_messageを使う (モーダル内ではこっち)
             # interaction.messageでモーダルに紐づくメッセージを取得できるとは限らない
             # モーダル内のView更新はinteraction.edit_original_responseで行う
             await interaction.edit_original_response(view=self)
        except discord.HTTPException as e:
             logging.error(f"Failed to edit modal view: {e}")


    async def on_submit(self, interaction: discord.Interaction):
        # 選択値のチェック
        if not self.category_select.values or not self.item_select.values:
             await interaction.response.send_message("カテゴリと項目を選択してください。", ephemeral=True, delete_after=10)
             return

        await interaction.response.defer(ephemeral=True, thinking=True) # thinking=True
        context, category = self.category_select.values[0].split('|')
        item_to_remove = self.item_select.values[0]

        success = await self.memo_cog.remove_item_from_list_file(category, item_to_remove, context)
        if success:
            await interaction.followup.send(f"🗑️ 「{item_to_remove}」をリストから完了（削除）しました。", ephemeral=True)
            # リスト投稿チャンネルを取得して更新
            list_channel = self.memo_cog.bot.get_channel(LIST_CHANNEL_ID)
            if list_channel:
                 await self.memo_cog.refresh_all_lists_post(list_channel)
            else:
                 logging.warning(f"List channel {LIST_CHANNEL_ID} not found for refresh.")
        else:
            await interaction.followup.send("❌ 完了処理中にエラーが発生しました。項目が存在しないか、ファイルアクセスに問題がある可能性があります。", ephemeral=True)

class AddToCalendarModal(discord.ui.Modal, title="カレンダーに登録"):
    def __init__(self, memo_cog_instance, tasks: list):
        super().__init__(timeout=None) # タイムアウトなし
        self.memo_cog = memo_cog_instance

        # tasksが空でないことを確認
        if tasks:
            # タスクが多すぎる場合 (Discord Selectの制限は25個)
            options = [discord.SelectOption(label=task[:100], value=task) for task in tasks][:25]
            placeholder = "カレンダーに登録するタスクを選択..."
            disabled = False
        else:
            options = [discord.SelectOption(label="タスクなし", value="no_task")] # ダミー
            placeholder = "登録できるタスクがありません"
            disabled = True

        self.task_select = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            disabled=disabled
        )
        self.add_item(self.task_select)

        self.target_date = discord.ui.TextInput(
            label="日付 (YYYY-MM-DD形式、空欄で今日)",
            required=False,
            placeholder=datetime.now(JST).strftime('%Y-%m-%d') # 今日の日付をプレースホルダーに
        )
        self.add_item(self.target_date)

    async def on_submit(self, interaction: discord.Interaction):
        # 選択値のチェック
        if not self.task_select.values or self.task_select.values[0] == "no_task":
             await interaction.response.send_message("タスクが選択されていません。", ephemeral=True, delete_after=10)
             return

        await interaction.response.defer(ephemeral=True, thinking=True) # thinking=True
        selected_task = self.task_select.values[0]

        target_date_str = self.target_date.value.strip() # strip() を追加
        target_date_obj = datetime.now(JST).date() # デフォルトは今日
        if target_date_str: # 日付入力がある場合のみパース試行
            try:
                target_date_obj = datetime.strptime(target_date_str, '%Y-%m-%d').date()
            except ValueError:
                await interaction.followup.send("⚠️ 日付の形式が正しくありません。`YYYY-MM-DD`形式で入力するか、空欄にしてください。", ephemeral=True)
                return

        # JournalCogを取得 (CalendarCogではない)
        journal_cog = self.memo_cog.bot.get_cog('JournalCog')
        if not journal_cog or not journal_cog.is_ready or not journal_cog.calendar_service:
            await interaction.followup.send("⚠️ カレンダー機能が利用できません (JournalCog未ロードまたはカレンダー未認証)。", ephemeral=True)
            return

        try:
            # JournalCogのメソッドを呼び出す (関数名を修正)
            # await journal_cog.schedule_task_from_memo(selected_task, target_date_obj) # これは存在しない
            # 代わりに直接イベントを作成する (JournalCogに専用メソッドを作るのが望ましい)

            # --- Google Calendar イベント作成 ---
            event_body = {
                'summary': selected_task,
                'start': {'date': target_date_obj.isoformat()}, # 終日イベントとして登録
                'end': {'date': target_date_obj.isoformat()},
            }
            await asyncio.to_thread(
                 journal_cog.calendar_service.events().insert(
                    calendarId=journal_cog.google_calendar_id, # JournalCogからカレンダーIDを取得
                    body=event_body
                ).execute
            )
            # --- ここまで ---

            await interaction.followup.send(f"✅ 「{selected_task}」を {target_date_obj.strftime('%Y-%m-%d')} の終日予定としてカレンダーに登録しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"カレンダー登録中にエラー ({selected_task}): {e}", exc_info=True)
            await interaction.followup.send(f"❌ カレンダー登録処理中にエラーが発生しました: `{e}`", ephemeral=True)


class ListManagementView(discord.ui.View):
    def __init__(self, memo_cog_instance):
        super().__init__(timeout=None) # 永続View
        self.memo_cog = memo_cog_instance
        # custom_idを設定して再起動後も識別可能に
        self.add_item_button.custom_id = "list_mgmt_add_item"
        self.complete_item_button.custom_id = "list_mgmt_complete_item"
        self.add_to_calendar_button.custom_id = "list_mgmt_add_to_calendar"


    # ボタンデコレータ内で custom_id を指定
    @discord.ui.button(label="項目を追加", style=discord.ButtonStyle.success, emoji="➕", custom_id="list_mgmt_add_item")
    async def add_item_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Dropboxが利用可能かチェック
        if not self.memo_cog.dbx_available:
             await interaction.response.send_message("⚠️ Dropboxが利用できないため、項目を追加できません。", ephemeral=True)
             return
        await interaction.response.send_modal(AddItemModal(self.memo_cog))

    @discord.ui.button(label="項目を完了", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="list_mgmt_complete_item")
    async def complete_item_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Dropboxが利用可能かチェック
        if not self.memo_cog.dbx_available:
             await interaction.response.send_message("⚠️ Dropboxが利用できないため、項目を完了できません。", ephemeral=True)
             return
        items = await self.memo_cog.get_all_list_items_structured()
        await interaction.response.send_modal(CompleteItemModal(self.memo_cog, items))

    @discord.ui.button(label="カレンダーに登録", style=discord.ButtonStyle.secondary, emoji="📅", custom_id="list_mgmt_add_to_calendar")
    async def add_to_calendar_button(self, interaction: discord.Interaction, button: discord.ui.Button):
         # Dropboxが利用可能かチェック
        if not self.memo_cog.dbx_available:
             await interaction.response.send_message("⚠️ Dropboxが利用できないため、カレンダー登録用のタスクを取得できません。", ephemeral=True)
             return

        work_tasks = await self.memo_cog.get_list_items("Task", "Work")
        personal_tasks = await self.memo_cog.get_list_items("Task", "Personal")
        all_tasks = work_tasks + personal_tasks
        if not all_tasks:
            await interaction.response.send_message("カレンダーに登録できるタスクがありません。", ephemeral=True, delete_after=10)
            return
        await interaction.response.send_modal(AddToCalendarModal(self.memo_cog, all_tasks))


# === Cog 本体 ===
class MemoCog(commands.Cog):

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # --- 基本的なチェック ---
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return
        if message.reference or message.interaction:
             return
        if message.attachments and not message.content:
             # ... (voice/image check) ...
             return

        content = message.content.strip()
        if not content:
             return

        logging.info(f"Received message in MEMO_CHANNEL (ID: {message.id}): {content[:50]}...")

        # --- URL種別の判定と処理分岐 ---
        url_content = None
        youtube_match = YOUTUBE_URL_REGEX.match(content)
        web_match = URL_REGEX.match(content)
        is_full_url = False
        if youtube_match and youtube_match.group(0) == content:
            url_content = content; url_type = "youtube"; is_full_url = True
        elif web_match and web_match.group(0) == content:
            url_content = content; url_type = "web"; is_full_url = True
        else: url_type = "text"

        try:
            if url_type == "youtube":
                logging.info(f"YouTube URL detected for message {message.id}. Starting summary process...")
                youtube_cog = self.bot.get_cog('YouTubeCog')
                if youtube_cog and hasattr(youtube_cog, 'perform_summary_async'):
                    await youtube_cog.perform_summary_async(url=url_content, message=message)
                    logging.info(f"YouTube summary process called for message {message.id}")
                else:
                    logging.error("YouTubeCog or perform_summary_async method not found.")
                    await message.add_reaction("⚠️")

            elif url_type == "web":
                logging.info(f"Web URL detected for message {message.id}. Starting web clip process...")
                webclip_cog = self.bot.get_cog('WebClipCog')
                if webclip_cog and hasattr(webclip_cog, 'perform_clip_async'):
                    await webclip_cog.perform_clip_async(url=url_content, message=message)
                    logging.info(f"Web clip process called for message {message.id}")
                else:
                    logging.error("WebClipCog or perform_clip_async method not found.")
                    await message.add_reaction("⚠️")

            elif url_type == "text":
                logging.info(f"Text memo detected for message {message.id}. Saving memo...")
                try:
                    await add_memo_async(
                        content=message.content, # stripしない元の内容
                        author=f"{message.author} ({message.author.id})",
                        created_at=message.created_at.isoformat(),
                        message_id=message.id
                    )
                    await message.add_reaction("📄") # 保存完了
                    logging.info(f"Text memo saved for message {message.id}")
                except Exception as save_e:
                     logging.error(f"Failed to save text memo {message.id}: {save_e}", exc_info=True)
                     await message.add_reaction("💾") # 保存失敗

                if self.gemini_available:
                    logging.info(f"Attempting AI categorization for message {message.id}...")
                    await self.categorize_and_propose_action(message)
                else:
                     logging.info(f"Skipping AI categorization (Gemini not available) for message {message.id}")

        except Exception as e:
            logging.error(f"Error processing message {message.id}: {e}", exc_info=True)
            try:
                # 既存のリアクションがあれば削除してからエラーをつける
                bot_user = self.bot.user
                if bot_user:
                     if isinstance(message, discord.Message): # InteractionMessageはリアクション削除できない
                         await message.remove_reaction("⏳", bot_user)
                         await message.remove_reaction("📄", bot_user)
                if isinstance(message, discord.Message):
                     await message.add_reaction("❌")
            except discord.HTTPException: pass


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # ➕ リアクションによる手動分類のみ
        pass # 省略

    async def categorize_and_propose_action(self, message: discord.Message):
        pass # 省略

    # --- list_group コマンド群 ---
    list_group = app_commands.Group(name="list", description="タスク、アイデアなどのリストを管理します。")

    @list_group.command(name="show", description="指定したカテゴリのリストを表示します。")
    @app_commands.describe(context="どちらのリストを表示しますか？", category="表示したいリストのカテゴリを選択してください。")
    @app_commands.choices(context=CONTEXT_CHOICES, category=CATEGORY_CHOICES)
    async def show_list(self, interaction: discord.Interaction, context: app_commands.Choice[str], category: app_commands.Choice[str]):
        if not self.dbx_available:
            await interaction.response.send_message("⚠️ Dropboxが利用できないため、リストを表示できません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True) # thinking=True
        items = await self.get_list_items(category.value, context.value)

        if category.value == "Task": items = self._sort_tasks_with_deadline(items)

        embed = discord.Embed(title=f"📋 {context.name}の{CATEGORY_MAP[category.value]['prompt']}", color=discord.Color.blue())
        if items:
            description_text = "\n".join([f"- {item}" for item in items])
            if len(description_text) > 4000: description_text = description_text[:3997] + "..."
            embed.description = description_text
        else: embed.description = "このリストにはまだ何もありません。"
        await interaction.followup.send(embed=embed, ephemeral=True) # followupを使用

    # --- 定期実行タスク ---
    @tasks.loop(hours=24) # 実行間隔
    async def post_all_lists(self):
        if not self.dbx_available:
             logging.warning("post_all_lists: Dropbox not available. Skipping task.")
             return
        await self.refresh_all_lists_post()

    async def refresh_all_lists_post(self, channel=None):
        """全てのリストをDiscordに投稿または編集する"""
        if not self.dbx_available:
             logging.warning("refresh_all_lists_post: Dropbox not available.")
             return

        if not channel:
            channel = self.bot.get_channel(LIST_CHANNEL_ID)
        if not channel:
            logging.warning(f"リスト投稿用のチャンネル(ID: {LIST_CHANNEL_ID})が見つかりません。")
            return

        logging.info(f"Refreshing list post in channel {channel.name} ({channel.id})")

        embed = discord.Embed(
            title=f"📅 {datetime.now(JST).strftime('%Y-%m-%d')} のリスト一覧",
            color=discord.Color.orange(),
            timestamp=datetime.now(JST)
        )

        has_items = False
        all_items_structured = await self.get_all_list_items_structured()

        if not all_items_structured and self.dbx_available: # Dropboxは使えるがリストが空 or 取得失敗
             embed.description = "リスト情報を取得できませんでした。Dropbox接続を確認してください。"
             logging.error("Failed to get structured list items for posting (but dbx is available).")
        elif not all_items_structured and not self.dbx_available:
             embed.description = "Dropboxに接続できないためリストを取得できません。"
        else: # リスト取得成功 (空の場合も含む)
            for context_value, categories in all_items_structured.items():
                context_name = next((c.name for c in CONTEXT_CHOICES if c.value == context_value), context_value)
                field_value = ""
                field_has_content = False # このフィールドに内容があるか
                for cat_choice in CATEGORY_CHOICES: # 定義順に表示
                    category_value = cat_choice.value
                    category_name = cat_choice.name
                    items = categories.get(category_value, []) # .get()で安全にアクセス

                    if category_value == "Task": items = self._sort_tasks_with_deadline(items)

                    if items:
                        has_items = True
                        field_has_content = True
                        current_category_text = f"**{category_name}**\n" + "\n".join([f"- {item}" for item in items]) + "\n\n"
                        # フィールド文字数制限チェック
                        if len(field_value) + len(current_category_text) > 1020:
                            # 既存の内容でフィールドを追加
                            if field_value:
                                embed.add_field(name=f"--- {context_name} ---", value=field_value.strip(), inline=False)
                            # 新しいフィールドを開始 (長すぎる場合は切り詰め)
                            field_value = current_category_text[:1020] + "..." if len(current_category_text) > 1024 else current_category_text
                        else:
                             field_value += current_category_text

                # ループ後、field_valueに残っている内容があればフィールド追加
                if field_value:
                    # 前のフィールドと同じコンテキスト名を使うか、(続き)を付けるか
                    embed.add_field(name=f"--- {context_name} ---" if field_has_content else f"--- {context_name} (空) ---", value=field_value.strip() if field_value else "項目なし", inline=False)


        if not has_items and all_items_structured : # 構造化データは取得できたが空だった場合
            embed.description = "すべてのリストは現在空です。"

        # --- 永続Viewの適用 ---
        # 毎回新しいViewインスタンスを作成して適用する
        view = ListManagementView(self)

        try:
            # チャンネルの最終メッセージが自分のリストメッセージか確認
            last_message = None
            async for msg in channel.history(limit=1): last_message = msg

            if self.last_list_message_id and last_message and last_message.id == self.last_list_message_id:
                message = await channel.fetch_message(self.last_list_message_id)
                await message.edit(embed=embed, view=view) # 常に新しいViewを適用
                logging.info(f"リスト投稿を編集しました (Message ID: {self.last_list_message_id})")
            else:
                # チャンネル内の古いリストメッセージを削除 (任意)
                deleted_count = 0
                async for old_msg in channel.history(limit=50): # 検索範囲を限定
                    if old_msg.author == self.bot.user and old_msg.embeds:
                        if "リスト一覧" in old_msg.embeds[0].title:
                            try:
                                await old_msg.delete()
                                deleted_count += 1
                                await asyncio.sleep(1) # Rate limit対策
                            except discord.HTTPException: pass # 削除失敗は無視
                if deleted_count > 0: logging.info(f"{deleted_count}件の古いリスト投稿を削除しました。")

                # 新規投稿
                message = await channel.send(embed=embed, view=view)
                self.last_list_message_id = message.id
                logging.info(f"リストを新規投稿しました (Message ID: {self.last_list_message_id})")

        except (discord.NotFound, discord.Forbidden) as e:
            logging.warning(f"リスト投稿の編集/削除に失敗 ({e})。新規投稿を試みます。")
            try:
                # 念のため古いメッセージ削除を再試行 (より広い範囲で)
                async for old_msg in channel.history(limit=100):
                     if old_msg.author == self.bot.user and old_msg.embeds and "リスト一覧" in old_msg.embeds[0].title:
                         try: await old_msg.delete(); await asyncio.sleep(1)
                         except discord.HTTPException: pass

                message = await channel.send(embed=embed, view=view)
                self.last_list_message_id = message.id
                logging.info(f"リストを新規投稿しました (Message ID: {self.last_list_message_id})")
            except discord.HTTPException as send_e:
                 logging.error(f"リストの新規投稿にも失敗しました: {send_e}")
                 self.last_list_message_id = None # 失敗時はIDをクリア
        except Exception as e:
            logging.error(f"リスト投稿の更新中に予期せぬエラー: {e}", exc_info=True)
            self.last_list_message_id = None # 不明なエラー時もIDクリア


    @post_all_lists.before_loop
    async def before_post_task_list(self):
        """ループ開始前にBotの準備と初回実行時間を待つ"""