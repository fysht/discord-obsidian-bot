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
        self.user_name = "ゆうすけ"
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        self.drive_service = bot.drive_service
        self.calendar_service = bot.calendar_service
        self.tasks_service = getattr(bot, "tasks_service", None)
        self.gemini_client = bot.gemini_client

        self.user_manual_cache = ""
        self.last_manual_fetch = None

    async def _get_user_manual(self):
        now = datetime.datetime.now()
        if (
            self.last_manual_fetch
            and (now - self.last_manual_fetch).total_seconds() < 3600
        ):
            return self.user_manual_cache

        service = self.drive_service.get_service()
        if not service:
            return ""
        try:
            folder_id = await self.drive_service.find_file(
                service, self.drive_folder_id, ".bot"
            )
            if not folder_id:
                return ""
            file_id = await self.drive_service.find_file(
                service, folder_id, "UserManual.md"
            )
            if file_id:
                content = await self.drive_service.read_text_file(service, file_id)
                self.user_manual_cache = content
                self.last_manual_fetch = now
                return content
        except Exception as e:
            logging.error(f"UserManual 読み込みエラー: {e}")
        return ""

    async def _append_raw_message_to_obsidian(
        self,
        text: str,
        folder_name: str = "DailyNotes",
        file_name: str = None,
        target_heading: str = "## 💬 Timeline",
    ):
        if not text:
            return
        service = self.drive_service.get_service()
        if not service:
            return

        folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, folder_name
        )
        if not folder_id:
            folder_id = await self.drive_service.create_folder(
                service, self.drive_folder_id, folder_name
            )

        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        if not file_name:
            file_name = f"{now.strftime('%Y-%m-%d')}.md"

        f_id = await self.drive_service.find_file(service, folder_id, file_name)

        lines = text.split("\n")
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
                if raw_content:
                    content = raw_content
            except Exception:
                pass

        new_content = update_section(content, append_text, target_heading)

        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(
                service, folder_id, file_name, new_content
            )

    async def _append_english_log_to_obsidian(self, text: str):
        if not text:
            return
        prompt = f"""以下のテキストが日本語であれば自然な英語に翻訳し、英語であればより自然なネイティブ表現に修正してください。
出力は英語のテキストのみとし、解説や挨拶は一切含めないでください。
【テキスト】
{text}"""
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )
            english_text = response.text.strip()
        except Exception as e:
            logging.error(f"PartnerCog 英訳エラー: {e}")
            return

        service = self.drive_service.get_service()
        if not service:
            return

        base_folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, "EnglishLearning"
        )
        if not base_folder_id:
            base_folder_id = await self.drive_service.create_folder(
                service, self.drive_folder_id, "EnglishLearning"
            )

        logs_folder_id = await self.drive_service.find_file(
            service, base_folder_id, "Logs"
        )
        if not logs_folder_id:
            logs_folder_id = await self.drive_service.create_folder(
                service, base_folder_id, "Logs"
            )

        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        file_name = f"{now.strftime('%Y-%m-%d')}_EN.md"

        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)

        en_lines = english_text.split("\n")
        ja_lines = text.split("\n")

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
                if raw_content:
                    content = raw_content
            except Exception:
                pass

        new_content = update_section(content, append_text, "## 💬 English Log")
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(
                service, logs_folder_id, file_name, new_content
            )

    async def _search_drive_notes(self, keywords: str):
        return await self.drive_service.search_markdown_files(keywords)

    async def generate_and_send_routine_message(
        self, context_data: str, instruction: str
    ):
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel:
            return

        recent_messages = []
        async for msg in channel.history(limit=6):
            role = "AI" if msg.author.id == self.bot.user.id else "User"
            recent_messages.append(f"{role}: {msg.content}")
        recent_log = "\n".join(reversed(recent_messages))

        user_manual = await self._get_user_manual()
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        system_prompt = get_system_prompt(self.user_name, now_str, user_manual)

        prompt = f"""{system_prompt}

【重要ミッション：空気を読んだ発言】
あなたは今、定期的なお知らせ（ルーティン）をユーザーに伝えるタイミングになりました。
しかし、急にロボットのように定時報告するのは絶対にやめてください。以下の「直近の会話ログ」の空気を読み取り、**自然な人間のように話題を切り出して**ください。

- もしユーザーが直前まで別の話で盛り上がっていたり、悩んでいたりする場合は、まずその話題に短く共感・返答した上で、「あ、そういえば」「全然話変わるんだけどさ」など、人間らしいクッション言葉を挟んでルーティンの話題に移行してください。
- 直近数時間会話がなければ、「お疲れ様！」「今ちょっと時間ある？」など、自然な挨拶から入ってください。

【直近の会話ログ】
{recent_log if recent_log else "（しばらく会話がありません）"}

【今回伝えるデータ】
{context_data}

【指示】
{instruction}
"""
        try:
            # コスト削減のため、ルーティン生成も flash モデルに変更
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )

            reply_text = response.text.strip()

            await channel.send(reply_text)
        except Exception as e:
            logging.error(f"PartnerCog 定期メッセージ生成エラー: {e}")

    async def fetch_todays_chat_log(self, channel: discord.TextChannel) -> str:
        today_start = datetime.datetime.now(JST).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_end = today_start + datetime.timedelta(days=1)

        log_lines = []
        async for msg in channel.history(
            limit=500, after=today_start, before=today_end, oldest_first=True
        ):
            time_str = msg.created_at.astimezone(JST).strftime("%H:%M")

            if msg.author.bot:
                if msg.content.startswith("【自動記録】"):
                    clean_content = msg.content.replace("【自動記録】", "").strip()
                    log_lines.append(f"{time_str} [システム記録]: {clean_content}")
                continue

            text = msg.content.strip()
            if not text or text.startswith("/"):
                continue

            log_lines.append(f"{time_str} [私]: {text}")

        return "\n".join(log_lines)

    async def _build_conversation_context(self, channel, current_msg_id: int, limit=30):
        messages = []
        async for msg in channel.history(limit=limit + 1, oldest_first=False):
            if msg.id == current_msg_id:
                continue
            if msg.content.startswith("/"):
                continue
            if msg.author.bot and msg.author.id != self.bot.user.id:
                continue
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments:
                text += " [メディア送信]"
            messages.append(
                types.Content(role=role, parts=[types.Part.from_text(text=text)])
            )
        return list(reversed(messages))

    async def _show_interim_summary(self, message: discord.Message):
        async with message.channel.typing():
            logs = await self.fetch_todays_chat_log(message.channel)
            if not logs:
                await message.reply("今日はまだ何も話してないね！")
                return
            prompt = f"{PROMPT_INTERIM_SUMMARY}\n\n{logs}"
            try:
                # コスト削減のため flash モデルに変更
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt
                )
                await message.reply(
                    f"今のところこんな感じ！👇\n\n{response.text.strip()}"
                )
            except Exception as e:
                await message.reply(f"ごめんね、エラーが出ちゃった💦 ({e})")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.channel.id != self.memo_channel_id:
            return

        self.user_name = "ゆうすけ"
        text = message.content.strip()
        is_short_message = len(text) < 30

        if text and not text.startswith("/"):
            asyncio.create_task(self._append_raw_message_to_obsidian(text))
            asyncio.create_task(self._append_english_log_to_obsidian(text))

        if is_short_message and text in ["まとめ", "途中経過", "整理して", "今の状態"]:
            await self._show_interim_summary(message)
            return

        input_parts = []
        if text:
            input_parts.append(types.Part.from_text(text=text))
        for att in message.attachments:
            if att.content_type and att.content_type.startswith(("image/", "audio/")):
                input_parts.append(
                    types.Part.from_bytes(
                        data=await att.read(), mime_type=att.content_type
                    )
                )
        if not input_parts:
            return

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        user_manual = await self._get_user_manual()
        system_prompt = get_system_prompt(self.user_name, now_str, user_manual)

        function_tools = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="search_memory",
                        description="Obsidianをキーワード検索する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "keywords": types.Schema(type=types.Type.STRING)
                            },
                            required=["keywords"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="check_schedule",
                        description="カレンダーの予定・リマインダーを確認する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING, description="YYYY-MM-DD"
                                )
                            },
                            required=["date"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="create_calendar_event",
                        description="カレンダーに予定やリマインダーを追加する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "summary": types.Schema(type=types.Type.STRING),
                                "start_time": types.Schema(type=types.Type.STRING),
                                "end_time": types.Schema(type=types.Type.STRING),
                            },
                            required=["summary", "start_time", "end_time"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="delete_calendar_event",
                        description="カレンダーの予定を検索してキャンセル・削除する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING, description="YYYY-MM-DD"
                                ),
                                "keyword": types.Schema(type=types.Type.STRING),
                            },
                            required=["date", "keyword"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="check_tasks",
                        description="Google Tasksの未完了タスク（ToDoリスト）を確認する。仕事、プライベートなどのリスト指定が可能。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "list_name": types.Schema(
                                    type=types.Type.STRING,
                                    description="リスト名（例: 仕事, プライベート）。省略時はデフォルト。",
                                )
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="add_task",
                        description="Google Tasks（ToDoリスト）に新しいタスクを追加する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "title": types.Schema(
                                    type=types.Type.STRING, description="タスク名"
                                ),
                                "list_name": types.Schema(
                                    type=types.Type.STRING,
                                    description="追加先のリスト名（例: 仕事, プライベート）。省略時はデフォルト。",
                                ),
                            },
                            required=["title"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="complete_task",
                        description="Google Tasksのタスクを完了（チェック）する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "keyword": types.Schema(
                                    type=types.Type.STRING,
                                    description="完了させたいタスク名の一部",
                                ),
                                "list_name": types.Schema(
                                    type=types.Type.STRING,
                                    description="リスト名（例: 仕事, プライベート）。省略時はデフォルト。",
                                ),
                            },
                            required=["keyword"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="record_stock_trade",
                        description="ユーザーが株の銘柄について発言した際に記録する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "code": types.Schema(
                                    type=types.Type.STRING,
                                    description="銘柄コード（例: 7203, AAPL）",
                                ),
                                "name": types.Schema(
                                    type=types.Type.STRING,
                                    description="銘柄名（例: トヨタ, Apple）",
                                ),
                                "memo": types.Schema(
                                    type=types.Type.STRING,
                                    description="エントリー理由や目標、ユーザーのコメントなど",
                                ),
                            },
                            required=["code", "name", "memo"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="record_habit",
                        description="ユーザーが習慣（例：筋トレ、週1回の掃除など）を「完了した」「やった」と明示的に報告した際に記録する。予定や会話の流れでは呼び出さないこと。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "habit_name": types.Schema(
                                    type=types.Type.STRING, description="習慣の名前"
                                ),
                                "frequency_days": types.Schema(
                                    type=types.Type.INTEGER,
                                    description="習慣の頻度（日数）。毎日は1、週1回は7。指定がなければ1。",
                                ),
                            },
                            required=["habit_name"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="list_habits",
                        description="現在登録されている習慣と、その頻度の一覧を取得する。",
                    ),
                    types.FunctionDeclaration(
                        name="delete_habit",
                        description="ユーザーが特定の習慣をリストから削除してほしいと頼んだ際に削除する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "habit_name": types.Schema(type=types.Type.STRING)
                            },
                            required=["habit_name"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="report_sleep",
                        description="ユーザーが指定した日付の睡眠記録を確認したい時に呼び出す。日付指定がない場合は今日とする。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING,
                                    description="確認したい日付（YYYY-MM-DD）。省略時は今日。",
                                )
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="report_health",
                        description="ユーザーが指定した日付の活動データ（歩数など）を確認したい時に呼び出す。日付指定がない場合は今日とする。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING,
                                    description="確認したい日付（YYYY-MM-DD）。省略時は今日。",
                                )
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="sync_past_fitbit",
                        description="ユーザーが「過去のFitbitデータ（睡眠や活動）を〇日分まとめて取得して・同期して」と頼んだ時に呼び出す。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "days": types.Schema(
                                    type=types.Type.INTEGER,
                                    description="さかのぼる日数。最大30。（例: 1週間分なら 7）",
                                )
                            },
                            required=["days"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="give_english_quiz",
                        description="ユーザーが「英語のクイズ出して」「瞬間英作文やりたい」と頼んだ時に呼び出す。",
                    ),
                    types.FunctionDeclaration(
                        name="sync_location",
                        description="過去のロケーション履歴（タイムライン）を指定した日付で同期し、Obsidianに保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING,
                                    description="同期したい日付（YYYY-MM-DD）",
                                )
                            },
                            required=["date"],
                        ),
                    ),
                    # ★追加1: 学習ノート記録用ツール
                    types.FunctionDeclaration(
                        name="record_study_note",
                        description="ユーザーが学習した内容（NotebookLMからのまとめなど）をノートに記録・保存したいと言った際に呼び出す。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "subject": types.Schema(
                                    type=types.Type.STRING,
                                    description="科目名やテーマ（例：民法、会社法など）",
                                ),
                                "memo": types.Schema(
                                    type=types.Type.STRING,
                                    description="記録する学習内容のテキストデータ",
                                ),
                            },
                            required=["subject", "memo"],
                        ),
                    ),
                    # ★追加2: 読書ノート記録用ツール
                    types.FunctionDeclaration(
                        name="record_book_note",
                        description="ユーザーが読書メモや本の要約をノートに記録・保存したいと言った際に呼び出す。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "book_title": types.Schema(
                                    type=types.Type.STRING, description="本のタイトル"
                                ),
                                "memo": types.Schema(
                                    type=types.Type.STRING,
                                    description="記録する読書メモのテキストデータ",
                                ),
                            },
                            required=["book_title", "memo"],
                        ),
                    ),
                ]
            )
        ]

        # 常に一番安価なモデルを使用
        use_model = "gemini-2.5-flash"
        contents = await self._build_conversation_context(
            message.channel, message.id, limit=10
        )
        contents.append(types.Content(role="user", parts=input_parts))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model=use_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt, tools=function_tools
                ),
            )

            if response.function_calls:
                contents.append(response.candidates[0].content)
                function_responses = []

                for function_call in response.function_calls:
                    tool_result = ""
                    if function_call.name == "search_memory":
                        tool_result = await self._search_drive_notes(
                            function_call.args["keywords"]
                        )
                    elif function_call.name == "check_schedule":
                        if self.calendar_service:
                            tool_result = (
                                await self.calendar_service.list_events_for_date(
                                    function_call.args["date"]
                                )
                            )
                        else:
                            tool_result = "システムエラー: カレンダーサービスに接続されていません。"
                    elif function_call.name == "create_calendar_event":
                        if self.calendar_service:
                            tool_result = await self.calendar_service.create_event(
                                function_call.args["summary"],
                                function_call.args["start_time"],
                                function_call.args["end_time"],
                                "",
                            )
                        else:
                            tool_result = "システムエラー: カレンダーサービスに接続されていません。"
                    elif function_call.name == "delete_calendar_event":
                        if self.calendar_service:
                            tool_result = (
                                await self.calendar_service.delete_event_by_keyword(
                                    function_call.args["date"],
                                    function_call.args["keyword"],
                                )
                            )
                        else:
                            tool_result = "システムエラー: カレンダーサービスに接続されていません。"
                    elif function_call.name == "check_tasks":
                        list_name = function_call.args.get("list_name")
                        if self.tasks_service:
                            tool_result = (
                                await self.tasks_service.get_uncompleted_tasks(
                                    list_name
                                )
                            )
                        else:
                            tool_result = (
                                "システムエラー: Tasksサービスに接続されていません。"
                            )
                    elif function_call.name == "add_task":
                        title = function_call.args["title"]
                        list_name = function_call.args.get("list_name")
                        if self.tasks_service:
                            tool_result = await self.tasks_service.add_task(
                                title, list_name=list_name
                            )
                        else:
                            tool_result = (
                                "システムエラー: Tasksサービスに接続されていません。"
                            )
                    elif function_call.name == "complete_task":
                        keyword = function_call.args["keyword"]
                        list_name = function_call.args.get("list_name")
                        if self.tasks_service:
                            tool_result = (
                                await self.tasks_service.complete_task_by_keyword(
                                    keyword, list_name=list_name
                                )
                            )
                        else:
                            tool_result = (
                                "システムエラー: Tasksサービスに接続されていません。"
                            )

                    elif function_call.name == "record_stock_trade":
                        code = function_call.args["code"].upper()
                        name = function_call.args["name"]
                        memo = function_call.args["memo"]
                        stock_cog = self.bot.get_cog("StockCog")
                        if stock_cog:
                            file_id = await stock_cog._find_stock_note_id(code)
                            if file_id:
                                await stock_cog._append_memo_to_note(file_id, memo)
                                tool_result = (
                                    f"既存の銘柄ノート（{name}）にメモを追記しました。"
                                )
                            else:
                                filename = f"{code}_{name}.md"
                                now = datetime.datetime.now(JST)
                                note_content = f'---\ncode: "{code}"\nname: "{name}"\nstatus: "Watching"\ncreated: {now.isoformat()}\ntags: [stock, investment]\n---\n# {name} ({code})\n## Logs\n- {now.strftime("%Y-%m-%d %H:%M")} {memo}\n## Review\n'
                                await stock_cog._save_file(filename, note_content)
                                tool_result = f"新しい銘柄ノート（{name}）を作成し、メモを記録しました。"
                        else:
                            tool_result = "システムエラー: StockCogが見つかりません。"
                    elif function_call.name == "record_habit":
                        habit_cog = self.bot.get_cog("HabitCog")
                        if habit_cog:
                            freq = int(function_call.args.get("frequency_days", 1))
                            tool_result = await habit_cog.complete_habit(
                                function_call.args["habit_name"], freq
                            )
                        else:
                            tool_result = "システムエラー: HabitCogが見つかりません。"
                    elif function_call.name == "list_habits":
                        habit_cog = self.bot.get_cog("HabitCog")
                        if habit_cog:
                            tool_result = await habit_cog.list_habits()
                        else:
                            tool_result = "システムエラー: HabitCogが見つかりません。"
                    elif function_call.name == "delete_habit":
                        habit_cog = self.bot.get_cog("HabitCog")
                        if habit_cog:
                            tool_result = await habit_cog.delete_habit(
                                function_call.args["habit_name"]
                            )
                        else:
                            tool_result = "システムエラー: HabitCogが見つかりません。"
                    elif function_call.name == "report_sleep":
                        fitbit_cog = self.bot.get_cog("FitbitCog")
                        if fitbit_cog:
                            target_date_str = function_call.args.get("date")
                            asyncio.create_task(
                                fitbit_cog.send_sleep_report(target_date_str)
                            )
                            tool_result = f"{target_date_str or '今日'}の睡眠レポートの取得と解析を開始しました。別メッセージとしてすぐに送信されます。"
                        else:
                            tool_result = "システムエラー: FitbitCogが見つかりません。"
                    elif function_call.name == "report_health":
                        fitbit_cog = self.bot.get_cog("FitbitCog")
                        if fitbit_cog:
                            target_date_str = function_call.args.get("date")
                            asyncio.create_task(
                                fitbit_cog.send_full_health_report(target_date_str)
                            )
                            tool_result = f"{target_date_str or '今日'}の健康レポートの取得と解析を開始しました。別メッセージとしてすぐに送信されます。"
                        else:
                            tool_result = "システムエラー: FitbitCogが見つかりません。"

                    elif function_call.name == "sync_past_fitbit":
                        days = int(function_call.args.get("days", 7))
                        fitbit_cog = self.bot.get_cog("FitbitCog")
                        if fitbit_cog:
                            asyncio.create_task(
                                fitbit_cog.perform_batch_sync_and_notify(
                                    days, message.channel
                                )
                            )
                            tool_result = f"過去{days}日分のFitbitデータの一括同期処理を裏側（バックグラウンド）で開始しました。完了次第、AIから自発的に専用の完了メッセージが送信されます。あなたはユーザーに「今から裏でまとめて取ってくるから、少し待っててね！」と短く明るく伝えてください。"
                        else:
                            tool_result = "システムエラー: FitbitCogが見つかりません。"

                    elif function_call.name == "give_english_quiz":
                        en_cog = self.bot.get_cog("EnglishLearningCog")
                        if en_cog:
                            asyncio.create_task(en_cog.daily_english_quiz())
                            tool_result = "英語クイズの生成を開始しました。別メッセージとしてすぐに送信されます。"
                        else:
                            tool_result = (
                                "システムエラー: EnglishLearningCogが見つかりません。"
                            )
                    elif function_call.name == "sync_location":
                        target_date = function_call.args["date"]
                        loc_cog = self.bot.get_cog("LocationLogCog")
                        if loc_cog:
                            tool_result = await loc_cog.perform_manual_sync(target_date)
                        else:
                            tool_result = (
                                "システムエラー: LocationLogCogが見つかりません。"
                            )

                    # ★追加: 学習・読書ノート処理
                    elif function_call.name == "record_study_note":
                        subject = function_call.args["subject"]
                        memo = function_call.args["memo"]
                        study_cog = self.bot.get_cog("StudyCog")
                        if study_cog:
                            await study_cog.append_study_memo(subject, memo)
                            tool_result = (
                                f"学習ノート（{subject}）に内容をバッチリ保存しました。"
                            )
                        else:
                            tool_result = "システムエラー: StudyCogが見つかりません。"

                    elif function_call.name == "record_book_note":
                        book_title = function_call.args["book_title"]
                        memo = function_call.args["memo"]
                        book_cog = self.bot.get_cog("BookCog")
                        if book_cog:
                            await book_cog.append_book_memo(book_title, memo)
                            tool_result = (
                                f"読書ノート（{book_title}）にメモを保存しました。"
                            )
                        else:
                            tool_result = "システムエラー: BookCogが見つかりません。"

                    function_responses.append(
                        types.Part.from_function_response(
                            name=function_call.name,
                            response={"result": str(tool_result)},
                        )
                    )

                contents.append(types.Content(role="user", parts=function_responses))
                response_final = await self.gemini_client.aio.models.generate_content(
                    model=use_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt
                    ),
                )

                if response_final.text:
                    reply_text = response_final.text.strip()
                    await message.channel.send(reply_text)
            else:
                if response.text:
                    reply_text = response.text.strip()
                    await message.channel.send(reply_text)

        except Exception as e:
            logging.error(f"PartnerCog 会話生成エラー: {e}")
            await message.channel.send(
                "ごめんね、ちょっと今考え込んでて…もう一回お願いできる？💦"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))
