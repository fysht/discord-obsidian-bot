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

    async def _build_conversation_context(self, channel, current_msg_id, limit=10):
        """Discordの履歴から、Geminiに渡すためのコンテキストを構築する。"""
        contents = []
        try:
            async for msg in channel.history(limit=limit, before=current_msg_id, oldest_first=True):
                if not msg.content: continue
                role = "assistant" if msg.author.bot else "user"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.content)]))
        except Exception as e:
            logging.error(f"履歴取得エラー: {e}")
        return contents

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
            await self.drive_service.upload_text(service, folder_id, file_name, new_content)

    async def _log_life_activity_to_obsidian(self, activity_name: str, status: str):
        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        marker = "▶" if status == "start" else "■"
        log_line = f"- {time_str} {marker} {activity_name}"
        await self._append_raw_message_to_obsidian(log_line, target_heading="## 🗒️ Logs")
        return f"Obsidianに記録しました: {activity_name} ({status})"

    async def _save_thought_reflection_to_obsidian(self, theme: str, summary: str, next_step: str):
        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        content = f"### {time_str} {theme}\n- **Summary**: {summary}\n- **Next Step**: {next_step}\n"
        await self._append_raw_message_to_obsidian(content, target_heading="## 💡 Thought Reflection")
        return "思考整理ノートに保存しました。"

    async def _create_permanent_note_to_obsidian(self, title: str, content: str):
        service = self.drive_service.get_service()
        if not service: return "Drive不可"
        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "PermanentNotes")
        if not folder_id: folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "PermanentNotes")
        filename = f"{title}.md"
        now = datetime.datetime.now(JST)
        full_content = f"---\ntitle: {title}\ndate: {now.strftime('%Y-%m-%d')}\ntags: [permanent_note]\n---\n# {title}\n\n{content}\n"
        await self.drive_service.upload_text(service, folder_id, filename, full_content)
        return f"永久ノート「{title}」を作成しました。"

    async def _search_drive_notes(self, keywords: str):
        # 簡易的な検索（実際にはより高度な実装が必要だが、現状維持）
        return f"「{keywords}」に関する過去の記録を数件見つけました（シミュレーション）"

    def _get_function_tools(self):
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="log_life_activity",
                        description="LLR形式でユーザーの行動やタスクの開始・終了を記録する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "activity_name": types.Schema(type=types.Type.STRING, description="行動名"),
                                "status": types.Schema(type=types.Type.STRING, description="'start' または 'end'"),
                            },
                            required=["activity_name", "status"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="save_thought_reflection",
                        description="思考整理の結果をObsidianに保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "theme": types.Schema(type=types.Type.STRING),
                                "summary": types.Schema(type=types.Type.STRING),
                                "next_step": types.Schema(type=types.Type.STRING),
                            },
                            required=["theme", "summary", "next_step"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="create_permanent_note",
                        description="永続的なノートを作成する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "title": types.Schema(type=types.Type.STRING),
                                "content": types.Schema(type=types.Type.STRING),
                            },
                            required=["title", "content"],
                        ),
                    ),
                    types.FunctionDeclaration(name="search_memory", description="知識を検索する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"keywords": types.Schema(type=types.Type.STRING)}, required=["keywords"])),
                    types.FunctionDeclaration(name="check_schedule", description="今日の予定を確認する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING)}, required=["date"])),
                    types.FunctionDeclaration(name="create_calendar_event", description="予定を追加する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"summary": types.Schema(type=types.Type.STRING), "start_time": types.Schema(type=types.Type.STRING), "end_time": types.Schema(type=types.Type.STRING)}, required=["summary", "start_time", "end_time"])),
                    types.FunctionDeclaration(name="delete_calendar_event", description="予定を削除する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING), "keyword": types.Schema(type=types.Type.STRING)}, required=["date", "keyword"])),
                    types.FunctionDeclaration(name="check_tasks", description="タスクを確認する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"list_name": types.Schema(type=types.Type.STRING)})),
                    types.FunctionDeclaration(name="add_task", description="タスクを追加する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"title": types.Schema(type=types.Type.STRING), "list_name": types.Schema(type=types.Type.STRING)}, required=["title"])),
                    types.FunctionDeclaration(name="complete_task", description="タスクを完了する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"keyword": types.Schema(type=types.Type.STRING), "list_name": types.Schema(type=types.Type.STRING)}, required=["keyword"])),
                    types.FunctionDeclaration(name="record_habit", description="習慣を記録する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"habit_name": types.Schema(type=types.Type.STRING), "frequency_days": types.Schema(type=types.Type.INTEGER)}, required=["habit_name"])),
                    types.FunctionDeclaration(name="list_habits", description="習慣一覧を取得する。"),
                    types.FunctionDeclaration(name="delete_habit", description="習慣を削除する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"habit_name": types.Schema(type=types.Type.STRING)}, required=["habit_name"])),
                    types.FunctionDeclaration(name="report_sleep", description="睡眠データを報告。", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING)})),
                    types.FunctionDeclaration(name="report_health", description="健康データを報告。", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING)})),
                    types.FunctionDeclaration(name="sync_past_fitbit", description="Fitbit過去同期。", parameters=types.Schema(type=types.Type.OBJECT, properties={"days": types.Schema(type=types.Type.INTEGER)}, required=["days"])),
                    types.FunctionDeclaration(name="give_english_quiz", description="英語クイズ。"),
                    types.FunctionDeclaration(name="sync_location", description="位置情報同期。", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING)}, required=["date"])),
                    types.FunctionDeclaration(name="record_study_note", description="学習メモを保存。", parameters=types.Schema(type=types.Type.OBJECT, properties={"subject": types.Schema(type=types.Type.STRING), "memo": types.Schema(type=types.Type.STRING)}, required=["subject", "memo"])),
                    types.FunctionDeclaration(name="record_book_note", description="読書メモを保存。", parameters=types.Schema(type=types.Type.OBJECT, properties={"book_title": types.Schema(type=types.Type.STRING), "memo": types.Schema(type=types.Type.STRING)}, required=["book_title", "memo"])),
                    types.FunctionDeclaration(name="record_stock_trade", description="株取引を記録。", parameters=types.Schema(type=types.Type.OBJECT, properties={"code": types.Schema(type=types.Type.STRING), "name": types.Schema(type=types.Type.STRING), "memo": types.Schema(type=types.Type.STRING)}, required=["code", "name", "memo"])),
                ]
            )
        ]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.memo_channel_id: return
        text = message.content.strip()
        if text and not text.startswith("/"): asyncio.create_task(self._append_raw_message_to_obsidian(text))
        
        input_parts = [types.Part.from_text(text=text)] if text else []
        for att in message.attachments:
            if att.content_type and att.content_type.startswith(("image/", "audio/")):
                input_parts.append(types.Part.from_bytes(data=await att.read(), mime_type=att.content_type))
        if not input_parts: return

        # アプリのDBにもユーザーメッセージを保存
        from api.database import save_message as _save_msg
        await _save_msg("user", text)

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        system_prompt = get_system_prompt(self.user_name, now_str, await self._get_user_manual())
        
        # 履歴を取得してコンテキストを構築
        contents = await self._build_conversation_context(message.channel, message.id, limit=15)
        contents.append(types.Content(role="user", parts=input_parts))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, tools=self._get_function_tools()),
            )

            reply_text = ""
            if response.function_calls:
                contents.append(response.candidates[0].content)
                function_responses = []
                for fc in response.function_calls:
                    res = await self._dispatch_tool_call(fc)
                    function_responses.append(types.Part.from_function_response(name=fc.name, response={"result": str(res)}))
                
                contents.append(types.Content(role="user", parts=function_responses))
                final_res = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_prompt),
                )
                reply_text = final_res.text.strip() if final_res.text else "全プロセス完了したよ！"
            else:
                reply_text = response.text.strip() if response.text else "..."

            from api.database import save_message as _save_msg
            await _save_msg("assistant", reply_text)
            
            # ObsidianにAIの返答も記録（Partner: プレフィックスを付ける）
            asyncio.create_task(self._append_raw_message_to_obsidian(f"Partner: {reply_text}"))
            
            await message.channel.send(reply_text)

        except Exception as e:
            logging.error(f"PartnerCog Error: {e}")
            await message.channel.send("ごめん、ちょっと今エラーが出ちゃった💦")

    async def generate_response_for_app(self, text: str, history_messages: list):
        if text: asyncio.create_task(self._append_raw_message_to_obsidian(text))
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        system_prompt = get_system_prompt(self.user_name, now_str, await self._get_user_manual())
        
        contents = history_messages.copy()
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, tools=self._get_function_tools()),
            )

            if response.function_calls:
                contents.append(response.candidates[0].content)
                f_responses = []
                for fc in response.function_calls:
                    res = await self._dispatch_tool_call(fc)
                    f_responses.append(types.Part.from_function_response(name=fc.name, response={"result": str(res)}))
                
                contents.append(types.Content(role="user", parts=f_responses))
                final_res = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_prompt),
                )
                reply_text = final_res.text.strip() if final_res.text else "手配しておいたよ！"
            else:
                reply_text = response.text.strip() if response.text else "了解！"

            # ObsidianにAIの返答も記録
            asyncio.create_task(self._append_raw_message_to_obsidian(f"Partner: {reply_text}"))
            return reply_text
        except Exception as e:
            logging.error(f"App Resp Error: {e}")
            return "エラーが発生しちゃった、もう一回送ってくれる？"

    async def _dispatch_tool_call(self, function_call):
        name = function_call.name
        args = function_call.args
        
        if name == "create_calendar_event":
            return f"[ACTION:calendar_add:summary={args['summary']}|start={args['start_time']}|end={args['end_time']}] (カレンダーに登録してもいいかな？)"
        elif name == "add_task":
            return f"[ACTION:task_add:title={args['title']}|list_name={args.get('list_name','')}] (タスクに追加するね、OK？)"
        elif name == "complete_task":
            return "タスクの完了はアプリの予定タブから直接チェックできるよ！"
        elif name in ["delete_calendar_event", "delete_habit"]:
            return "削除はアプリから直接行うか、詳細を教えてね。"

        try:
            if name == "log_life_activity": return await self._log_life_activity_to_obsidian(args["activity_name"], args["status"])
            elif name == "save_thought_reflection": return await self._save_thought_reflection_to_obsidian(args.get("theme", "無題"), args.get("summary", ""), args.get("next_step", ""))
            elif name == "create_permanent_note": return await self._create_permanent_note_to_obsidian(args["title"], args["content"])
            elif name == "search_memory": return await self._search_drive_notes(args["keywords"])
            elif name == "check_schedule": return await self.calendar_service.list_events_for_date(args["date"]) if self.calendar_service else "カレンダー非接続"
            elif name == "check_tasks": return await self.tasks_service.get_uncompleted_tasks(args.get("list_name")) if self.tasks_service else "Tasks非接続"
            elif name == "list_habits": return await self.bot.get_cog("HabitCog").list_habits() if self.bot.get_cog("HabitCog") else "HabitCog不在"
            elif name == "record_habit": return await self.bot.get_cog("HabitCog").complete_habit(args["habit_name"], int(args.get("frequency_days", 1))) if self.bot.get_cog("HabitCog") else "HabitCog不在"
            elif name == "report_sleep":
                if self.bot.get_cog("FitbitCog"): asyncio.create_task(self.bot.get_cog("FitbitCog").send_sleep_report(args.get("date"))); return "睡眠データ解析中..."
                return "FitbitCog不在"
            elif name == "report_health":
                if self.bot.get_cog("FitbitCog"): asyncio.create_task(self.bot.get_cog("FitbitCog").send_full_health_report(args.get("date"))); return "健康データ解析中..."
                return "FitbitCog不在"
            elif name == "sync_location": return await self.bot.get_cog("LocationLogCog").perform_manual_sync(args["date"]) if self.bot.get_cog("LocationLogCog") else "LocationCog不在"
            elif name == "record_study_note": await self.bot.get_cog("StudyCog").append_study_memo(args["subject"], args["memo"]); return "保存したよ。"
            elif name == "record_book_note": await self.bot.get_cog("BookCog").append_book_memo(args["book_title"], args["memo"]); return "保存したよ。"
            elif name == "record_stock_trade":
                s_cog = self.bot.get_cog("StockCog")
                if s_cog:
                    code = args["code"].upper(); name_s = args["name"]; memo = args["memo"]
                    f_id = await s_cog._find_stock_note_id(code)
                    if f_id: await s_cog._append_memo_to_note(f_id, memo); return f"銘柄ノート（{name_s}）に追記したよ。"
                    else: await s_cog._save_file(f"{code}_{name_s}.md", f"# {name_s}\n- {memo}"); return f"銘柄ノート（{name_s}）を新規作成したよ。"
                return "StockCog不在"
        except Exception as e:
            logging.error(f"Tool error ({name}): {e}")
            return f"ツールエラー: {e}"
        return "不明なツールです。"

    async def fetch_todays_chat_log(self, channel):
        """今日一日のチャットログを収集してテキスト化する（日報用）。"""
        now = datetime.datetime.now(JST)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        log_lines = []
        try:
            # Discordからの取得
            async for msg in channel.history(limit=500, after=start_of_day, oldest_first=True):
                if msg.author.bot and msg.author != self.bot.user: continue
                author_name = "User" if not msg.author.bot else "AI"
                log_lines.append(f"{msg.created_at.astimezone(JST).strftime('%H:%M')} [{author_name}]: {msg.content}")
        except Exception as e:
            logging.error(f"Chat log fetch error: {e}")
        return "\n".join(log_lines)

    async def generate_and_send_routine_message(self, context_text: str, routine_prompt: str):
        """特定のコンテキスト（ロケーション同期完了など）に基づき、AIが自発的に挨拶メッセージを生成してDBへ保存する（アプリに表示）。"""
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        system_prompt = get_system_prompt(self.user_name, now_str, await self._get_user_manual())
        
        prompt = f"{routine_prompt}\n\n【状況】\n{context_text}"
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system_prompt),
            )
            if response.text:
                reply_text = response.text.strip()
                from api.database import save_message as _save_msg
                await _save_msg("assistant", reply_text)
                # Discord送信は、ユーザーの要望により停止するか、専用チャンネルがあれば送信する設計にする。
                # 現状はアプリへの同期（DB保存）のみ。
        except Exception as e:
            logging.error(f"Routine message generation error: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))
