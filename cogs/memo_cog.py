import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import timezone
from obsidian_handler import add_memo_async
import json
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re

MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))

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
    def __init__(self, memo_cog_instance, message: discord.Message, category: str, item_to_add: str):
        super().__init__(timeout=180)
        self.memo_cog = memo_cog_instance
        self.message = message
        self.category = category
        self.item_to_add = item_to_add

    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        success = await self.memo_cog.add_item_to_list_file(self.category, self.item_to_add)
        if success:
            # 修正点1: edit_original_message -> edit_original_response
            await interaction.edit_original_response(
                content=f"✅ **{CATEGORY_MAP[self.category]['prompt']}** に「{self.item_to_add}」を追加しました。",
                view=None
            )
        else:
            # 修正点1: edit_original_message -> edit_original_response
            await interaction.edit_original_response(content="❌リストへの追加中にエラーが発生しました。", view=None)
        self.stop()

    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()

class RemoveFromListView(discord.ui.View):
    """リストから項目を削除するためのボタンを持つView"""
    def __init__(self, memo_cog_instance, category: str, items: list):
        super().__init__(timeout=300)
        self.memo_cog = memo_cog_instance
        self.category = category
        
        if not items:
            self.add_item(discord.ui.Button(label="このリストに項目はありません", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            for item in items:
                self.add_item(discord.ui.Button(label=item[:80], style=discord.ButtonStyle.danger, custom_id=f"remove_{item}"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get("custom_id")
        if custom_id and custom_id.startswith("remove_"):
            item_to_remove = custom_id.replace("remove_", "")
            
            success = await self.memo_cog.remove_item_from_list_file(self.category, item_to_remove)
            
            if success:
                await interaction.response.send_message(f"🗑️ 「{item_to_remove}」をリストから削除しました。", ephemeral=True)
                new_items = await self.memo_cog.get_list_items(self.category)
                new_view = RemoveFromListView(self.memo_cog, self.category, new_items)
                await interaction.message.edit(view=new_view)
            else:
                await interaction.response.send_message("❌ 削除中にエラーが発生しました。", ephemeral=True)

        return False

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

    async def get_list_items(self, category: str) -> list[str]:
        """Obsidianのリストファイルから未完了の項目を読み込む"""
        list_info = CATEGORY_MAP.get(category)
        if not list_info: return []
        
        file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{list_info['file']}"
        
        try:
            _, res = self.dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            # チェックボックス形式 `- [ ] item` を正規表現で抽出
            items = re.findall(r"-\s*\[\s*\]\s*(.+)", content)
            return [item.strip() for item in items]
        except ApiError as e:
            # 修正点2: 正しいエラー判定ロジックに修正
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return [] # ファイルが存在しない場合は空リスト
            logging.error(f"Dropboxファイルのダウンロードに失敗: {e}")
            return []

    async def add_item_to_list_file(self, category: str, item: str) -> bool:
        """Obsidianのリストファイルに項目を追記する"""
        list_info = CATEGORY_MAP.get(category)
        if not list_info: return False
        
        file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{list_info['file']}"
        
        try:
            try:
                _, res = self.dbx.files_download(file_path)
                content = res.content.decode('utf-8')
            except ApiError as e:
                # 修正点2: 正しいエラー判定ロジックに修正
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

    async def remove_item_from_list_file(self, category: str, item_to_remove: str) -> bool:
        """Obsidianのリストファイルから指定された項目を完了済みにする"""
        list_info = CATEGORY_MAP.get(category)
        if not list_info: return False

        file_path = f"{self.dropbox_vault_path}{LISTS_PATH}/{list_info['file']}"
        
        try:
            _, res = self.dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            
            # 削除対象の行を `- [x]` に置換
            # 正規表現でエスケープ処理を忘れずに
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

        calendar_cog = self.bot.get_cog('CalendarCog')
        if calendar_cog and message.reference and message.reference.message_id:
            if any(p['prompt_msg_id'] == message.reference.message_id for p in calendar_cog.pending_date_prompts.values()):
                logging.info("カレンダーの日付指定への返信のため、メモ保存をスキップします。")
                return

        try:
            await add_memo_async(
                content=message.content,
                author=f"{message.author} ({message.author.id})",
                created_at=message.created_at.replace(tzinfo=timezone.utc).isoformat(),
                message_id=message.id
            )
            await message.add_reaction("✅")
        except Exception as e:
            logging.error(f"[memo_cog] Failed to save memo: {e}", exc_info=True)
            await message.add_reaction("❌")

        if self.gemini_model:
            await self.categorize_and_propose_action(message)

    async def categorize_and_propose_action(self, message: discord.Message):
        """メモを分類し、リストへの追加を提案する"""
        prompt = f"""
        以下のメモの内容を分析し、最も適切なカテゴリを一つだけ選んでください。
        - **Task**: 具体的な行動が必要なタスク（例：「〇〇を買いに行く」「〇〇を予約する」）
        - **Idea**: アイデア、考え、気づき、備忘録
        - **Shopping**: 買いたい物そのもの（例：「牛乳」「電池」）
        - **Bookmark**: 行きたい場所、見たい映画、気になる本やWebサイト
        - **Other**: 上記のいずれにも当てはまらない一般的なメモ

        出力は必ず `{{ "category": "選んだカテゴリ名", "item": "リストに追加すべき具体的な項目名" }}` というJSON形式で行ってください。
        itemはメモから抽出した、リストに追加するのに最適な短い名詞または動詞句にしてください。

        ---
        メモ: {message.content}
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            # AIの出力からJSON部分のみを抽出する
            json_match = re.search(r'```json\n(\{.*?\})\n```', response.text, re.DOTALL)
            json_text = json_match.group(1) if json_match else response.text
            result_json = json.loads(json_text)
            
            category = result_json.get("category")
            item = result_json.get("item")

            if category and item and category in CATEGORY_MAP:
                prompt_text = CATEGORY_MAP[category]['prompt']
                view = AddToListView(self, message, category, item)
                await message.reply(f"このメモを **{prompt_text}** に追加しますか？\n`{item}`", view=view)

        except (json.JSONDecodeError, KeyError) as e:
            logging.warning(f"メモの分類結果の解析に失敗: {e}\nAI Response: {response.text}")
        except Exception as e:
            logging.error(f"メモの分類中に予期せぬエラー: {e}", exc_info=True)
            
    # --- スラッシュコマンドの定義 ---
    list_group = app_commands.Group(name="list", description="タスク、アイデアなどのリストを管理します。")

    @list_group.command(name="show", description="指定したカテゴリのリストを表示します。")
    @app_commands.describe(category="表示したいリストのカテゴリを選択してください。")
    @app_commands.choices(category=[
        app_commands.Choice(name="タスク", value="Task"),
        app_commands.Choice(name="アイデア", value="Idea"),
        app_commands.Choice(name="買い物リスト", value="Shopping"),
        app_commands.Choice(name="ブックマーク", value="Bookmark"),
    ])
    async def show_list(self, interaction: discord.Interaction, category: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        category_key = category.value
        prompt_text = CATEGORY_MAP[category_key]['prompt']
        
        items = await self.get_list_items(category_key)
        
        embed = discord.Embed(title=f"📋 {prompt_text}", color=discord.Color.blue())
        
        if not items:
            embed.description = "このリストにはまだ何もありません。"
        else:
            embed.description = "\n".join([f"- {item}" for item in items])
            
        await interaction.followup.send(embed=embed)

    @list_group.command(name="remove", description="指定したリストから項目を削除（完了）します。")
    @app_commands.describe(category="項目を削除したいリストのカテゴリを選択してください。")
    @app_commands.choices(category=[
        app_commands.Choice(name="タスク", value="Task"),
        app_commands.Choice(name="アイデア", value="Idea"),
        app_commands.Choice(name="買い物リスト", value="Shopping"),
        app_commands.Choice(name="ブックマーク", value="Bookmark"),
    ])
    async def remove_from_list(self, interaction: discord.Interaction, category: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        category_key = category.value
        prompt_text = CATEGORY_MAP[category_key]['prompt']
        
        items = await self.get_list_items(category_key)
        view = RemoveFromListView(self, category_key, items)
        
        await interaction.followup.send(
            f"**{prompt_text}** から削除（完了）したい項目を選んでください。",
            view=view
        )

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info("[memo_cog] Bot is ready.")

async def setup(bot):
    await bot.add_cog(MemoCog(bot))