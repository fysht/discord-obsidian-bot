import os
import logging
import datetime
import asyncio

from discord.ext import commands
from google.genai import types

from config import JST
from utils.obsidian_utils import update_section
from prompts import get_system_prompt, PROMPT_INTERIM_SUMMARY, PROMPT_CONTEXTUAL_LOG


class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_name = "ゆうすけ"
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        self.drive_service = bot.drive_service
        self.calendar_service = bot.calendar_service
        self.tasks_service = getattr(bot, "tasks_service", None)
        self.gemini_client = bot.gemini_client

        self.user_manual_cache = ""
        self.last_manual_fetch = None
        self.last_interaction = datetime.datetime.now(JST)

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

    async def _get_todays_obsidian_note(self) -> str:
        """今日のデイリーノートの内容を取得する。"""
        service = self.drive_service.get_service()
        if not service:
            return ""
        try:
            folder_id = await self.drive_service.find_file(
                service, self.drive_folder_id, "DailyNotes"
            )
            if not folder_id:
                return ""
            today_file = f"{datetime.datetime.now(JST).strftime('%Y-%m-%d')}.md"
            file_id = await self.drive_service.find_file(service, folder_id, today_file)
            if file_id:
                return await self.drive_service.read_text_file(service, file_id)
        except Exception as e:
            logging.error(f"DailyNote 読み取りエラー: {e}")
        return ""

    async def _save_todays_obsidian_note(self, content: str):
        """今日のデイリーノートを保存（作成・更新）する。"""
        service = self.drive_service.get_service()
        if not service:
            return
        try:
            folder_id = await self.drive_service.find_file(
                service, self.drive_folder_id, "DailyNotes"
            )
            if not folder_id:
                folder_id = await self.drive_service.create_folder(
                    service, self.drive_folder_id, "DailyNotes"
                )
            today_file = f"{datetime.datetime.now(JST).strftime('%Y-%m-%d')}.md"
            file_id = await self.drive_service.find_file(service, folder_id, today_file)
            if file_id:
                await self.drive_service.update_text(service, file_id, content)
            else:
                await self.drive_service.upload_text(
                    service, folder_id, today_file, content
                )
        except Exception as e:
            logging.error(f"DailyNote 保存エラー: {e}")

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
            except Exception as e:
                logging.error(f"Obsidianファイル読み込み失敗: {e}")

        new_content = update_section(content, append_text, target_heading)
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(
                service, folder_id, file_name, new_content
            )

    async def _save_thought_reflection_to_obsidian(
        self, theme: str, summary: str, next_step: str
    ):
        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        content = f"### {time_str} {theme}\n- **Summary**: {summary}\n- **Next Step**: {next_step}\n"
        await self._append_raw_message_to_obsidian(
            content, target_heading="## 💡 Thought Reflection"
        )
        return "思考整理ノートに保存しました。"

    async def _create_permanent_note_to_obsidian(self, title: str, content: str):
        service = self.drive_service.get_service()
        if not service:
            return "Drive不可"
        folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, "PermanentNotes"
        )
        if not folder_id:
            folder_id = await self.drive_service.create_folder(
                service, self.drive_folder_id, "PermanentNotes"
            )
        filename = f"{title}.md"
        now = datetime.datetime.now(JST)
        full_content = f"---\ntitle: {title}\ndate: {now.strftime('%Y-%m-%d')}\ntags: [permanent_note]\n---\n# {title}\n\n{content}\n"
        await self.drive_service.upload_text(
            service, folder_id, filename, full_content
        )
        return f"永久ノート「{title}」を作成しました。"

    async def _log_life_activity_to_obsidian(
        self, activity_name: str, status: str
    ):
        """ライフログをレンジ形式 (START - END) で記録・更新する"""
        if not self.drive_folder_id:
            return "DriveID未設定"

        note_content = await self._get_todays_obsidian_note()
        if not note_content:
            today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            note_content = f"# Daily Note {today_str}\n"

        import re
        now_time = datetime.datetime.now(JST).strftime("%H:%M")

        if status == "start":
            new_line = f"- {now_time} ▶ {activity_name}"
            updated_content = update_section(note_content, new_line, "## 🪟 Lifelog")
            await self._save_todays_obsidian_note(updated_content)
            return f"「{activity_name}」を開始したよ！"

        elif status == "end":
            lines = note_content.split("\n")
            updated = False
            for i in range(len(lines) - 1, -1, -1):
                if f"▶ {activity_name}" in lines[i]:
                    start_time_match = re.search(r"- (\d{2}:\d{2}) ▶", lines[i])
                    if not start_time_match:
                        start_time_match = re.search(r"- (\d{2}:\d{2})", lines[i])
                    if start_time_match:
                        start_time = start_time_match.group(1)
                        lines[i] = f"- {start_time} - {now_time} {activity_name}"
                        updated = True
                        break

            if updated:
                await self._save_todays_obsidian_note("\n".join(lines))
                return f"「{activity_name}」を終了したよ！お疲れ様。"
            else:
                new_line = f"- {now_time} ■ {activity_name}"
                updated_content = update_section(note_content, new_line, "## 🪟 Lifelog")
                await self._save_todays_obsidian_note(updated_content)
                return f"「{activity_name}」を記録しておいたよ。"
        return "不明なステータス"

    async def _search_drive_notes(self, keywords):
        if isinstance(keywords, list):
            keywords = " ".join(keywords)
        return await self.drive_service.search_markdown_files(keywords)

    async def _save_contextual_user_log(self, user_text: str, ai_text: str):
        """ユーザーの発言をベースに、AIの文脈を補足した1行ログを生成してObsidianに保存する。"""
        if not self.gemini_client or not user_text:
            return
        prompt = f"{PROMPT_CONTEXTUAL_LOG}\n\nAIの発言: {ai_text}\nユーザーの発言: {user_text}"
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-pro", contents=prompt
            )
            log_entry = response.text.strip() if response.text else ""
            if log_entry:
                await self._append_raw_message_to_obsidian(log_entry)
        except Exception as e:
            logging.error(f"Contextual log error: {e}")

    def _get_function_tools(self):
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="create_calendar_event",
                        description="Googleカレンダーに新しい予定を追加する。",
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
                        description="予定を削除する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(type=types.Type.STRING),
                                "keyword": types.Schema(type=types.Type.STRING),
                            },
                            required=["date", "keyword"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="check_tasks",
                        description="タスクを確認する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "list_name": types.Schema(type=types.Type.STRING)
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="add_task",
                        description="タスクを追加する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "title": types.Schema(type=types.Type.STRING),
                                "list_name": types.Schema(type=types.Type.STRING),
                            },
                            required=["title"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="complete_task",
                        description="タスクを完了する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "keyword": types.Schema(type=types.Type.STRING),
                                "list_name": types.Schema(type=types.Type.STRING),
                            },
                            required=["keyword"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="delete_task",
                        description="指定されたキーワードに合致するタスクを削除する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "keyword": types.Schema(type=types.Type.STRING),
                                "list_name": types.Schema(type=types.Type.STRING),
                            },
                            required=["keyword"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="record_habit",
                        description="習慣を記録する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "habit_name": types.Schema(type=types.Type.STRING),
                                "frequency_days": types.Schema(
                                    type=types.Type.INTEGER
                                ),
                            },
                            required=["habit_name"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="list_habits", description="習慣一覧を取得する。"
                    ),
                    types.FunctionDeclaration(
                        name="delete_habit",
                        description="習慣を削除する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "habit_name": types.Schema(type=types.Type.STRING)
                            },
                            required=["habit_name"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="log_life_activity",
                        description="ObsidianのLifelogセクションに活動を記録する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "activity_name": types.Schema(
                                    type=types.Type.STRING
                                ),
                                "status": types.Schema(
                                    type=types.Type.STRING,
                                    enum=["start", "end"],
                                ),
                            },
                            required=["activity_name", "status"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="save_thought_reflection",
                        description="思考や内省、気づきをObsidianに保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "theme": types.Schema(type=types.Type.STRING),
                                "summary": types.Schema(type=types.Type.STRING),
                                "next_step": types.Schema(type=types.Type.STRING),
                            },
                            required=["theme", "summary"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="create_permanent_note",
                        description="永続的なノート（知識・概念）をObsidianに作成する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "title": types.Schema(type=types.Type.STRING),
                                "content": types.Schema(type=types.Type.STRING),
                            },
                            required=["title", "content"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="search_memory",
                        description="Google Drive内の過去のノートを検索する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "keywords": types.Schema(
                                    type=types.Type.ARRAY,
                                    items=types.Schema(type=types.Type.STRING),
                                )
                            },
                            required=["keywords"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="check_schedule",
                        description="指定された日付の予定一覧を取得する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING,
                                    description="YYYY-MM-DD",
                                )
                            },
                            required=["date"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="report_sleep",
                        description="睡眠データを解析・報告する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING,
                                    description="YYYY-MM-DD",
                                )
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="report_health",
                        description="健康データ（歩数、心拍等）を解析・報告する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING,
                                    description="YYYY-MM-DD",
                                )
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="sync_location",
                        description="位置情報の履歴を同期する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "date": types.Schema(
                                    type=types.Type.STRING,
                                    description="YYYY-MM-DD",
                                )
                            },
                            required=["date"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="record_study_note",
                        description="学習メモを保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "subject": types.Schema(type=types.Type.STRING),
                                "memo": types.Schema(type=types.Type.STRING),
                            },
                            required=["subject", "memo"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="record_book_note",
                        description="読書メモを保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "book_title": types.Schema(type=types.Type.STRING),
                                "memo": types.Schema(type=types.Type.STRING),
                            },
                            required=["book_title", "memo"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="record_stock_trade",
                        description="株取引の記録・メモを保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "code": types.Schema(type=types.Type.STRING),
                                "name": types.Schema(type=types.Type.STRING),
                                "memo": types.Schema(type=types.Type.STRING),
                            },
                            required=["code", "name", "memo"],
                        ),
                    ),
                ]
            )
        ]

    async def _dispatch_tool_call(self, function_call):
        name = function_call.name
        args = function_call.args

        if name == "create_calendar_event":
            return f"[ACTION:calendar_add:summary={args['summary']}|start={args['start_time']}|end={args['end_time']}] (カレンダーに登録してもいいかな？)"
        elif name == "add_task":
            return f"[ACTION:task_add:title={args['title']}|list_name={args.get('list_name','')}] (タスクに追加するね、OK？)"
        elif name == "delete_task":
            return f"[ACTION:task_delete:keyword={args['keyword']}|list_name={args.get('list_name','')}] (タスクを削除するね、OK？)"
        elif name == "complete_task":
            return "タスクの完了はアプリの予定タブから直接チェックできるよ！"
        elif name in ["delete_calendar_event", "delete_habit"]:
            return "削除はアプリから直接行うか、詳細を教えてね。"

        try:
            if name == "log_life_activity":
                return await self._log_life_activity_to_obsidian(
                    args["activity_name"], args["status"]
                )
            elif name == "save_thought_reflection":
                return await self._save_thought_reflection_to_obsidian(
                    args.get("theme", "無題"),
                    args.get("summary", ""),
                    args.get("next_step", ""),
                )
            elif name == "create_permanent_note":
                return await self._create_permanent_note_to_obsidian(
                    args["title"], args["content"]
                )
            elif name == "search_memory":
                return await self._search_drive_notes(args["keywords"])
            elif name == "check_schedule":
                return (
                    await self.calendar_service.list_events_for_date(args["date"])
                    if self.calendar_service
                    else "カレンダー非接続"
                )
            elif name == "check_tasks":
                return (
                    await self.tasks_service.get_uncompleted_tasks(
                        args.get("list_name")
                    )
                    if self.tasks_service
                    else "Tasks非接続"
                )
            elif name == "list_habits":
                habit_cog = self.bot.get_cog("HabitCog")
                return await habit_cog.list_habits() if habit_cog else "HabitCog不在"
            elif name == "record_habit":
                habit_cog = self.bot.get_cog("HabitCog")
                return (
                    await habit_cog.complete_habit(
                        args["habit_name"], int(args.get("frequency_days", 1))
                    )
                    if habit_cog
                    else "HabitCog不在"
                )
            elif name == "report_sleep":
                fitbit_cog = self.bot.get_cog("FitbitCog")
                if fitbit_cog:
                    asyncio.create_task(
                        fitbit_cog.send_sleep_report(args.get("date"))
                    )
                    return "睡眠データ解析中..."
                return "FitbitCog不在"
            elif name == "report_health":
                fitbit_cog = self.bot.get_cog("FitbitCog")
                if fitbit_cog:
                    asyncio.create_task(
                        fitbit_cog.send_full_health_report(args.get("date"))
                    )
                    return "健康データ解析中..."
                return "FitbitCog不在"
            elif name == "sync_location":
                location_cog = self.bot.get_cog("LocationLogCog")
                return (
                    await location_cog.perform_manual_sync(args["date"])
                    if location_cog
                    else "LocationCog不在"
                )
            elif name == "record_study_note":
                study_cog = self.bot.get_cog("StudyCog")
                if study_cog:
                    await study_cog.append_study_memo(args["subject"], args["memo"])
                    return "保存したよ。"
                return "StudyCog不在"
            elif name == "record_book_note":
                book_cog = self.bot.get_cog("BookCog")
                if book_cog:
                    await book_cog.append_book_memo(args["book_title"], args["memo"])
                    return "保存したよ。"
                return "BookCog不在"
            elif name == "record_stock_trade":
                stock_cog = self.bot.get_cog("StockCog")
                if stock_cog:
                    code = args["code"].upper()
                    name_s = args["name"]
                    memo = args["memo"]
                    f_id = await stock_cog._find_stock_note_id(code)
                    if f_id:
                        await stock_cog._append_memo_to_note(f_id, memo)
                        return f"銘柄ノート（{name_s}）に追記したよ。"
                    else:
                        await stock_cog._save_file(
                            f"{code}_{name_s}.md", f"# {name_s}\n- {memo}"
                        )
                        return f"銘柄ノート（{name_s}）を新規作成したよ。"
                return "StockCog不在"
        except Exception as e:
            logging.error(f"Tool error ({name}): {e}")
            return f"ツールエラー: {e}"
        return "不明なツールです。"

    async def generate_response_for_app(self, text: str, history_messages: list):
        """PWAアプリから呼び出されるAI応答生成"""
        self.last_interaction = datetime.datetime.now(JST)

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        system_prompt = get_system_prompt(
            self.user_name, now_str, await self._get_user_manual()
        )

        contents = history_messages.copy()
        contents.append(
            types.Content(
                role="user", parts=[types.Part.from_text(text=text)]
            )
        )

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=self._get_function_tools(),
                ),
            )

            if response.function_calls:
                contents.append(response.candidates[0].content)
                f_responses = []
                for fc in response.function_calls:
                    res = await self._dispatch_tool_call(fc)
                    f_responses.append(
                        types.Part.from_function_response(
                            name=fc.name, response={"result": str(res)}
                        )
                    )

                contents.append(types.Content(role="user", parts=f_responses))
                final_res = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt
                    ),
                )
                reply_text = (
                    final_res.text.strip()
                    if final_res.text
                    else "了解、手配しておく。"
                )
            else:
                reply_text = response.text.strip() if response.text else "了解！"

            asyncio.create_task(self._save_contextual_user_log(text, reply_text))
            return reply_text
        except Exception as e:
            logging.error(f"App Resp Error: {e}")
            return "エラーが発生しちゃった、もう一回送ってくれる？"

    async def generate_and_send_routine_message(
        self, context_text: str, routine_prompt: str
    ):
        """定期メッセージをAIで生成してDBへ保存する（アプリに表示）。"""
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        system_prompt = get_system_prompt(
            self.user_name, now_str, await self._get_user_manual()
        )

        prompt = f"{routine_prompt}\n\n【状況】\n{context_text}"
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system_prompt),
            )
            if response.text:
                reply_text = response.text.strip()
                from api.database import save_message as _save_msg
                await _save_msg("assistant", reply_text)
        except Exception as e:
            logging.error(f"Routine message generation error: {e}")

    async def fetch_todays_chat_log(self, channel=None) -> str:
        """今日のチャットログを収集してテキスト化する（日報用）。"""
        log_lines = []
        try:
            from api.database import get_todays_log
            db_log = await get_todays_log()
            if db_log and db_log.strip():
                log_lines.append(db_log)
        except Exception as e:
            logging.error(f"PWA DB log fetch error: {e}")
        return "\n".join(log_lines)


async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))
