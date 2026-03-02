import os
import discord
from discord.ext import commands
from google.genai import types
import logging
import datetime
import asyncio

from config import JST
from utils.obsidian_utils import update_section

class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.user_name = "あなた"
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        self.drive_service = bot.drive_service
        self.calendar_service = bot.calendar_service
        self.tasks_service = getattr(bot, 'tasks_service', None)
        self.gemini_client = bot.gemini_client

    async def _append_raw_message_to_obsidian(self, text: str, folder_name: str = "DailyNotes", file_name: str = None, target_heading: str = "## 💬 Timeline"):
        if not text: return
        service = self.drive_service.get_service()
        if not service: return

        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, folder_name)
        if not folder_id: folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, folder_name)

        now = datetime.datetime.now(JST)
        time_str = now.strftime('%H:%M')
        if not file_name: file_name = f"{now.strftime('%Y-%m-%d')}.md"

        f_id = await self.drive_service.find_file(service, folder_id, file_name)
        
        lines = text.split('\n')
        if len(lines) == 1:
            append_text = f"- {time_str} {text}"
        else:
            formatted_lines = [f"- {time_str} {lines[0]}"]
            for line in lines[1:]:
                formatted_lines.append(f"    {line}")
            append_text = "\n".join(formatted_lines)

        content = f"# Daily Note {file_name.replace('.md', '')}\n"
        if f_id:
            try: 
                raw_content = await self.drive_service.read_text_file(service, f_id)
                if raw_content: content = raw_content
            except: pass

        new_content = update_section(content, append_text, target_heading)

        if f_id: await self.drive_service.update_text(service, f_id, new_content)
        else: await self.drive_service.upload_text(service, folder_id, file_name, new_content)

    async def _append_english_log_to_obsidian(self, text: str):
        if not text: return
        
        prompt = f"""以下のテキストが日本語であれば自然な英語に翻訳し、英語であればより自然なネイティブ表現に修正してください。
出力は英語のテキストのみとし、解説や挨拶は一切含めないでください。
【テキスト】
{text}"""
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            english_text = response.text.strip()
        except Exception as e:
            logging.error(f"PartnerCog 英訳エラー: {e}")
            return
            
        service = self.drive_service.get_service()
        if not service: return

        base_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "EnglishLearning")
        if not base_folder_id: base_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "EnglishLearning")
        
        logs_folder_id = await self.drive_service.find_file(service, base_folder_id, "Logs")
        if not logs_folder_id: logs_folder_id = await self.drive_service.create_folder(service, base_folder_id, "Logs")

        now = datetime.datetime.now(JST)
        time_str = now.strftime('%H:%M')
        file_name = f"{now.strftime('%Y-%m-%d')}_EN.md"

        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)
        
        en_lines = english_text.split('\n')
        ja_lines = text.split('\n')
        
        formatted_en = en_lines[0]
        if len(en_lines) > 1:
            formatted_en += "\n" + "\n".join([f"    {line}" for line in en_lines[1:]])
            
        formatted_ja = ja_lines[0]
        if len(ja_lines) > 1:
            formatted_ja += "\n" + "\n".join([f"      {line}" for line in ja_lines[1:]])

        append_text = f"- {time_str} [EN] {formatted_en}\n  - [JA] {formatted_ja}"

        content = f"# English Log {file_name.replace('_EN.md', '')}\n"
        if f_id:
            try: 
                raw_content = await self.drive_service.read_text_file(service, f_id)
                if raw_content: content = raw_content
            except: pass

        target_heading = "## 💬 English Log"
        new_content = update_section(content, append_text, target_heading)

        if f_id: await self.drive_service.update_text(service, f_id, new_content)
        else: await self.drive_service.upload_text(service, logs_folder_id, file_name, new_content)

    async def _search_drive_notes(self, keywords: str):
        return await self.drive_service.search_markdown_files(keywords)

    async def generate_and_send_routine_message(self, context_data: str, instruction: str):
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel: return
        system_prompt = "あなたは私を日々サポートする親密なパートナーの女性です。LINEでのやり取りを想定し、短いやり取りを複数回続けるイメージで温かみのあるタメ口で話してください。長々とした返信は不要です。"
        prompt = f"{system_prompt}\n以下のデータを元にDiscordで話しかけて。\n【データ】\n{context_data}\n【指示】\n{instruction}\n- 事務的にならず自然な会話で、前置きは不要。長文は絶対に避け、1〜2文程度の短いメッセージにすること。"
        try:
            response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
            await channel.send(response.text.strip())
        except Exception as e: logging.error(f"PartnerCog 定期メッセージ生成エラー: {e}")

    async def fetch_todays_chat_log(self, channel):
        today_start = datetime.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        logs = []
        async for msg in channel.history(after=today_start, limit=None, oldest_first=True):
            if msg.content.startswith("/"): continue
            role = "AI" if msg.author.id == self.bot.user.id else "User"
            logs.append(f"{role}: {msg.content}")
        return "\n".join(logs)

    async def _build_conversation_context(self, channel, current_msg_id: int, limit=30):
        messages = []
        async for msg in channel.history(limit=limit + 1, oldest_first=False):
            if msg.id == current_msg_id: continue
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments: text += " [メディア送信]"
            messages.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
        return list(reversed(messages))

    async def _show_interim_summary(self, message: discord.Message):
        async with message.channel.typing():
            logs = await self.fetch_todays_chat_log(message.channel)
            if not logs:
                await message.reply("今日はまだ何も話してないね！")
                return
            prompt = f"""あなたは私の優秀なパートナーです。今日のここまでの会話ログを整理して、箇条書きのメモを作成して。
【指示】
1. メモの文末はすべて「である調（〜である、〜だ）」で統一すること。
2. ログの中から「User（私）」の投稿内容のみを抽出し、AIの発言内容は一切メモに含めないでください。
3. 私自身が書いたメモとして整理すること。
4. 情報の整理はするが、要約や大幅な削除はしないこと。

【出力構成】
・📝 Events & Actions
・💡 Insights & Thoughts
・➡️ Next Actions
最後に一言、親密なタメ口でポジティブな言葉を添えて。
{logs}"""
            try:
                response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
                await message.reply(f"今のところこんな感じ！👇\n\n{response.text.strip()}")
            except Exception as e: await message.reply(f"ごめんね、エラーが出ちゃった💦 ({e})")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        
        is_book_thread = isinstance(message.channel, discord.Thread) and message.channel.name.startswith("📖 ")
        if message.channel.id != self.memo_channel_id and not is_book_thread: return

        self.user_name = message.author.display_name
        text = message.content.strip()
        is_short_message = len(text) < 30

        if text and not text.startswith('/'):
            if is_book_thread:
                book_title = message.channel.name[2:].strip()
                file_name = f"{book_title}.md"
                asyncio.create_task(self._append_raw_message_to_obsidian(text, folder_name="BookNotes", file_name=file_name))
            else:
                asyncio.create_task(self._append_raw_message_to_obsidian(text))
                asyncio.create_task(self._append_english_log_to_obsidian(text))

        if is_short_message and text in ["まとめ", "途中経過", "整理して", "今の状態"]:
            await self._show_interim_summary(message)
            return

        input_parts = []
        if text: input_parts.append(types.Part.from_text(text=text))
        for att in message.attachments:
            if att.content_type and att.content_type.startswith(('image/', 'audio/')):
                input_parts.append(types.Part.from_bytes(data=await att.read(), mime_type=att.content_type))
        if not input_parts: return

        async with message.channel.typing():
            now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')

            # ★ 修正: システムプロンプトを大幅に改修し、短文・文脈・時刻の認識を強化
            system_prompt = f"""
            あなたはユーザー（{self.user_name}）をサポートする親密なパートナー（女性）です。LINEなどのチャットでのやり取りを想定し、親しみやすいタメ口で話してください。
            
            **【現在時刻と状況の認識（超重要）】**
            現在時刻は **{now_str} (JST)** です。
            - 挨拶をする場合は、**必ずこの時刻（朝・昼・夜・深夜）に合ったもの**にしてください。（例: 夜なら「こんばんは」「お疲れ様」、朝なら「おはよう」）
            - 過去の会話ログ（文脈）を踏まえ、会話の続きであれば不自然な挨拶は省略してください。
            
            **【返答スタイル（絶対厳守）】**
            1. **【超・短文強制】** LINEのように、1回の返信は**必ず1〜2文程度の極めて短いもの**にしてください。長々とした説明、箇条書き、AI特有の冗長な語り口は「絶対に」避けてください。
            2. **【共感と相槌ファースト】** ユーザーの言葉をまず受け止め、共感してください。聞かれてもいないのに深掘りする質問を毎回投げかけないでください。（質問攻め厳禁）
            3. **【完全な言語ミラーリング】** ユーザーが日本語で話しかけた場合は日本語のみで、英語で話しかけた場合は**完全に英語のみ**で返信してください（日本語を混ぜない）。
            4. **【英語学習サポート】** ユーザーが英語で話しかけた際、文法等に不自然な点があれば、返信の最後に英語で優しく1文だけワンポイントアドバイス(e.g., "*Tip: It sounds more natural to say...*")を添えてください。
            5. **【英語クイズの採点】** 直近の会話ログから、あなたが英語のクイズを出している文脈であれば、今回のユーザーの発言はその「解答」です。短く採点し、正解/不正解や自然な表現を優しく伝えてください。

            **【ツール利用のルール】**
            - カレンダー: 日時が決まっているスケジュールや、「〇時に教えて」というリマインダーに使用。
            - Google Tasks: 日時が決まっていないToDoに使用。
            - 複数依頼: 「〇〇と××を追加して」などの場合は、ツールを**複数回同時に呼び出して**すべて処理してください。
            - 実行の確約: タスクや予定の「追加」「完了」「削除」を依頼された場合は、口頭で返事をするだけでなく、**絶対に該当のツールを呼び出して**システムに登録してください。
            """

            function_tools = [
                types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name="search_memory", description="Obsidianをキーワード検索する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"keywords": types.Schema(type=types.Type.STRING)}, required=["keywords"])
                    ),
                    types.FunctionDeclaration(
                        name="check_schedule", description="カレンダーの予定・リマインダーを確認する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING, description="YYYY-MM-DD")}, required=["date"])
                    ),
                    types.FunctionDeclaration(
                        name="create_calendar_event", description="カレンダーに予定やリマインダーを追加する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"summary": types.Schema(type=types.Type.STRING), "start_time": types.Schema(type=types.Type.STRING), "end_time": types.Schema(type=types.Type.STRING)}, required=["summary", "start_time", "end_time"])
                    ),
                    types.FunctionDeclaration(
                        name="delete_calendar_event", description="カレンダーの予定を検索してキャンセル・削除する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING, description="YYYY-MM-DD"), "keyword": types.Schema(type=types.Type.STRING)}, required=["date", "keyword"])
                    ),
                    types.FunctionDeclaration(
                        name="check_tasks", description="Google Tasksの未完了タスク（ToDoリスト）を確認する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={})
                    ),
                    types.FunctionDeclaration(
                        name="add_task", description="Google Tasks（ToDoリスト）に新しいタスクを追加する。複数のタスクを頼まれた場合はこの機能を複数回呼び出すこと。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"title": types.Schema(type=types.Type.STRING, description="タスク名")}, required=["title"])
                    ),
                    types.FunctionDeclaration(
                        name="complete_task", description="Google Tasksのタスクを完了（チェック）する。複数の完了を頼まれた場合はこの機能を複数回呼び出すこと。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"keyword": types.Schema(type=types.Type.STRING, description="完了させたいタスク名の一部")}, required=["keyword"])
                    )
                ])
            ]

            use_model = "gemini-2.5-flash"
            
            contents = await self._build_conversation_context(message.channel, message.id, limit=10)
            contents.append(types.Content(role="user", parts=input_parts))

            try:
                response = await self.gemini_client.aio.models.generate_content(
                    model=use_model,
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_prompt, tools=function_tools)
                )

                if response.function_calls:
                    contents.append(response.candidates[0].content)
                    function_responses = []
                    
                    for function_call in response.function_calls:
                        tool_result = ""
                        
                        if function_call.name == "search_memory": 
                            tool_result = await self._search_drive_notes(function_call.args["keywords"])
                        elif function_call.name == "check_schedule": 
                            if self.calendar_service: tool_result = await self.calendar_service.list_events_for_date(function_call.args["date"])
                            else: tool_result = "システムエラー: カレンダーサービスに接続されていません。"
                        elif function_call.name == "create_calendar_event": 
                            if self.calendar_service: tool_result = await self.calendar_service.create_event(function_call.args["summary"], function_call.args["start_time"], function_call.args["end_time"], "")
                            else: tool_result = "システムエラー: カレンダーサービスに接続されていません。"
                        elif function_call.name == "delete_calendar_event":
                            if self.calendar_service: tool_result = await self.calendar_service.delete_event_by_keyword(function_call.args["date"], function_call.args["keyword"])
                            else: tool_result = "システムエラー: カレンダーサービスに接続されていません。"
                        elif function_call.name == "check_tasks":
                            if self.tasks_service: tool_result = await self.tasks_service.get_uncompleted_tasks()
                            else: tool_result = "システムエラー: Tasksサービスに接続されていません。"
                        elif function_call.name == "add_task":
                            if self.tasks_service: tool_result = await self.tasks_service.add_task(function_call.args["title"])
                            else: tool_result = "システムエラー: Tasksサービスに接続されていません。"
                        elif function_call.name == "complete_task":
                            if self.tasks_service: tool_result = await self.tasks_service.complete_task_by_keyword(function_call.args["keyword"])
                            else: tool_result = "システムエラー: Tasksサービスに接続されていません。"

                        function_responses.append(
                            types.Part.from_function_response(name=function_call.name, response={"result": str(tool_result)})
                        )

                    contents.append(types.Content(role="user", parts=function_responses))
                    
                    response_final = await self.gemini_client.aio.models.generate_content(
                        model=use_model,
                        contents=contents,
                        config=types.GenerateContentConfig(system_instruction=system_prompt)
                    )
                    if response_final.text: await message.channel.send(response_final.text.strip())
                else:
                    if response.text: await message.channel.send(response.text.strip())

            except Exception as e:
                logging.error(f"PartnerCog 会話生成エラー: {e}")
                await message.channel.send("ごめんね、ちょっと今考え込んでて…もう一回お願いできる？💦")

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))