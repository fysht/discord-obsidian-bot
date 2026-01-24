import discord
from discord.ext import commands
import os
import aiohttp
import asyncio
import json
import logging
import re
from datetime import datetime
import google.generativeai as genai
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError

# 共通関数をインポート
from utils.obsidian_utils import update_section

# Gemini APIの設定
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# 対応するMIMEタイプ
SUPPORTED_MIME_TYPES = {
    'application/pdf': 'application/pdf',
    'image/png': 'image/png',
    'image/jpeg': 'image/jpeg',
    'image/webp': 'image/webp',
    'image/heic': 'image/heic',
}

class HandwrittenMemo(commands.Cog):
    """手書きメモ(PDF/画像)を解析し、日付特定・内容整理を行ってObsidianに保存するCog"""

    def __init__(self, bot):
        self.bot = bot
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.attachment_folder = "99_Attachments"
        
    async def analyze_memo_content(self, file_bytes, mime_type):
        """
        Geminiを使用して、手書きメモから「日付」と「整理された内容」を抽出する
        """
        try:
            model = genai.GenerativeModel('gemini-2.5-pro') 
            
            prompt = (
                "あなたは優秀な秘書です。添付された手書きメモ（またはスキャンPDF）を読み取り、以下の処理を行ってください。\n\n"
                "1. **日付の特定**: メモ内に記載されている日付を探し、`YYYY-MM-DD` 形式（例: 2026-01-24）で抽出してください。\n"
                "   - 日付が見つからない場合は、今日の日付を使用してください。\n"
                "2. **内容の整理**: メモの内容を読み取り、単なる文字起こしではなく、文脈を理解して**重要なポイントを箇条書き（Markdown）**でまとめてください。\n"
                "   - 雑なメモ書きであっても、意味が通るように補完・整理してください。\n"
                "   - 音声メモの要約のように、簡潔かつ明確なリスト形式にしてください。\n\n"
                "**出力形式 (JSONのみ):**\n"
                "```json\n"
                "{\n"
                "  \"date\": \"YYYY-MM-DD\",\n"
                "  \"content\": \"- 要点1\\n- 要点2...\"\n"
                "}\n"
                "```"
            )

            file_part = {"mime_type": mime_type, "data": file_bytes}
            
            response = await model.generate_content_async([prompt, file_part])
            response_text = response.text.strip()
            
            # JSONブロックの抽出
            json_match = re.search(r'```json\s*({.*?})\s*```', response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_str = response_text

            result = json.loads(json_str)
            return result.get("date"), result.get("content")

        except Exception as e:
            logging.error(f"Gemini Analysis Error: {e}")
            return None, None

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        
        # 添付ファイルがあるかチェック
        if message.attachments:
            for attachment in message.attachments:
                # PDFまたは画像ファイルのみ処理
                if any(attachment.content_type.startswith(t) for t in ['image/', 'application/pdf']):
                    await self.process_scanned_file(message, attachment)
                    return # 1メッセージにつき1ファイル処理（またはループで複数処理も可）

    async def process_scanned_file(self, message, attachment):
        """ファイルをダウンロードし、解析・保存を行う"""
        processing_msg = await message.channel.send("🔄 手書きメモを解析中...")
        
        try:
            # 1. ファイルのダウンロード
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    if resp.status != 200:
                        raise Exception("ダウンロードに失敗しました")
                    file_bytes = await resp.read()
                    mime_type = attachment.content_type

            # 2. Geminiによる解析 (日付と内容の抽出)
            extracted_date_str, organized_content = await self.analyze_memo_content(file_bytes, mime_type)
            
            if not extracted_date_str or not organized_content:
                await processing_msg.edit(content="❌ メモの解析に失敗しました。")
                return

            # 日付フォーマットの再確認
            try:
                target_date = datetime.strptime(extracted_date_str, '%Y-%m-%d')
                date_str = target_date.strftime('%Y-%m-%d')
            except ValueError:
                # 抽出日付が不正な場合は投稿日を採用
                target_date = datetime.now()
                date_str = target_date.strftime('%Y-%m-%d')
                organized_content = f"(⚠️ 日付不明のため今日の日付に保存)\n{organized_content}"

            # 3. Dropboxへの保存処理
            stock_cog = self.bot.get_cog("StockCog") # StockCogからDropboxインスタンスを借りる(既存コード踏襲)
            dbx = getattr(stock_cog, "dbx", None)

            if not dbx:
                await processing_msg.edit(content="❌ Dropboxクライアントが利用できません。")
                return

            # A. 元ファイルの保存 (Attachmentsフォルダ)
            original_filename = attachment.filename
            file_ext = os.path.splitext(original_filename)[1]
            saved_filename = f"Scan_{date_str}_{datetime.now().strftime('%H%M%S')}{file_ext}"
            save_path = f"{self.dropbox_vault_path}/{self.attachment_folder}/{saved_filename}"

            try:
                await asyncio.to_thread(
                    dbx.files_upload, 
                    file_bytes, 
                    save_path, 
                    mode=WriteMode('add')
                )
            except Exception as e:
                logging.error(f"File Save Error: {e}")
                # ファイル保存に失敗してもテキスト保存は続行

            # B. Obsidianノートへのテキスト追記
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
            
            # ノートのダウンロード (なければ空)
            try:
                _, res = await asyncio.to_thread(dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                 # ファイルがない場合は新規作成扱い
                current_content = ""

            # 追記内容の作成
            # 画像リンク + 整理されたテキスト
            timestamp_header = datetime.now().strftime('%H:%M')
            content_to_add = (
                f"- {timestamp_header} (Handwritten)\n"
                f"\t- ![[{self.attachment_folder}/{saved_filename}]]\n" # 元ファイルへのリンク
            )
            # AIが生成したテキストをインデントして追加
            for line in organized_content.split('\n'):
                content_to_add += f"\t- {line}\n"

            # update_sectionを使って追記
            section_header = "## Handwritten Memos" # または "## Memo"
            new_note_content = update_section(current_content, content_to_add, section_header)

            # アップロード (上書き)
            await asyncio.to_thread(
                dbx.files_upload, 
                new_note_content.encode('utf-8'), 
                daily_note_path, 
                mode=WriteMode('overwrite')
            )

            # 4. 完了通知
            embed = discord.Embed(title=f"📝 メモを保存しました ({date_str})", description=organized_content, color=discord.Color.green())
            embed.set_footer(text=f"Saved to {daily_note_path}")
            await processing_msg.edit(content="", embed=embed)
            await message.add_reaction("✅")

        except Exception as e:
            logging.error(f"Process Error: {e}", exc_info=True)
            await processing_msg.edit(content=f"❌ エラーが発生しました: {e}")

async def setup(bot):
    await bot.add_cog(HandwrittenMemo(bot))