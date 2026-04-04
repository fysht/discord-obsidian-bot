import os
import logging
import datetime
from discord.ext import commands
from config import JST
from utils.obsidian_utils import update_section

INVESTMENT_FOLDER = "Investment"
STOCKS_FOLDER = "Stocks"


class StockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    async def _get_stock_list(self):
        """Stocksフォルダ内のファイル一覧を取得"""
        service = self.drive_service.get_service()
        if not service:
            return []

        inv_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, INVESTMENT_FOLDER
        )
        if not inv_folder:
            return []
        stk_folder = await self.drive_service.find_file(
            service, inv_folder, STOCKS_FOLDER
        )
        if not stk_folder:
            return []

        query = f"'{stk_folder}' in parents and mimeType = 'text/markdown' and trashed = false"
        try:
            results = service.files().list(q=query, fields="files(id, name)").execute()
            return results.get("files", [])
        except Exception as e:
            logging.error(f"Stock list fetch error: {e}")
            return []

    async def _find_stock_note_id(self, code: str):
        """銘柄コードからノートのファイルIDを検索"""
        stock_files = await self._get_stock_list()
        for f in stock_files:
            if f["name"].startswith(f"{code}_"):
                return f["id"]
        return None

    async def _append_memo_to_note(self, file_id: str, memo: str):
        """既存のノートにメモを追記"""
        service = self.drive_service.get_service()
        if not service:
            return False

        try:
            content = await self.drive_service.read_text_file(service, file_id)
            now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
            append_text = f"- {now_str} {memo}"
            new_content = update_section(content, append_text, "## Logs")
            await self.drive_service.update_text(service, file_id, new_content)
            return True
        except Exception as e:
            logging.error(f"Stock append error: {e}")
            return False

    async def _save_file(self, filename: str, content: str):
        """新しい銘柄ノートを作成して保存"""
        service = self.drive_service.get_service()
        if not service:
            return False

        inv_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, INVESTMENT_FOLDER
        )
        if not inv_folder:
            inv_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, INVESTMENT_FOLDER
            )
        stk_folder = await self.drive_service.find_file(
            service, inv_folder, STOCKS_FOLDER
        )
        if not stk_folder:
            stk_folder = await self.drive_service.create_folder(
                service, inv_folder, STOCKS_FOLDER
            )

        try:
            await self.drive_service.upload_text(service, stk_folder, filename, content)
            return True
        except Exception as e:
            logging.error(f"Stock save error: {e}")
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(StockCog(bot))
