import os
import discord
from discord.ext import commands, tasks
import logging
import aiohttp
import google.generativeai as genai
from datetime import datetime
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, FileMetadata
from dropbox.exceptions import ApiError
import json
import re
import io
import asyncio

# 共通関数をインポート
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUPPORTED_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'application/pdf']
SCAN_FOLDER = "/Inbox/Scans" # 監視するDropboxフォルダ
PROCESSED_LIST_PATH = "/ObsidianVault/.bot/processed_scans.json" # 処理済みファイルのIDリスト

class HandwrittenMemoCog(commands.Cog):
    """
    手書きメモ画像をテキスト化するCog。
    - Memo Sheet -> そのままObsidianに保存
    - Daily Log Board -> JournalCogに渡してアドバイス生成・保存
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.channel_id = int(os.getenv("HANDWRITTEN_MEMO_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        # 監視設定
        self.scan_folder = os.getenv("DROPBOX_SCAN_FOLDER", f"{self.dropbox_vault_path}{SCAN_FOLDER}")
        self.processed_list_path = os.getenv("DROPBOX_PROCESSED_LIST_PATH", PROCESSED_LIST_PATH)

        # --- 初期チェックとクライアント初期化 ---
        self.is_ready = False
        if not self.channel_id:
            logging.warning("HandwrittenMemoCog: HANDWRITTEN_MEMO_CHANNEL_IDが設定されていません。")
            return
        if not self.gemini_api_key:
            logging.warning("HandwrittenMemoCog: GEMINI_API_KEYが設定されていません。")
            return
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("HandwrittenMemoCog: Dropboxの認証情報が不足しています。")
            return

        self.session = aiohttp.ClientSession()
        genai.configure(api_key=self.gemini_api_key)
        self.gemini_model = genai.GenerativeModel("gemini-2.5-pro", generation_config={"response_mime_type": "application/json"})
        self.is_ready = True
        logging.info("✅ HandwrittenMemoCogが正常に初期化されました。")

    async def cog_unload(self):
        await self.session.close()
        self.check_dropbox_folder.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready and not self.check_dropbox_folder.is_running():
            self.check_dropbox_folder.start()
            logging.info(f"📂 Dropbox Watcher Started: {self.scan_folder}")

    # =========================================================================
    # Helper: Processed List Management
    # =========================================================================
    async def _load_processed_ids(self, dbx) -> list:
        try:
            _, res = dbx.files_download(self.processed_list_path)
            return json.loads(res.content.decode('utf-8'))
        except (ApiError, json.JSONDecodeError):
            return []

    async def _save_processed_id(self, dbx, file_id: str):
        ids = await self._load_processed_ids(dbx)
        if file_id not in ids:
            ids.append(file_id)
            if len(ids) > 1000: ids = ids[-1000:]
            data = json.dumps(ids, ensure_ascii=False).encode('utf-8')
            dbx.files_upload(data, self.processed_list_path, mode=WriteMode('overwrite'))

    # =========================================================================
    # 1. Discord Message Handler
    # =========================================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return
        
        if message.attachments:
            valid_attachments = [att for att in message.attachments if att.content_type in SUPPORTED_TYPES]
            if valid_attachments:
                await message.add_reaction("⏳")
                for attachment in valid_attachments:
                    async with self.session.get(attachment.url) as resp:
                        if resp.status != 200: continue
                        file_bytes = await resp.read()
                    
                    result_embed = await self._process_file_logic(
                        filename=attachment.filename,
                        mime_type=attachment.content_type,
                        file_bytes=file_bytes,
                        source_type="Discord"
                    )
                    await message.reply(embed=result_embed)
                
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("✅")

    # =========================================================================
    # 2. Dropbox Folder Watcher
    # =========================================================================
    @tasks.loop(minutes=1.0)
    async def check_dropbox_folder(self):
        if not self.is_ready: return
        
        try:
            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                processed_ids = await self._load_processed_ids(dbx)
                try:
                    result = dbx.files_list_folder(self.scan_folder)
                except ApiError:
                    return

                for entry in result.entries:
                    if isinstance(entry, FileMetadata):
                        if entry.id in processed_ids: continue

                        ext = os.path.splitext(entry.name)[1].lower()
                        mime_type = self._get_mime_from_ext(ext)
                        if not mime_type: continue

                        logging.info(f"📂 New scan detected: {entry.name}")
                        _, res = dbx.files_download(entry.path_lower)
                        file_bytes = res.content
                        
                        result_embed = await self._process_file_logic(
                            filename=entry.name,
                            mime_type=mime_type,
                            file_bytes=file_bytes,
                            source_type="Dropbox Watcher"
                        )

                        channel = self.bot.get_channel(self.channel_id)
                        if channel:
                            await channel.send(embed=result_embed)

                        await self._save_processed_id(dbx, entry.id)
                        logging.info(f"Marked as processed: {entry.name}")

        except Exception as e:
            logging.error(f"Dropbox Watcher Error: {e}", exc_info=True)

    def _get_mime_from_ext(self, ext):
        if ext in ['.jpg', '.jpeg']: return 'image/jpeg'
        if ext == '.png': return 'image/png'
        if ext == '.webp': return 'image/webp'
        if ext == '.pdf': return 'application/pdf'
        return None

    # =========================================================================
    # 3. Core Logic
    # =========================================================================
    async def _process_file_logic(self, filename: str, mime_type: str, file_bytes: bytes, source_type: str) -> discord.Embed:
        
        file_data = {"mime_type": mime_type, "data": file_bytes}
        
        # OCR + 分類用プロンプト
        prompt = [
            """
            このファイルは手書きの「デイリーノート (Daily Log Board)」または「メモノート (Memo Sheet)」をスキャンしたものです。
            内容を解析し、以下の情報を抽出してJSON形式で出力してください。

            # ノートの形式定義
            - **Daily Log Board**: 左上に「DATE」欄、中央に「TASKS」「NOTES」欄、右下に「REVIEW」欄があるレイアウト。
            - **Memo Sheet**: 上部に「DATE」欄があり、全体がグリッドの方眼紙レイアウト。
            - **PDFの場合**: 複数ページある場合は、全てのページの内容を統合して1つのcontentとしてまとめてください。

            # 出力フォーマット (JSON)
            {
                "date": "YYYY-MM-DD",
                "type": "daily_log" または "memo",
                "content": "string"
            }
            # content作成ルール
            - Daily Log: TASKS欄はMarkdownタスクリスト(- [ ])、NOTES/REVIEWは箇条書き。
            - Memo: Markdown箇条書き。
            """,
            file_data,
        ]

        try:
            response = await self.gemini_model.generate_content_async(prompt)
            result = json.loads(response.text)
        except Exception as e:
            logging.error(f"AI Parse Error: {e}")
            return discord.Embed(title="❌ Error", description=f"AI解析に失敗しました: {e}", color=discord.Color.red())

        extracted_date_str = result.get("date")
        note_type = result.get("type", "memo")
        transcribed_content = result.get("content", "")

        # 日付決定
        target_date = datetime.now(JST)
        if extracted_date_str:
            try:
                dt = datetime.strptime(extracted_date_str, '%Y-%m-%d')
                target_date = dt.replace(tzinfo=JST)
            except ValueError:
                pass
        target_date_str = target_date.strftime('%Y-%m-%d')
        display_time = datetime.now(JST).strftime('%H:%M')

        # --- 分岐処理: JournalCog連携か、通常保存か ---

        # Case A: デイリーログの場合 -> JournalCogに丸投げしてアドバイスをもらう
        if note_type == "daily_log":
            journal_cog = self.bot.get_cog("JournalCog")
            if journal_cog:
                # JournalCog側で保存もアドバイス生成も行う
                logging.info(f"Delegating daily_log to JournalCog: {target_date_str}")
                advice_embed = await journal_cog.process_handwritten_journal(transcribed_content, target_date_str)
                # Embedのフッターなどを少し調整
                advice_embed.set_footer(text=f"Filename: {filename} | {advice_embed.footer.text}")
                return advice_embed
            else:
                logging.error("JournalCog not found! Fallback to normal save.")
                # JournalCogがない場合のフォールバック（以下へ進む）

        # Case B: メモノートの場合（またはJournalCogがない場合） -> 自分で保存
        section_header = "## Handwritten Memos"
        content_to_add = f"- {display_time} (Memo Sheet)\n{transcribed_content}"

        with dropbox.Dropbox(
            oauth2_refresh_token=self.dropbox_refresh_token,
            app_key=self.dropbox_app_key,
            app_secret=self.dropbox_app_secret
        ) as dbx:
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{target_date_str}.md"
            try:
                _, res = dbx.files_download(daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# Daily Note {target_date_str}\n"
                else:
                    raise e

            new_content = update_section(daily_note_content, content_to_add, section_header)
            dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))

        embed = discord.Embed(
            title=f"📝 {target_date_str} のメモを取り込みました",
            description=f"**Source:** {source_type}\n**Type:** {note_type}\n\n{transcribed_content[:300]}...", 
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Filename: {filename} (Kept in folder)")
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(HandwrittenMemoCog(bot))