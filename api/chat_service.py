import os
import logging
import datetime
import re

from google.genai import types
from config import JST
from prompts import get_system_prompt
from api.database import get_history, save_message, get_todays_log
from utils.obsidian_utils import update_section


class ChatService:
    """PWA向けのAI応答生成サービス。PartnerCogの核心ロジックを共有して使う。"""

    def __init__(self, gemini_client, drive_service, calendar_service, tasks_service):
        self.gemini_client = gemini_client
        self.drive_service = drive_service
        self.calendar_service = calendar_service
        self.tasks_service = tasks_service
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.user_name = "ゆうすけ"

        self.user_manual_cache = ""
        self.last_manual_fetch = None

    async def _get_user_manual(self):
        now = datetime.datetime.now()
        if self.last_manual_fetch and (now - self.last_manual_fetch).total_seconds() < 3600:
            return self.user_manual_cache

        service = self.drive_service.get_service()
        if not service:
            return ""
        try:
            folder_id = await self.drive_service.find_file(service, self.drive_folder_id, ".bot")
            if not folder_id:
                return ""
            file_id = await self.drive_service.find_file(service, folder_id, "UserManual.md")
            if file_id:
                content = await self.drive_service.read_text_file(service, file_id)
                self.user_manual_cache = content
                self.last_manual_fetch = now
                return content
        except Exception as e:
            logging.error(f"ChatService UserManual読み込みエラー: {e}")
        return ""

    async def _search_drive_notes(self, keywords: str):
        return await self.drive_service.search_markdown_files(keywords)

    async def _log_life_activity(self, activity_name: str, status: str) -> str:
        service = self.drive_service.get_service()
        if not service:
            return "システムエラー"

        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not folder_id:
            folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")

        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        file_name = f"{now.strftime('%Y-%m-%d')}.md"

        f_id = await self.drive_service.find_file(service, folder_id, file_name)
        content = f"# Daily Note {file_name.replace('.md', '')}\n"
        if f_id:
            try:
                raw = await self.drive_service.read_text_file(service, f_id)
                if raw:
                    content = raw
            except Exception:
                pass

        if status == "start":
            append_text = f"- [/] {activity_name} ({time_str}-)"
            new_content = update_section(content, append_text, "## 🎯 Tasks")
            if f_id:
                await self.drive_service.update_text(service, f_id, new_content)
            else:
                await self.drive_service.upload_text(service, folder_id, file_name, new_content)
            return f"「{activity_name}」の開始を記録しました。"
        elif status == "end":
            pattern = re.compile(rf"- \[\/\] {re.escape(activity_name)} \((.*?)-\)")
            match = pattern.search(content)
            if match:
                start_time = match.group(1)
                replacement = f"- [x] {activity_name} ({start_time}-{time_str})"
                new_content = content[:match.start()] + replacement + content[match.end():]
                if f_id:
                    await self.drive_service.update_text(service, f_id, new_content)
                return f"「{activity_name}」の終了を記録しました（開始: {start_time}）。"
            else:
                append_text = f"- [x] {activity_name} (開始時間不明-{time_str})"
                new_content = update_section(content, append_text, "## 🎯 Tasks")
                if f_id:
                    await self.drive_service.update_text(service, f_id, new_content)
                return f"「{activity_name}」の終了時刻のみ記録しました。"
        return "不正なステータスです。"

    async def _save_thought_reflection(self, theme: str, summary: str, next_step: str) -> str:
        service = self.drive_service.get_service()
        if not service:
            return "システムエラー"
        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        file_name = f"{now.strftime('%Y-%m-%d')}.md"
        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not folder_id:
            folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")
        f_id = await self.drive_service.find_file(service, folder_id, file_name)
        content = f"# Daily Note {file_name.replace('.md', '')}\n"
        if f_id:
            try:
                raw = await self.drive_service.read_text_file(service, f_id)
                if raw:
                    content = raw
            except Exception:
                pass
        append_text = f"### {time_str} テーマ: {theme}\n{summary}\n**Next Step:** {next_step}"
        new_content = update_section(content, append_text, "## 🤔 Thought Reflection")
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(service, folder_id, file_name, new_content)
        return f"壁打ちの内容を保存しました。"

    async def _create_permanent_note(self, title: str, content: str) -> str:
        service = self.drive_service.get_service()
        if not service:
            return "システムエラー"
        zk_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "Zettelkasten")
        if not zk_folder_id:
            zk_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "Zettelkasten")
        safe_title = title.replace("/", "_").replace("\\", "_")
        zk_file_name = f"{safe_title}.md"
        zk_f_id = await self.drive_service.find_file(service, zk_folder_id, zk_file_name)
        if zk_f_id:
            return f"「{safe_title}」は既に存在します。"
        await self.drive_service.upload_text(service, zk_folder_id, zk_file_name, content)
        return f"永久ノート「{safe_title}」を保存しました。"

    async def _append_to_timeline(self, text: str):
        service = self.drive_service.get_service()
        if not service:
            return
        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not folder_id:
            folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")
        now = datetime.datetime.now(JST)
        time_str = now.strftime("%H:%M")
        file_name = f"{now.strftime('%Y-%m-%d')}.md"
        f_id = await self.drive_service.find_file(service, folder_id, file_name)
        note_content = f"# Daily Note {file_name.replace('.md', '')}\n"
        if f_id:
            try:
                raw = await self.drive_service.read_text_file(service, f_id)
                if raw:
                    note_content = raw
            except Exception:
                pass
        append_text = f"- {time_str} {text}"
        new_content = update_section(note_content, append_text, "## 💬 Timeline")
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(service, folder_id, file_name, new_content)

    def _build_function_tools(self):
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="log_life_activity",
                        description="LLR形式でユーザーの行動やタスクの開始・終了を記録する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "activity_name": types.Schema(type=types.Type.STRING, description="行動やタスクの名前"),
                                "status": types.Schema(type=types.Type.STRING, description="'start' または 'end'"),
                            },
                            required=["activity_name", "status"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="save_thought_reflection",
                        description="壁打ち（思考整理）の結果を保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "theme": types.Schema(type=types.Type.STRING, description="テーマ"),
                                "summary": types.Schema(type=types.Type.STRING, description="整理した内容"),
                                "next_step": types.Schema(type=types.Type.STRING, description="次のアクション"),
                            },
                            required=["theme", "summary", "next_step"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="create_permanent_note",
                        description="深い洞察やアイデアをZettelkasten（永久ノート）として保存する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "title": types.Schema(type=types.Type.STRING, description="ノートのタイトル"),
                                "content": types.Schema(type=types.Type.STRING, description="ノートの本文"),
                            },
                            required=["title", "content"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="search_memory",
                        description="Obsidianをキーワード検索する。",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={"keywords": types.Schema(type=types.Type.STRING)},
                            required=["keywords"],
                        ),
                    ),
                ]
            )
        ]

    async def generate_response(self, user_message: str) -> str:
        """ユーザーのメッセージに対してAIの応答を生成する"""
        # Obsidian Timelineに追記
        try:
            await self._append_to_timeline(user_message)
        except Exception as e:
            logging.error(f"ChatService Timeline追記エラー: {e}")

        # 会話履歴を取得してコンテキストを構築
        history = await get_history(limit=10)
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        user_manual = await self._get_user_manual()
        system_prompt = get_system_prompt(self.user_name, now_str, user_manual)

        contents = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])]))
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_message)]))

        function_tools = self._build_function_tools()

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt, tools=function_tools
                ),
            )

            # Function Callingのハンドリング
            if response.function_calls:
                contents.append(response.candidates[0].content)
                function_responses = []

                for fc in response.function_calls:
                    tool_result = ""
                    if fc.name == "log_life_activity":
                        tool_result = await self._log_life_activity(fc.args["activity_name"], fc.args["status"])
                    elif fc.name == "save_thought_reflection":
                        tool_result = await self._save_thought_reflection(
                            fc.args.get("theme", "無題"), fc.args.get("summary", ""), fc.args.get("next_step", "")
                        )
                    elif fc.name == "create_permanent_note":
                        tool_result = await self._create_permanent_note(fc.args["title"], fc.args["content"])
                    elif fc.name == "search_memory":
                        tool_result = await self._search_drive_notes(fc.args["keywords"])

                    function_responses.append(
                        types.Part.from_function_response(name=fc.name, response={"result": str(tool_result)})
                    )

                contents.append(types.Content(role="user", parts=function_responses))
                follow_up = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_prompt),
                )
                return follow_up.text.strip()

            return response.text.strip()

        except Exception as e:
            logging.error(f"ChatService AI応答エラー: {e}")
            return "申し訳ございません。一時的にエラーが発生しました。少々お待ちくださいませ。"
