import os
import discord
from discord.ext import commands
import datetime
import aiohttp
import re
import asyncio
import logging

from config import JST
from utils.obsidian_utils import update_section  # ★追加: ノートへの追記用
from prompts import (
    PROMPT_BOOK_SUMMARY, 
    PROMPT_BOOK_AUTHOR, 
    PROMPT_BOOK_QUIZ, 
    PROMPT_BOOK_ACTION,
    PROMPT_BOOK_CHAT
)

class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # 1. メインのメモチャンネルでのAmazonリンク検知とスレッド作成
        if message.channel.id == self.memo_channel_id:
            text = message.content.strip()
            amazon_pattern = r'(https?://(?:www\.)?(?:amazon\.co\.jp|amzn\.to)[^\s]+)'
            match = re.search(amazon_pattern, text)
            
            if match:
                url = match.group(1)
                await message.add_reaction("📚")
                asyncio.create_task(self.process_book_link(message, url))
            return

        # 2. 読書スレッド（名前が「📖」から始まるスレッド）内での自然な会話処理
        if isinstance(message.channel, discord.Thread) and message.channel.name.startswith("📖 "):
            text = message.content.strip()
            book_title = message.channel.name[2:].strip()

            # Obsidian（Drive）から読書ノートの内容を全読み込みする
            note_content = await self._read_book_note(book_title)
            if not note_content:
                return # ノートが見つからない場合は何もしない

            # 会話内容（キーワード）による機能の自動振り分け
            if "要約" in text or "まとめ" in text:
                async with message.channel.typing():
                    result = await self.perform_summarize(book_title)
                    await message.reply(result)
                return

            prompt_template = None
            if any(keyword in text for keyword in ["クイズ", "問題", "テスト"]):
                prompt_template = PROMPT_BOOK_QUIZ
            elif any(keyword in text for keyword in ["著者", "作者", "ペルソナ"]):
                prompt_template = PROMPT_BOOK_AUTHOR
            elif any(keyword in text for keyword in ["アクション", "行動", "日常", "活か"]):
                prompt_template = PROMPT_BOOK_ACTION
            else:
                prompt_template = f"{PROMPT_BOOK_CHAT}\n\n【私の発言・質問】\n{text}"

            # Gemini APIを呼び出して返信
            prompt = f"{prompt_template}\n\n【読書ノートのテキスト】\n{note_content}"
            async with message.channel.typing():
                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro", contents=prompt
                    )
                    ai_reply = response.text.strip()
                    await message.reply(ai_reply)
                    
                    # ★追加: ユーザーの発言とAIの回答をセットにしてObsidianに記録する
                    await self._append_qa_to_log(book_title, text, ai_reply)
                    
                except Exception as e:
                    logging.error(f"Book Chat Error: {e}")
                    await message.reply("ごめんね、AIの処理中にエラーが発生しちゃった💦")

    # ★追加: 読書ログに会話を追記するメソッド
    async def _append_qa_to_log(self, book_title: str, user_text: str, ai_text: str):
        service = self.drive_service.get_service()
        if not service: return
        
        book_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "BookNotes")
        if not book_folder_id: return
        
        file_name = f"{book_title}.md"
        f_id = await self.drive_service.find_file(service, book_folder_id, file_name)
        if not f_id: return
        
        content = await self.drive_service.read_text_file(service, f_id)
        if not content: return
        
        time_str = datetime.datetime.now(JST).strftime('%H:%M')
        
        # 改行があってもリスト表示が崩れないようにインデントを調整
        user_formatted = user_text.replace('\n', '\n    ')
        ai_formatted = ai_text.replace('\n', '\n        ')
        
        log_entry = f"- **{time_str}** 👤 {user_formatted}\n    - 🤖 {ai_formatted}"
        
        new_content = update_section(content, log_entry, "## 📖 Reading Log")
        await self.drive_service.update_text(service, f_id, new_content)

    async def process_book_link(self, message: discord.Message, url: str):
        title = "名称未設定の本"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    html = await resp.text()
                    match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
                    if match:
                        title = match.group(1).replace("Amazon.co.jp:", "").replace("Amazon.co.jp :", "").strip()
        except Exception:
            pass 

        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]

        service = self.drive_service.get_service()
        if service:
            book_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "BookNotes")
            if not book_folder_id:
                book_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "BookNotes")

            file_name = f"{safe_title}.md"
            f_id = await self.drive_service.find_file(service, book_folder_id, file_name)
            if not f_id:
                now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
                content = f"---\ntitle: {safe_title}\ndate: {now_str}\ntags: [book]\n---\n\n# {safe_title}\n\n## 📝 Summary & Learning\n\n\n## 📖 Reading Log\n\n\n## 📄 Book Text\nここに書籍のテキストデータを貼り付けてね！\n"
                await self.drive_service.upload_text(service, book_folder_id, file_name, content)

        msg = await message.reply(f"📚 『{safe_title}』の読書ノートを作成したよ！\nこのスレッドでメモや感想を書いてね。")
        thread = await msg.create_thread(name=f"📖 {safe_title}", auto_archive_duration=10080)
        await thread.send("ここが読書ルームだよ！\nノートの一番下にある「## 📄 Book Text」の所に本のテキストを貼り付けたら、私に「要約して」「クイズ出して」って話しかけてみてね！")

    async def _read_book_note(self, book_title: str) -> str:
        file_name = f"{book_title}.md"
        service = self.drive_service.get_service()
        if not service: return None
        
        book_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "BookNotes")
        if not book_folder_id: return None
        
        f_id = await self.drive_service.find_file(service, book_folder_id, file_name)
        if not f_id: return None
        
        return await self.drive_service.read_text_file(service, f_id)

    async def perform_summarize(self, book_title: str) -> str:
        service = self.drive_service.get_service()
        if not service: return "Google Driveに接続できなかったよ💦"
        
        file_name = f"{book_title}.md"
        book_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "BookNotes")
        if not book_folder_id: return "BookNotesフォルダが見つからないみたい。"
        
        f_id = await self.drive_service.find_file(service, book_folder_id, file_name)
        if not f_id: return f"ノート（{file_name}）が見つからないよ。"
        
        content = await self.drive_service.read_text_file(service, f_id)
        if not content: return "ノートの読み込みに失敗したよ。"
        
        log_heading = "## 📖 Reading Log"
        summary_heading = "## 📝 Summary & Learning"
        text_heading = "## 📄 Book Text"
        
        if log_heading not in content or summary_heading not in content:
            return "ノートの形式が正しくないみたい（見出しが見つかりません）。"
            
        parts = content.split(log_heading)
        top_half = parts[0].split(summary_heading)[0] 
        raw_log_section = parts[1]
        
        if text_heading in raw_log_section:
            log_parts = raw_log_section.split(text_heading)
            raw_log = log_parts[0].strip()
            book_text_section = f"\n\n{text_heading}{log_parts[1]}"
        else:
            raw_log = raw_log_section.strip()
            book_text_section = ""
        
        if not raw_log: return "まだ読書ログがないみたいだよ！"
        
        prompt = f"{PROMPT_BOOK_SUMMARY}\n\n【読書ログ】\n{raw_log}"
        try:
            response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
            summary_text = response.text.strip()
            new_content = f"{top_half}{summary_heading}\n{summary_text}\n\n\n{log_heading}\n{raw_log}\n{book_text_section}"
            await self.drive_service.update_text(service, f_id, new_content)
            return f"✨ 『{book_title}』の読書ノートを綺麗に要約して保存したよ！"
        except Exception as e:
            return f"AIの要約中にエラーが発生したよ💦: {e}"

async def setup(bot: commands.Bot):
    await bot.add_cog(BookCog(bot))