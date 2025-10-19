import os
import discord
from discord.ext import commands
import asyncio
import logging
import dropbox
from dropbox.files import WriteMode
from datetime import datetime, timezone, timedelta
import json
from obsidian_handler import add_memo_async

try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
    JST = timezone(timedelta(hours=+9), "JST")

LISTS_PATH = "/Lists"
ADD_TO_LIST_EMOJI = "➕"

CATEGORY_MAP = {
    "Task": {"file": "Tasks.md", "prompt": "タスク"},
    "Idea": {"file": "Ideas.md", "prompt": "アイデア"},
    "Shopping": {"file": "Shopping List.md", "prompt": "買い物リスト"},
    "Bookmark": {"file": "Bookmarks.md", "prompt": "ブックマーク"},
}


class AddToListView(discord.ui.View):
    def __init__(self, memo_cog_instance, item_to_add: str):
        super().__init__(timeout=30)
        self.memo_cog = memo_cog_instance
        self.item_to_add = item_to_add
        self.add_item(AddToListButton(label="リストに追加", style=discord.ButtonStyle.primary, memo_cog=memo_cog_instance, item=item_to_add))


class AddToListButton(discord.ui.Button):
    def __init__(self, label, style, memo_cog, item):
        super().__init__(label=label, style=style)
        self.memo_cog = memo_cog
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        modal = ManualAddToListModal(self.memo_cog, self.item)
        await interaction.response.send_modal(modal)


class ManualAddToListModal(discord.ui.Modal, title="リストに追加"):
    def __init__(self, memo_cog_instance, item_to_add: str):
        super().__init__(timeout=None)
        self.memo_cog = memo_cog_instance
        self.item_to_add = item_to_add

        self.context = discord.ui.TextInput(
            label="コンテキスト", placeholder="Work または Personal"
        )
        self.category = discord.ui.TextInput(
            label="カテゴリ", placeholder="Task, Idea, Shopping, Bookmark"
        )

        self.add_item(self.context)
        self.add_item(self.category)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        context_val = self.context.value.strip().capitalize()
        category_val = self.category.value.strip().capitalize()

        if context_val in ["Work", "Personal"] and category_val in CATEGORY_MAP:
            success = await self.memo_cog.add_item_to_list_file(
                category_val, self.item_to_add, context_val
            )
            if success:
                await interaction.followup.send("✅ 追加成功", ephemeral=True)
            else:
                await interaction.followup.send("❌ 追加エラー", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ 不正入力", ephemeral=True)


class MemoCog(commands.Cog):
    """Discordで受け取ったメモをObsidianやDropboxに保存するCog"""

    def __init__(self, bot: commands.Bot, dbx_client=None):
        self.bot = bot
        self.dbx = dbx_client
        self.reply_message = None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """#memo チャンネルに投稿されたメッセージを保存"""
        if message.author.bot:
            return
        if message.channel.name != "memo":
            return

        content = message.content.strip()
        if not content:
            return

        # メモをObsidianに保存
        await add_memo_async(content, context="General", category="Memo")

        # ユーザーに返信
        self.reply_message = await message.channel.send(
            f"📝 メモを記録しました: 「{content[:30]}...」",
            view=AddToListView(self, item_to_add=content),
        )

        # 60秒後に削除
        await asyncio.sleep(60)
        if self.reply_message:
            try:
                await self.reply_message.edit(content="📝 メモのみ記録", view=None)
                await asyncio.sleep(10)
                await self.reply_message.delete()
            except Exception as e:
                logging.error(f"メモ削除時エラー: {e}")

    async def add_item_to_list_file(self, category: str, item: str, context: str) -> bool:
        """Dropbox上のリストファイルにアイテムを追加"""
        if not self.dbx:
            logging.error("Dropboxクライアント未初期化")
            return False

        if category not in CATEGORY_MAP:
            logging.error(f"不正なカテゴリ: {category}")
            return False

        file_path = f"{LISTS_PATH}/{context}/{CATEGORY_MAP[category]['file']}"
        timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        new_entry = f"- {item}  （{timestamp}）\n"

        try:
            # 既存データ取得
            existing_data = ""
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, file_path)
                existing_data = res.content.decode("utf-8")
            except dropbox.exceptions.ApiError:
                logging.info(f"新規ファイル作成: {file_path}")

            # 新規データ追記
            updated_data = existing_data + new_entry

            await asyncio.to_thread(
                self.dbx.files_upload,
                updated_data.encode("utf-8"),
                file_path,
                mode=WriteMode("overwrite"),
            )
            logging.info(f"Dropboxに追加成功: {file_path}")
            return True

        except Exception as e:
            logging.error(f"Dropbox書き込みエラー: {e}")
            return False


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    dbx_token = os.getenv("DROPBOX_ACCESS_TOKEN")
    dbx_client = dropbox.Dropbox(dbx_token) if dbx_token else None
    await bot.add_cog(MemoCog(bot, dbx_client))