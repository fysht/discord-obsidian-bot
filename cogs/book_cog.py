import os
import discord
from discord.ext import commands
import datetime
import aiohttp
import re
import asyncio
import logging
from google.genai import types

from config import JST
from utils.obsidian_utils import update_section
from prompts import PROMPT_BOOK_SUMMARY, PROMPT_BOOK_CHAT

class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.book_channel_id = int(os.getenv("BOOK_CHANNEL_ID", 0))
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0)) # メモチャンネルのID
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client
        self.notified_today = set() # 1日1回の通知管理用

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # ==========================================
        # 1. 読書チャンネル（親）での発言からスレッドを自動作成
        # ==========================================
        if message.channel.id == self.book_channel_id and not isinstance(message.channel, discord.Thread):
            text = message.content.strip()

            # パターンA: Amazonリンクが直接貼られた場合
            amazon_pattern = r'(https?://(?:www\.)?(?:amazon\.co\.jp|amzn\.to)[^\s]+)'
            match = re.search(amazon_pattern, text)
            if match:
                url = match.group(1)
                await message.add_reaction("📚")
                asyncio.create_task(self.process_book_link(message, url))
                return

            # パターンB: 自然言語で「〜の本を読むよ」と言われた場合
            extract_tool = types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="create_book_thread",
                        description="ユーザーの発言からこれから読む本のタイトルを抽出し、読書用のスレッドを作成します。",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "book_title": {
                                    "type": "STRING",
                                    "description": "読み始める本のタイトル"
                                }
                            },
                            "required": ["book_title"]
                        }
                    )
                ]
            )

            prompt = f"ユーザーが新しく本を読み始めようとしています。発言から本のタイトルを抽出してツールを呼び出してください。ただの雑談や本のタイトルが含まれていない場合はツールを呼ばずに短く返信してください。\n\n【ユーザーの発言】\n{text}"

            async with message.channel.typing():
                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro",
                        contents=prompt,
                        config=types.GenerateContentConfig(tools=[extract_tool])
                    )

                    if response.function_calls:
                        for fc in response.function_calls:
                            if fc.name == "create_book_thread":
                                book_title = fc.args.get("book_title", "名称未設定の本")
                                await self._create_book_thread_manual(message, book_title)
                                return
                    
                    if response.text:
                        await message.reply(response.text.strip())

                except Exception as e:
                    logging.error(f"Book Thread Creation Error: {e}")
                    await message.reply("ごめんね、AIの処理中にエラーが発生しちゃった💦")
            return


        # ==========================================
        # 2. 読書スレッド内でのチャット処理
        # ==========================================
        if isinstance(message.channel, discord.Thread) and message.channel.name.startswith("📖 "):
            text = message.content.strip()
            book_title = message.channel.name[2:].strip()

            note_content = await self._read_book_note(book_title)
            if not note_content:
                return 

            prompt = f"{PROMPT_BOOK_CHAT}\n\n【読書ノートのテキスト】\n{note_content}\n\n【私の発言】\n{text}"

            summarize_tool = types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="summarize_book_note",
                        description="これまでの読書ログを整理し、Obsidianの読書ノートを更新・上書き保存します。ユーザーが「ノートを更新して」と明示した、または確認に対してYESと答えた場合のみ使用します。"
                    )
                ]
            )

            async with message.channel.typing():
                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro", 
                        contents=prompt,
                        config=types.GenerateContentConfig(tools=[summarize_tool])
                    )
                    
                    if response.function_calls:
                        for fc in response.function_calls:
                            if fc.name == "summarize_book_note":
                                result_msg = await self.perform_summarize(book_title)
                                await message.reply(result_msg)
                                return

                    ai_reply = response.text.strip()
                    await message.reply(ai_reply)
                    
                    # ログの追記
                    await self._append_qa_to_log(book_title, text, ai_reply)
                    
                    # 1日1回だけメインのメモチャンネルに自動記録を送信する
                    now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
                    cache_key = f"{now_str}_{book_title}"
                    if cache_key not in self.notified_today:
                        self.notified_today.add(cache_key)
                        if self.memo_channel_id != 0:
                            memo_channel = self.bot.get_channel(self.memo_channel_id)
                            if memo_channel:
                                await memo_channel.send(f"【自動記録】📚 本日、『{book_title}』の読書ノートで対話・学習を行いました。")

                except Exception as e:
                    logging.error(f"Book Chat Error: {e}")
                    await message.reply("ごめんね、AIの処理中にエラーが発生しちゃった💦")

    # ==========================================
    # 内部処理メソッド
    # ==========================================
    async def _create_book_thread_manual(self, message: discord.Message, title: str):
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
        await thread.send("ここが読書ルームだよ！\nノートの一番下にある「## 📄 Book Text」の所に本のテキストを貼り付けたら、自由に話しかけてね！")

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
        
        time_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')
        
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

        await self._create_book_thread_manual(message, title)

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