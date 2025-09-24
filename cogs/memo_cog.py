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

# --- 環境変数 ---
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))
TASK_LIST_CHANNEL_ID = int(os.getenv("TASK_LIST_CHANNEL_ID", "0"))
JST = timezone(timedelta(hours=+9), 'JST')

# --- 定数 ---
LISTS_PATH = "/Lists"
CATEGORY_MAP = {
    "Task": {"file": "Tasks.md", "prompt": "タスクリスト"},
    "Idea": {"file": "Ideas.md", "prompt": "アイデアリスト"},
    "Shopping": {"file": "Shopping List.md", "prompt": "買い物リスト"},
    "Bookmark": {"file": "Bookmarks.md", "prompt": "ブックマークリスト"},
}

# --- View定義 ---
class AddToListView(discord.ui.View):
    """メモをリストに追加するための確認ボタンを持つView"""
    def __init__(self, memo_cog_instance, message: discord.Message, category: str, item_to_add: str, context: str):
        super().__init__(timeout=180)
        self.memo_cog = memo_cog_instance
        self.message = message
        self.category = category
        self.item_to_add = item_to_add
        self.context = context

    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        success = await self.memo_cog.add_item_to_list_file(self.category, self.item_to_add, self.context)
        if success:
            await interaction.edit_original_response(
                content=f"✅ **{self.context}** の **{CATEGORY_MAP[self.category]['prompt']}** に「{self.item_to_add}」を追加しました。",
                view=None
            )
        else:
            await interaction.edit_original_response(content="❌リストへの追加中にエラーが発生しました。", view=None)
        self.stop()

    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()


