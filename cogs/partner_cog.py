import os
import discord
from discord.ext import commands
from google.genai import types
import logging
import datetime
import asyncio

from config import JST
from utils.obsidian_utils import update_section
from prompts import get_system_prompt, PROMPT_INTERIM_SUMMARY

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
        
        now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')
        system_prompt = get_system_prompt(self.user_name, now_str)
        
        prompt = f"{system_prompt}\n以下のデータを元にDiscordで話しかけて。\n【データ】\n{context_data}\n【指示】\n{instruction}"
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
            
            prompt = f"{PROMPT_INTERIM_SUMMARY}\n\n{logs}"
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
            system_prompt = get_system_prompt(self.user_name, now_str)

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
                        name="add_task", description="Google Tasks（ToDoリスト）に新しいタスクを追加する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"title": types.Schema(type=types.Type.STRING, description="タスク名")}, required=["title"])
                    ),
                    types.FunctionDeclaration(
                        name="complete_task", description="Google Tasksのタスクを完了（チェック）する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"keyword": types.Schema(type=types.Type.STRING, description="完了させたいタスク名の一部")}, required=["keyword"])
                    ),
                    types.FunctionDeclaration(
                        name="record_stock_trade",
                        description="ユーザーが株の銘柄について発言した際に、銘柄と理由を投資ノートに記録する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "code": types.Schema(type=types.Type.STRING, description="銘柄コード（例: 7203, AAPL）"),
                                "name": types.Schema(type=types.Type.STRING, description="銘柄名（例: トヨタ, Apple）"),
                                "memo": types.Schema(type=types.Type.STRING, description="エントリー理由や目標、ユーザーのコメントなど")
                            },
                            required=["code", "name", "memo"]
                        )
                    ),
                    types.FunctionDeclaration(
                        name="record_habit",
                        description="ユーザーが習慣（例：筋トレ、読書など）を完了したと報告した際に記録する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"habit_name": types.Schema(type=types.Type.STRING)}, required=["habit_name"])
                    ),
                    types.FunctionDeclaration(
                        name="delete_habit",
                        description="ユーザーが特定の習慣をリストから削除してほしいと頼んだ際に削除する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"habit_name": types.Schema(type=types.Type.STRING)}, required=["habit_name"])
                    ),
                    # ▼ 追加: Fitbitと英語クイズ用のツール
                    types.FunctionDeclaration(
                        name="report_sleep",
                        description="ユーザーが「昨日の睡眠データ教えて」「睡眠レポートを出して」など、朝の睡眠記録を確認したい時に呼び出す。"
                    ),
                    types.FunctionDeclaration(
                        name="report_health",
                        description="ユーザーが「今日の歩数は？」「Fitbitデータ教えて」「健康レポート出して」など、1日の活動データを確認したい時に呼び出す。"
                    ),
                    types.FunctionDeclaration(
                        name="give_english_quiz",
                        description="ユーザーが「英語のクイズ出して」「瞬間英作文やりたい」と頼んだ時に呼び出す。"
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
                        elif function_call.name == "record_stock_trade":
                            code = function_call.args["code"].upper()
                            name = function_call.args["name"]
                            memo = function_call.args["memo"]
                            stock_cog = self.bot.get_cog("StockCog")
                            
                            if stock_cog:
                                file_id = await stock_cog._find_stock_note_id(code)
                                if file_id:
                                    await stock_cog._append_memo_to_note(file_id, memo)
                                    tool_result = f"既存の銘柄ノート（{name}）にメモを追記しました。"
                                else:
                                    filename = f"{code}_{name}.md"
                                    now = datetime.datetime.now(JST)
                                    note_content = f"---\ncode: \"{code}\"\nname: \"{name}\"\nstatus: \"Watching\"\ncreated: {now.isoformat()}\ntags: [stock, investment]\n---\n# {name} ({code})\n## Logs\n- {now.strftime('%Y-%m-%d %H:%M')} {memo}\n## Review\n"
                                    await stock_cog._save_file(filename, note_content)
                                    tool_result = f"新しい銘柄ノート（{name}）を作成し、メモを記録しました。"
                            else:
                                tool_result = "システムエラー: StockCogが見つかりません。"
                        elif function_call.name == "record_habit":
                            habit_cog = self.bot.get_cog("HabitCog")
                            if habit_cog:
                                tool_result = await habit_cog.complete_habit(function_call.args["habit_name"])
                            else:
                                tool_result = "システムエラー: HabitCogが見つかりません。"
                        elif function_call.name == "delete_habit":
                            habit_cog = self.bot.get_cog("HabitCog")
                            if habit_cog:
                                tool_result = await habit_cog.delete_habit(function_call.args["habit_name"])
                            else:
                                tool_result = "システムエラー: HabitCogが見つかりません。"
                                
                        # ▼ 追加: Fitbitと英語クイズの実行ロジック
                        elif function_call.name == "report_sleep":
                            fitbit_cog = self.bot.get_cog("FitbitCog")
                            if fitbit_cog:
                                asyncio.create_task(fitbit_cog.sleep_report())
                                tool_result = "睡眠レポートの取得と解析を開始しました。別メッセージとしてすぐに送信されます。"
                            else:
                                tool_result = "システムエラー: FitbitCogが見つかりません。"
                        elif function_call.name == "report_health":
                            fitbit_cog = self.bot.get_cog("FitbitCog")
                            if fitbit_cog:
                                asyncio.create_task(fitbit_cog.full_health_report())
                                tool_result = "健康レポートの取得と解析を開始しました。別メッセージとしてすぐに送信されます。"
                            else:
                                tool_result = "システムエラー: FitbitCogが見つかりません。"
                        elif function_call.name == "give_english_quiz":
                            en_cog = self.bot.get_cog("EnglishLearningCog")
                            if en_cog:
                                asyncio.create_task(en_cog.daily_english_quiz())
                                tool_result = "英語クイズの生成を開始しました。別メッセージとしてすぐに送信されます。"
                            else:
                                tool_result = "システムエラー: EnglishLearningCogが見つかりません。"

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