class RemoveFromListView(discord.ui.View):
    """リストから項目を削除するためのボタンを持つView"""
    def __init__(self, memo_cog_instance, category: str, items: list, context: str):
        super().__init__(timeout=300)
        self.memo_cog = memo_cog_instance
        self.category = category
        self.context = context

        if not items:
            self.add_item(discord.ui.Button(label="このリストに項目はありません", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            for item in items:
                self.add_item(discord.ui.Button(label=item[:80], style=discord.ButtonStyle.danger, custom_id=f"remove_{item}"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get("custom_id")
        if custom_id and custom_id.startswith("remove_"):
            item_to_remove = custom_id.replace("remove_", "")
            success = await self.memo_cog.remove_item_from_list_file(self.category, item_to_remove, self.context)

            if success:
                await interaction.response.send_message(f"🗑️ 「{item_to_remove}」をリストから削除しました。", ephemeral=True)
                new_items = await self.memo_cog.get_list_items(self.category, self.context)
                new_view = RemoveFromListView(self.memo_cog, self.category, new_items, self.context)
                await interaction.message.edit(view=new_view)
            else:
                await interaction.response.send_message("❌ 削除中にエラーが発生しました。", ephemeral=True)

        return False

# カレンダー登録選択用のView
class AddToCalendarView(discord.ui.View):
    def __init__(self, memo_cog_instance, category: str, items: list, context: str):
        super().__init__(timeout=300)
        self.memo_cog = memo_cog_instance
        self.category = category
        self.context = context

        if not items:
            self.add_item(discord.ui.Button(label="このリストに項目はありません", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            # 各項目をプルダウンメニューの選択肢として追加
            options = [discord.SelectOption(label=item[:100], value=item) for item in items]
            self.add_item(discord.ui.Select(placeholder="カレンダーに登録するタスクを選択...", options=options, custom_id="add_to_calendar_select"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.data.get("custom_id") == "add_to_calendar_select":
            selected_item = interaction.data["values"][0]
            
            # CalendarCogを取得
            calendar_cog = self.memo_cog.bot.get_cog('CalendarCog')
            if not calendar_cog:
                await interaction.response.send_message("カレンダー機能が利用できません。", ephemeral=True)
                return False

            await interaction.response.defer(ephemeral=True)
            
            try:
                # CalendarCogのメソッドを呼び出して終日予定として登録
                today = datetime.now(JST).date()
                await calendar_cog._create_google_calendar_event(selected_item, today)
                
                # リストから項目を削除
                success_remove = await self.memo_cog.remove_item_from_list_file(self.category, selected_item, self.context)
                if success_remove:
                    await interaction.followup.send(f"✅ 「{selected_item}」をカレンダーに登録し、リストから削除しました。")
                else:
                     await interaction.followup.send(f"✅ 「{selected_item}」をカレンダーに登録しましたが、リストからの削除に失敗しました。")
            except Exception as e:
                logging.error(f"カレンダー登録またはリスト削除中にエラー: {e}", exc_info=True)
                await interaction.followup.send(f"❌ 処理中にエラーが発生しました。")
        
        return False

# Highlight選択用のView
class HighlightSelectionView(discord.ui.View):
    def __init__(self, tasks: list, calendar_cog):
        super().__init__(timeout=300)
        self.calendar_cog = calendar_cog
        for task in tasks:
            button = discord.ui.Button(label=task[:80], style=discord.ButtonStyle.secondary, custom_id=f"highlight_{task}")
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        if not self.calendar_cog:
            await interaction.response.send_message("カレンダー機能が利用できません。", ephemeral=True)
            return

        selected_task = interaction.data['custom_id'].replace("highlight_", "")
        await interaction.response.defer(ephemeral=True)

        # カレンダーに登録
        event_summary = f"✨ ハイライト: {selected_task}"
        today = datetime.now(JST).date()

        try:
            # calendar_cogの内部メソッドを呼び出す
            await self.calendar_cog._create_google_calendar_event(event_summary, today)
            await interaction.followup.send(f"✅ 今日のハイライト「**{selected_task}**」をカレンダーに終日予定として登録しました！")
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
        except Exception as e:
            logging.error(f"ハイライトのカレンダー登録中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ カレンダーへの登録中にエラーが発生しました。")
        self.stop()

# --- Cog本体 ---

class MemoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
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
            self.post_task_list.start()

    def cog_unload(self):
        self.post_task_list.cancel()

    async def get_list_items(self, category: str, context: str) -> list[str]:
        """Obsidianのリストファイルから未完了の項目を読み込む"""
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
        """Obsidianのリストファイルに項目を追記する"""
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
            if content.strip().endswith("\n"):
                new_content = content + line_to_add + "\n"
            else:
                new_content = content + "\n" + line_to_add + "\n"
            self.dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
            logging.info(f"{file_path} に '{item}' を追記しました。")
            return True
        except Exception as e:
            logging.error(f"リストファイルへの追記中にエラー: {e}")
            return False

    async def remove_item_from_list_file(self, category: str, item_to_remove: str, context: str) -> bool:
        """Obsidianのリストファイルから指定された項目を完了済みにする"""
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

        if self.gemini_model:
            await self.categorize_and_propose_action(message)

    async def categorize_and_propose_action(self, message: discord.Message):
        """メモを分類し、リストへの追加を提案する"""
        prompt = f"""
        以下のメモの内容を分析し、「コンテキスト」「カテゴリ」「リストに追加すべき項目名」を判断してください。

        # 指示
        1.  **コンテキスト**: このメモが「Work（仕事）」に関するものか、「Personal（プライベート）」に関するものか判断してください。
        2.  **カテゴリ**: 最も適切なカテゴリを一つだけ選んでください。
            - **Task**: 具体的な行動が必要なタスク
            - **Idea**: アイデア、考え、気づき
            - **Shopping**: 買いたい物
            - **Bookmark**: 後で見たい・行きたい場所やコンテンツ
            - **Other**: 上記のいずれにも当てはまらないメモ
        3.  **項目名**: メモから抽出し、リストに追加するのに最適な短い名詞または動詞句にしてください。

        # 出力形式
        - 出力は必ず以下のJSON形式で行ってください。
        - JSON以外の説明文や前置きは一切含めないでください。
        ```json
        {{
          "context": "Work" or "Personal",
          "category": "選んだカテゴリ名",
          "item": "リストに追加すべき具体的な項目名"
        }}
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
                await message.reply(f"このメモを **{context}** の **{prompt_text}** に追加しますか？\n`{item}`", view=view)

        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            logging.warning(f"メモの分類結果の解析に失敗: {e}\nAI Response: {response.text}")
        except Exception as e:
            logging.error(f"メモの分類中に予期せぬエラー: {e}", exc_info=True)

    # --- スラッシュコマンド ---
    list_group = app_commands.Group(name="list", description="タスク、アイデアなどのリストを管理します。")

    @list_group.command(name="show", description="指定したカテゴリのリストを表示します。")
    @app_commands.describe(
        context="どちらのリストを表示しますか？",
        category="表示したいリストのカテゴリを選択してください。"
    )
    @app_commands.choices(context=[
        app_commands.Choice(name="仕事", value="Work"),
        app_commands.Choice(name="プライベート", value="Personal"),
    ], category=[
        app_commands.Choice(name="タスク", value="Task"),
        app_commands.Choice(name="アイデア", value="Idea"),
        app_commands.Choice(name="買い物リスト", value="Shopping"),
        app_commands.Choice(name="ブックマーク", value="Bookmark"),
    ])
    async def show_list(self, interaction: discord.Interaction, context: app_commands.Choice[str], category: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        items = await self.get_list_items(category.value, context.value)
        embed = discord.Embed(title=f"📋 {context.name}の{CATEGORY_MAP[category.value]['prompt']}", color=discord.Color.blue())
        if not items:
            embed.description = "このリストにはまだ何もありません。"
        else:
            embed.description = "\n".join([f"- {item}" for item in items])
        
        view = AddToCalendarView(self, category.value, items, context.value) if category.value == "Task" else None
        
        await interaction.followup.send(embed=embed, view=view)

    @list_group.command(name="remove", description="指定したリストから項目を削除（完了）します。")
    @app_commands.describe(
        context="どちらのリストから削除しますか？",
        category="項目を削除したいリストのカテゴリを選択してください。"
    )
    @app_commands.choices(context=[
        app_commands.Choice(name="仕事", value="Work"),
        app_commands.Choice(name="プライベート", value="Personal"),
    ], category=[
        app_commands.Choice(name="タスク", value="Task"),
        app_commands.Choice(name="アイデア", value="Idea"),
        app_commands.Choice(name="買い物リスト", value="Shopping"),
        app_commands.Choice(name="ブックマーク", value="Bookmark"),
    ])
    async def remove_from_list(self, interaction: discord.Interaction, context: app_commands.Choice[str], category: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        items = await self.get_list_items(category.value, context.value)
        view = RemoveFromListView(self, category.value, items, context.value)
        await interaction.followup.send(
            f"**{context.name}** の **{CATEGORY_MAP[category.value]['prompt']}** から削除（完了）したい項目を選んでください。",
            view=view
        )

    @app_commands.command(name="highlight", description="今日のハイライト（最重要事項）をタスクリストから選択します。")
    @app_commands.describe(context="どちらのタスクリストから選択しますか？")
    @app_commands.choices(context=[
        app_commands.Choice(name="仕事", value="Work"),
        app_commands.Choice(name="プライベート", value="Personal"),
    ])
    async def set_highlight(self, interaction: discord.Interaction, context: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        # calendar_cogをbotから取得
        calendar_cog = self.bot.get_cog('CalendarCog')
        if not calendar_cog:
            await interaction.followup.send("エラー: カレンダー機能がロードされていません。")
            return

        tasks = await self.get_list_items("Task", context.value)
        if not tasks:
            await interaction.followup.send(f"{context.name}のタスクリストは空です。まずはタスクを追加してください。")
            return

        view = HighlightSelectionView(tasks, calendar_cog)
        await interaction.followup.send(f"**{context.name}** のタスクリストから、今日のハイライトを選択してください。", view=view)

    @app_commands.command(name="add_memo_to_list", description="過去のメモをIDを指定してリストに追加します。")
    @app_commands.describe(message_id="リストに追加したいメモのメッセージID")
    async def add_memo_to_list(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True)
        try:
            target_message = await self.bot.get_channel(MEMO_CHANNEL_ID).fetch_message(int(message_id))
            if target_message.author.bot or not target_message.content:
                await interaction.followup.send("指定されたIDのメッセージは処理対象外です。")
                return
            
            # 分類処理を再実行
            await self.categorize_and_propose_action(target_message)
            await interaction.followup.send(f"メッセージID: {message_id} のメモについて、リスト追加の提案を再表示しました。")

        except (ValueError, discord.NotFound):
            await interaction.followup.send("指定されたメッセージIDが見つかりません。")
        except Exception as e:
            await interaction.followup.send(f"エラーが発生しました: {e}")

    # --- 定期実行タスク ---
    @tasks.loop(hours=8)
    async def post_task_list(self):
        channel = self.bot.get_channel(TASK_LIST_CHANNEL_ID)
        if not channel: return
            
        logging.info("定期タスクリストの投稿を実行します。")
        
        # 仕事とプライベートの両方のタスクリストを取得
        work_tasks = await self.get_list_items("Task", "Work")
        personal_tasks = await self.get_list_items("Task", "Personal")
        
        embed = discord.Embed(title="現在のタスクリスト", color=discord.Color.orange())
        
        work_desc = "\n".join([f"- {item}" for item in work_tasks]) if work_tasks else "タスクはありません"
        personal_desc = "\n".join([f"- {item}" for item in personal_tasks]) if personal_tasks else "タスクはありません"

        embed.add_field(name="💼 仕事 (Work)", value=work_desc, inline=False)
        embed.add_field(name="🏠 プライベート (Personal)", value=personal_desc, inline=False)
        
        await channel.send(embed=embed)

    @post_task_list.before_loop
    async def before_post_task_list(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MemoCog(bot))