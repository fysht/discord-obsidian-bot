import os
import logging
import datetime
import asyncio

from discord.ext import commands
from google.genai import types

from config import JST
from utils.obsidian_utils import update_section
from utils.async_utils import safe_create_task
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

    @staticmethod
    def _truncate_at_sentence(text: str, limit: int) -> str:
        """text を limit 文字以内に収める。超過時は文末（。！？や改行）境界で切って「…」を付ける。
        境界が見つからなければ limit でハード切り。改行は空白に畳む。"""
        t = (text or "").replace("\n", " ").strip()
        if len(t) <= limit:
            return t
        head = t[:limit]
        # limit 手前で最後に現れる文末記号までで切る（あまりに手前なら諦めてハード切り）
        cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"), head.rfind("!"), head.rfind("?"))
        if cut >= int(limit * 0.5):
            return head[:cut + 1] + "…"
        return head.rstrip() + "…"

    async def _get_past_daily_journals(self) -> str:
        """1年前と3ヶ月前のデイリーノートから Daily Journal セクションを抜粋して返す。"""
        if not self.drive_service:
            return ""
        service = self.drive_service.get_service()
        if not service:
            return ""

        import re as _re
        now = datetime.datetime.now(JST)
        # 1年前 / 3ヶ月前（≒90日前）
        targets = [
            ("1年前の今日", (now - datetime.timedelta(days=365)).strftime("%Y-%m-%d")),
            ("3ヶ月前の今日", (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")),
        ]

        try:
            folder_id = await self.drive_service.find_file(
                service, self.drive_folder_id, "DailyNotes"
            )
            if not folder_id:
                return ""
        except Exception as e:
            logging.error(f"DailyNotes folder lookup error: {e}")
            return ""

        excerpts = []
        for label, date_str in targets:
            try:
                fid = await self.drive_service.find_file(service, folder_id, f"{date_str}.md")
                if not fid:
                    continue
                content = await self.drive_service.read_text_file(service, fid)
                if not content:
                    continue
                m = _re.search(r"## 📔 Daily Journal\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
                if not m:
                    continue
                journal = m.group(1).strip()
                if not journal:
                    continue
                # 通知カードは展開可能なので長文を保持できる。長すぎる場合のみ
                # 文末（。/！/？/改行）の境界で切って「…」を付け、文が途中で切れないようにする。
                snippet = self._truncate_at_sentence(journal, 700)
                excerpts.append(f"【{label}（{date_str}）】 {snippet}")
            except Exception as e:
                logging.debug(f"past journal fetch error ({date_str}): {e}")

        return "\n".join(excerpts)

    async def _get_mit_section(self, date_str: str | None = None) -> str:
        """指定日の MIT セクションを取得する。デフォルトは今日。"""
        if not date_str:
            date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        service = self.drive_service.get_service()
        if not service:
            return ""
        try:
            folder_id = await self.drive_service.find_file(
                service, self.drive_folder_id, "DailyNotes"
            )
            if not folder_id:
                return ""
            fid = await self.drive_service.find_file(service, folder_id, f"{date_str}.md")
            if not fid:
                return ""
            content = await self.drive_service.read_text_file(service, fid)
            import re as _re
            m = _re.search(r"## 🎯 MIT\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
            return m.group(1).strip() if m else ""
        except Exception as e:
            logging.debug(f"MIT section read error: {e}")
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
        target_heading: str = "## 💬 Chat Log",
        with_time: bool = True,
    ):
        """テキストを 1 行（複数行ならサブ行付き）でデイリーノートの指定セクションへ追記する。
        with_time=False のときは行頭に時刻 (HH:MM) を付けない（気分/体調/良かったこと/学びなど、
        時刻が不要な記録向け）。"""
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

        prefix = f"- {time_str} " if with_time else "- "
        lines = text.split("\n")
        if len(lines) == 1:
            append_text = f"{prefix}{text}"
        else:
            formatted_lines = [f"{prefix}{lines[0]}"]
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
            from services.gemini_model_resolver import resolve_gemini_model
            _m = await resolve_gemini_model("partner_chat", default_pro=True)
            response = await self.gemini_client.aio.models.generate_content(
                model=_m, contents=prompt
            )
            log_entry = response.text.strip() if response.text else ""
            if log_entry:
                await self._append_raw_message_to_obsidian(log_entry)
        except Exception as e:
            logging.error(f"Contextual log error: {e}")

    def _get_function_tools(self, enable_search: bool = False):
        """ツール一覧を返す。
        enable_search=True なら、Google 検索と関数呼び出しの両方を返す
        （Gemini に検索→そのデータでカレンダー登録などの連動アクションをさせるため）。
        併用時は generate_response_for_app 側で
        `tool_config.include_server_side_tool_invocations=True` を併せて設定する。
        """
        tools = []
        if enable_search:
            try:
                tools.append(types.Tool(google_search=types.GoogleSearch()))
            except Exception as e:
                logging.warning(f"google_search ツール構築失敗（model 未対応の可能性）: {e}")
        tools.append(types.Tool(
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
                        description=(
                            "ユーザーが**明示的に**『タスク／ToDo／やること／残ってる作業』の一覧を見せてと"
                            "頼んだときだけ呼ぶ。雑談・相談・記録・その他の話題では絶対に呼ばないこと。"
                            "判断に迷ったら呼ばずに普通に会話で返す。"
                        ),
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "list_name": types.Schema(type=types.Type.STRING, description="'仕事' または 'プライベート'。指定が無ければ省略可")
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
                        name="propose_permanent_note",
                        description=(
                            "永続的なノート（知識・概念）の保存をユーザーへ提案する。"
                            "即時保存はせず、フロントで確認モーダルを出してユーザーが承認した場合のみ実際に保存される。"
                        ),
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
                    types.FunctionDeclaration(
                        name="set_mit",
                        description=(
                            "今日の Most Important Tasks (MIT) を3つまで登録する。"
                            "ユーザーが今日の最重要タスクを宣言したときに呼ぶ。"
                            "このツールは即実行ではなく確認ボタンを返すため、"
                            "ユーザーが選んだ3つを items に渡せばOK。"
                        ),
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "items": types.Schema(
                                    type=types.Type.ARRAY,
                                    items=types.Schema(type=types.Type.STRING),
                                    description="MIT のタイトル文字列リスト（最大3つ）",
                                ),
                            },
                            required=["items"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_mit_status",
                        description=(
                            "今日の MIT の達成状況を取得する。"
                            "MIT セクションの行頭が '- [x]' のものは達成、'- [ ]' は未達。"
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="rollover_mit",
                        description=(
                            "今日の未達 MIT を翌日に持ち越す。"
                            "確認ボタンを返してから実際に書き込む。"
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="propose_note",
                        description=(
                            "ユーザーの会話の中で「メモにしておきたい」「書籍からの学びがある」などの"
                            "ノート化に値する内容が出てきたとき、ノート保存を提案する。"
                            "このツールは即実行せず、フロントに確認ボタンを返す。"
                            "ユーザーが押下したらノート保存モーダルが開き、内容を編集して保存できる。"
                            "category は 'study'（勉強）/ 'work'（仕事）/ 'idea'（アイデア）/ "
                            "'reading'（読書） / 'other'（その他）のいずれか。"
                            "読書メモを取りたい文脈では必ず category='reading' を指定すること。"
                        ),
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "title": types.Schema(
                                    type=types.Type.STRING,
                                    description="ノートのタイトル（短く具体的に）",
                                ),
                                "category": types.Schema(
                                    type=types.Type.STRING,
                                    description="study / work / idea / reading / other",
                                ),
                            },
                            required=["title", "category"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="log_journal_entry",
                        description=(
                            "ユーザー自身が会話の中で『今日の出来事』『学び・気づき』『良かったこと・感謝』"
                            "『気分』『体調』『食べたもの』『使ったお金』を話したとき、その内容を該当ログに"
                            "記録することを提案する。即時保存せず、ワンタップで記録できる確認ボタンを返す。"
                            "ask_log_question が『質問して答えてもらう』のに対し、こちらは『すでに話された内容を"
                            "そのまま記録に回す』ためのもの。相談・質問・雑談・予定の話には使わない。"
                            "scope は event（出来事）/ learning（学び・気づき）/ gratitude（良かったこと・感謝）/ "
                            "mood（気分）/ condition（体調）/ meal（食事）/ expense（支出）。"
                        ),
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "scope": types.Schema(
                                    type=types.Type.STRING,
                                    description="event / learning / gratitude / mood / condition / meal / expense",
                                ),
                                "text": types.Schema(
                                    type=types.Type.STRING,
                                    description="記録する本文（ユーザーの発言を簡潔に整えたもの。食事なら料理名、支出なら『品目 金額』）",
                                ),
                            },
                            required=["scope", "text"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="ask_log_question",
                        description=(
                            "ユーザーに記録のための質問を投げ、回答欄（選択チップ＋自由記載）を表示する。"
                            "食事・気分・体調・支出などを負担なくログに残してもらいたいときに使う。"
                            "例: 朝に『朝食は何を食べた？』(scope='meal')、"
                            "日中に『今の気分は？』(scope='mood')、買い物の話題で『何にいくら使った？』(scope='expense')。"
                            "即実行ではなく、ユーザーが回答欄に答えると自動でログに記録される。"
                            "scope は 'meal'（食事）/ 'expense'（支出）/ 'mood'（気分）/ "
                            "'condition'（体調）/ 'reading'（読書メモ）/ 'afternoon'（昼の振り返り）/ "
                            "'learning'（今日の学び・気づき）/ 'gratitude'（良かったこと・感謝）のいずれか。"
                        ),
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "scope": types.Schema(
                                    type=types.Type.STRING,
                                    description="meal / expense / mood / condition / reading / afternoon / learning / gratitude",
                                ),
                                "question": types.Schema(
                                    type=types.Type.STRING,
                                    description="ユーザーに表示する質問文（短く具体的に）",
                                ),
                            },
                            required=["scope", "question"],
                        ),
                    ),
                ]
            ))
        return tools

    async def _set_mit_to_obsidian(self, items: list[str]) -> str:
        """今日の DailyNote の `## 🎯 MIT` セクションに MIT を書き込む。"""
        if not items:
            return "MIT が空っぽだよ。"
        # 最大3つ
        items = [s.strip() for s in items if s and s.strip()][:3]
        if not items:
            return "有効な MIT が無かったよ。"

        note_content = await self._get_todays_obsidian_note()
        if not note_content:
            today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            note_content = f"# Daily Note {today_str}\n"

        section_text = "\n".join(f"- [ ] {t}" for t in items)
        new_content = update_section(note_content, section_text, "## 🎯 MIT")
        await self._save_todays_obsidian_note(new_content)
        return f"今日のMIT3つを設定したよ！: {' / '.join(items)}"

    async def _set_mit_for_date(self, date_str: str, items: list[str]) -> str:
        """指定日 (YYYY-MM-DD) の DailyNote の `## 🎯 MIT` に MIT を書き込む。
        前夜に答えた『明日のMIT』を翌日ノートへ前もって確定させる用途。"""
        items = [s.strip() for s in (items or []) if s and s.strip()][:3]
        if not items:
            return "有効な MIT が無かったよ。"
        service = self.drive_service.get_service()
        if not service:
            return "Obsidian に接続できなかったよ。"
        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not folder_id:
            folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")
        f_id = await self.drive_service.find_file(service, folder_id, f"{date_str}.md")
        if f_id:
            content = await self.drive_service.read_text_file(service, f_id)
        else:
            content = f"# Daily Note {date_str}\n"

        section_text = "\n".join(f"- [ ] {t}" for t in items)
        new_content = update_section(content, section_text, "## 🎯 MIT")
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(service, folder_id, f"{date_str}.md", new_content)
        return f"{date_str} のMITを設定したよ！: {' / '.join(items)}"

    async def _toggle_mit_in_obsidian(self, index: int) -> dict:
        """今日のMITの `index` 番目（0始まり）の `[ ]`/`[x]` をトグルする。"""
        import re as _re

        note_content = await self._get_todays_obsidian_note()
        if not note_content:
            return {"status": "error", "message": "今日のデイリーノートが見つかりません。"}

        m = _re.search(r"## 🎯 MIT\n(.*?)(?=\n## |\Z)", note_content, _re.DOTALL)
        if not m:
            return {"status": "error", "message": "MIT セクションが見つかりません。"}

        section_body = m.group(1)
        item_lines = []
        for line in section_body.splitlines():
            if _re.match(r"^\s*-\s*\[[ xX]\]\s*", line):
                item_lines.append(line)
        if index < 0 or index >= len(item_lines):
            return {"status": "error", "message": "対象の MIT が見つかりません。"}

        target_line = item_lines[index]
        is_done = bool(_re.match(r"^\s*-\s*\[[xX]\]\s*", target_line))
        if is_done:
            new_line = _re.sub(r"\[[xX]\]", "[ ]", target_line, count=1)
        else:
            new_line = _re.sub(r"\[\s\]", "[x]", target_line, count=1)

        new_section_body = section_body.replace(target_line, new_line, 1)
        new_content = note_content.replace(section_body, new_section_body, 1)
        await self._save_todays_obsidian_note(new_content)

        text = _re.sub(r"^\s*-\s*\[[ xX]\]\s*", "", new_line).strip()
        return {"status": "success", "index": index, "done": not is_done, "text": text}

    async def _rollover_mit(self) -> str:
        """今日の未達 MIT を翌日のノートに繰り越す。"""
        today_section = await self._get_mit_section()
        if not today_section:
            return "今日はMITが登録されてないみたい。"
        import re as _re
        unfinished = []
        for line in today_section.split("\n"):
            m = _re.match(r"^-\s*\[\s\]\s*(.+)$", line.strip())
            if m:
                unfinished.append(m.group(1).strip())
        if not unfinished:
            return "今日のMITは全部達成したじゃん！繰越しは無しでOK！"

        # 翌日のノートに書き込む
        tomorrow_str = (datetime.datetime.now(JST) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        service = self.drive_service.get_service()
        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not folder_id:
            folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")
        f_id = await self.drive_service.find_file(service, folder_id, f"{tomorrow_str}.md")
        if f_id:
            content = await self.drive_service.read_text_file(service, f_id)
        else:
            content = f"# Daily Note {tomorrow_str}\n"

        section_text = "\n".join(f"- [ ] {t}" for t in unfinished)
        new_content = update_section(content, section_text, "## 🎯 MIT")
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(service, folder_id, f"{tomorrow_str}.md", new_content)
        return f"未達のMIT {len(unfinished)} 件を明日に繰り越したよ: {' / '.join(unfinished)}"

    async def _recent_meal_names(self, days: int = 30, limit: int = 5) -> list[str]:
        """直近の食事ログから頻出の料理名を集計して返す（回答欄チップ用）。"""
        try:
            from api.database import get_meals_by_range
            today = datetime.datetime.now(JST).date()
            start = (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            rows = await get_meals_by_range(start, today.strftime("%Y-%m-%d"))
            counts: dict[str, int] = {}
            for r in rows:
                nm = (r.get("name") or "").strip()
                if nm and nm != "食事":
                    counts[nm] = counts.get(nm, 0) + 1
            return [n for n, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]]
        except Exception as e:
            logging.debug(f"_recent_meal_names error: {e}")
            return []

    async def _create_log_question(self, scope, question, meal_type: str = ""):
        """記録用の質問を作成し (qid, 本文文字列) を返す。失敗時は (None, エラー文字列)。
        scope レジストリに沿って context（選択チップ）を詰めて daily_question を登録する。
        meal_type を渡すと context に食事区分を保存し、回答時にこの区分で記録される
        （夜にまとめて回答しても朝食は朝食として残る）。"""
        scope = (scope or "").strip()
        question = (question or "").strip()
        if scope not in {"meal", "expense", "mood", "condition", "reading",
                         "afternoon", "learning", "gratitude"}:
            return None, "（その種類のログにはまだ対応してないんだ）"
        if not question:
            return None, "（質問文が空っぽだよ）"
        try:
            import json as _json
            from api.database import add_daily_question
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            context: dict = {}
            if scope == "meal":
                chips = await self._recent_meal_names()
                if chips:
                    context["chips"] = chips
                if (meal_type or "").strip():
                    context["meal_type"] = meal_type.strip()
            ctx_str = _json.dumps(context, ensure_ascii=False) if context else ""
            qid = await add_daily_question(today, question, scope=scope, context=ctx_str)
            return qid, f"{question}\n[QUESTIONS:{scope}:{today}]"
        except Exception as e:
            logging.error(f"_create_log_question error: {e}")
            return None, "（質問の準備に失敗しちゃった）"

    async def _ask_log_question(self, scope, question) -> str:
        """記録用の質問を作成し、回答欄を出すためのマーカー付き文字列を返す（会話ツール用）。"""
        _, body = await self._create_log_question(scope, question)
        return body

    async def send_log_question(self, scope: str, question: str, push: bool = True,
                                meal_type: str = "") -> bool:
        """ルーティン等から記録質問を能動的に投下する（質問作成＋回答欄付きメッセージ送信）。
        同じ「文面」の未回答質問が今日すでにあれば二重投下しない（朝食・昼食・夕食のように
        同じ scope でも文面が異なる質問は別物として両方出せる）。
        push=False のときは Push 通知を出さずメッセージ保存のみ（他の通知とまとめたい場合に使う）。
        meal_type（朝食/昼食/夕食）を渡すと、回答がその区分・代表時刻で食事ログに記録される。"""
        try:
            from api.database import get_questions_by_date
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            qtext = (question or "").strip()
            existing = await get_questions_by_date(today, scope=scope)
            if any(q.get("status") != "resolved" and (q.get("question") or "").strip() == qtext for q in existing):
                return False  # 既に同じ未回答質問がある（同一文面のみ抑止）
        except Exception as e:
            logging.debug(f"send_log_question 既存確認エラー: {e}")
        qid, body = await self._create_log_question(scope, question, meal_type=meal_type)
        if not body or body.startswith("（"):
            return False
        try:
            from api.database import save_message
            # メッセージは常に保存（チャット内の回答欄＋まとめビュー用）
            await save_message("assistant", body)
            if not push:
                return True  # 他の Push とまとめるため通知は出さない

            # 選択式スコープ（mood/condition）は、通知のアクションボタンから
            # アプリを開かず1タップで回答できるよう、Push にアクションを載せる（機能E）。
            from api import notification_service as _ns
            chips = self._choice_chips_for(scope)
            if qid and chips:
                actions, answers = [], {}
                for i, c in enumerate(chips[:2]):  # 通知のボタンは2件程度が上限
                    aid = f"a{i}"
                    actions.append({"action": aid, "title": c})
                    answers[aid] = c
                await _ns.send_push(
                    "📝 ちょっと記録", question, url="/",
                    actions=actions, data={"qid": qid, "answers": answers},
                )
            else:
                await _ns.send_push("📝 ちょっと記録", question, url="/")
            return True
        except Exception as e:
            logging.error(f"send_log_question 送信エラー: {e}")
            return False

    @staticmethod
    def _choice_chips_for(scope: str) -> list:
        """選択式スコープの既定チップを scope レジストリから取得する（通知アクション用）。"""
        try:
            from services.log_question_registry import get_scope_config
            cfg = get_scope_config(scope)
            if cfg.get("answer_type") == "choice":
                return list(cfg.get("chips") or [])
        except Exception:
            pass
        return []

    async def send_english_quiz(self) -> bool:
        """英語フレーズのクイズを1問、選択肢チップ付きの回答欄として投下する（学習レール）。
        正解と phrase_id を context に保存し、回答時に採点・記録する。"""
        try:
            from api.routers.english_phrases import english_phrases_quiz
            data = await english_phrases_quiz()
        except Exception:
            return False  # フレーズ未登録など
        options = data.get("options") or []
        correct = (data.get("phrase") or "").strip()
        if not options or not correct:
            return False
        try:
            import json as _json
            from api.database import add_daily_question, get_questions_by_date
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            existing = await get_questions_by_date(today, scope="english_quiz")
            if any(q.get("status") != "resolved" for q in existing):
                return False
            translation = (data.get("translation") or "").strip()
            qtext = (
                f"🗣 英語クイズ！「{translation}」に合うフレーズはどれ？" if translation
                else "🗣 英語クイズ！正しいフレーズはどれ？"
            )
            ctx = {"chips": options, "correct": correct, "phrase_id": data.get("id")}
            await add_daily_question(
                today, qtext, scope="english_quiz",
                context=_json.dumps(ctx, ensure_ascii=False),
            )
            from api.notification_service import save_message_and_notify
            await save_message_and_notify(
                "assistant", f"{qtext}\n[QUESTIONS:english_quiz:{today}]",
                proactive=True, title="🗣 英語クイズ",
            )
            return True
        except Exception as e:
            logging.error(f"send_english_quiz error: {e}")
            return False

    async def _dispatch_tool_call(self, function_call):
        name = function_call.name
        args = function_call.args

        if name == "create_calendar_event":
            return f"[ACTION:calendar_add:summary={args['summary']}|start={args['start_time']}|end={args['end_time']}] (カレンダーに登録してもいいかな？)"
        elif name == "add_task":
            return f"[ACTION:task_add:title={args['title']}|list_name={args.get('list_name','')}] (タスクに追加するね、OK？)"
        elif name == "delete_task":
            return f"[ACTION:task_delete:keyword={args['keyword']}|list_name={args.get('list_name','')}] (タスクを削除するね、OK？)"
        elif name == "set_mit":
            items = args.get("items", []) or []
            items_str = ",".join(s.replace(",", " ").replace("|", " ") for s in items[:3])
            return f"[ACTION:mit_set:items={items_str}] (今日のMIT3つ、これでOK？)"
        elif name == "rollover_mit":
            return "[ACTION:mit_rollover] (未達のMIT、明日に持ち越すね、OK？)"
        elif name == "propose_note":
            t = (args.get("title") or "メモ").replace("|", " ").replace("=", " ")
            cat = (args.get("category") or "other").strip()
            if cat not in {"study", "work", "idea", "reading", "other"}:
                cat = "other"
            return f"[ACTION:note_create:title={t}|category={cat}] (この内容、ノートに保存しておく？モーダルで内容を確認・編集できるよ)"
        elif name == "get_mit_status":
            section = await self._get_mit_section()
            return section if section else "今日はまだMITが設定されてないみたい。"
        elif name == "complete_task":
            return "タスクの完了はアプリの予定タブから直接チェックできるよ！"
        elif name in ["delete_calendar_event", "delete_habit"]:
            return "削除はアプリから直接行うか、詳細を教えてね。"
        elif name == "ask_log_question":
            return await self._ask_log_question(args.get("scope"), args.get("question"))
        elif name == "log_journal_entry":
            # ユーザーが話した内容をそのまま記録に回す確認ボタン。即時保存はしない。
            # scope ごとに最適な記録先へ振り分ける（食事=食事モーダル、支出=支出確認モーダル）。
            scope = (args.get("scope") or "").strip()
            text = (args.get("text") or "").replace("|", " ").replace("=", " ").replace("\n", " ").strip()
            if not text:
                return "（記録する内容が読み取れなかったので、記録の提案はしないでね。）"
            if scope == "meal":
                # 既存の食事ログ登録ボタン（押すと食事モーダルが料理名プリフィルで開く）。
                return f"[ACTION:log_meal:name={text[:40]}] (今の食事、食事ログに記録する？ボタンから登録できるよ。)"
            if scope == "expense":
                # 既存の支出確認フローに合わせ、テキストから金額等を抽出して確認ボタンを返す。
                try:
                    from api.routers.expenses import analyze_expense_text
                    ex = await analyze_expense_text(text)
                except Exception:
                    ex = {}

                def _s(x):
                    return str(x or "").replace("|", " ").replace("=", " ").replace("\n", " ")
                amount = int(ex.get("amount") or 0)
                vendor = _s(ex.get("vendor"))
                category = _s(ex.get("category") or "その他")
                date = _s(ex.get("date")) or datetime.datetime.now(JST).strftime("%Y-%m-%d")
                return (
                    f"[ACTION:expense_confirm:amount={amount}|vendor={vendor}|category={category}"
                    f"|payment_method={_s(ex.get('payment_method'))}|memo={_s(ex.get('memo') or text)}|date={date}] "
                    f"(支出を記録する？内容を確認して保存してね。)"
                )
            label = {
                "event": "出来事", "learning": "学び", "gratitude": "良かったこと",
                "mood": "気分", "condition": "体調",
            }.get(scope)
            if not label:
                return "（種別が判別できなかったので、記録の提案はしないでね。）"
            return (
                f"[ACTION:journal_log:scope={scope}|text={text}] "
                f"(今の話、「{label}」として記録する？ボタンを押すと残せるよ。)"
            )

        try:
            if name == "log_life_activity":
                # 自動保存はせず、ボタン提案として ACTION タグを返す
                n = (args.get("activity_name") or "").replace("|", " ").replace("=", " ")
                s = (args.get("status") or "start").strip()
                if s not in ("start", "end"):
                    s = "start"
                return (
                    f"[ACTION:log_life_activity:activity_name={n}|status={s}] "
                    f"({n} を「{'開始' if s == 'start' else '終了'}」として記録する？ボタンで保存できるよ。)"
                )
            elif name == "save_thought_reflection":
                # 自動保存はせず、ボタン提案として ACTION タグを返す
                th = (args.get("theme") or "無題").replace("|", " ").replace("=", " ")
                sm = (args.get("summary") or "").replace("|", " ").replace("=", " ").replace("\n", " / ")
                ns = (args.get("next_step") or "").replace("|", " ").replace("=", " ").replace("\n", " / ")
                return (
                    f"[ACTION:save_thought_reflection:theme={th}|summary={sm}|next_step={ns}] "
                    f"(思考整理「{th}」を保存する？ボタンで実行できるよ。)"
                )
            elif name == "propose_permanent_note":
                # 即時保存はしない。フロントで確認モーダルを起動するための ACTION 文字列を返す
                t = (args.get("title") or "メモ").replace("|", " ").replace("=", " ")
                c = (args.get("content") or "").replace("|", " ").replace("=", " ")
                return (
                    f"[ACTION:propose_perm_note:title={t}|content={c}] "
                    f"(この内容を永久ノートにする？確認モーダルから内容を編集して保存できるよ)"
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
                # 自動完了はせず、ボタン提案として ACTION タグを返す
                hn = (args.get("habit_name") or "").replace("|", " ").replace("=", " ")
                return (
                    f"[ACTION:habit_complete:habit_name={hn}] "
                    f"(習慣「{hn}」を完了として記録する？ボタンで実行できるよ。)"
                )
            elif name == "report_sleep":
                fitbit_cog = self.bot.get_cog("FitbitCog")
                if fitbit_cog:
                    safe_create_task(
                        fitbit_cog.send_sleep_report(args.get("date")),
                        name="fitbit-sleep-report",
                    )
                    return "睡眠データ解析中..."
                return "FitbitCog不在"
            elif name == "report_health":
                fitbit_cog = self.bot.get_cog("FitbitCog")
                if fitbit_cog:
                    safe_create_task(
                        fitbit_cog.send_full_health_report(args.get("date")),
                        name="fitbit-health-report",
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

    @staticmethod
    def _looks_like_search_query(text: str) -> bool:
        """ユーザー発話が「ネット検索が必要そう」か簡易判定する。
        最新情報・知識系の問い合わせや明示的な指示（「調べて」等）を拾う。"""
        if not text:
            return False
        t = text.strip()
        # 明示的なキーワード
        triggers = (
            "調べて", "ググって", "検索して", "最新", "今の", "現在の",
            "ニュース", "とは何", "とは？", "について教えて", "の意味",
            "は誰", "はいつ", "はどこ", "の使い方", "の価格", "の評判",
            "おすすめ", "比較", "の方法",
        )
        if any(k in t for k in triggers):
            return True
        # 末尾が「？」かつ短く事実っぽい問い合わせ
        if (t.endswith("？") or t.endswith("?")) and len(t) <= 60:
            return True
        return False

    async def generate_response_for_app(
        self, text: str, history_messages: list,
        english_mode: bool = False, search_mode: bool = False,
    ):
        """PWAアプリから呼び出されるAI応答生成。
        search_mode=True または発話が検索クエリ風なら Google Search grounding を有効化。"""
        self.last_interaction = datetime.datetime.now(JST)
        # 明示指定 or 自動判定で検索 grounding を有効化
        enable_search = bool(search_mode) or self._looks_like_search_query(text)

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        system_prompt = get_system_prompt(
            self.user_name, now_str, await self._get_user_manual()
        )
        if english_mode:
            from prompts import PROMPT_ENGLISH_CONVERSATION
            system_prompt = system_prompt + "\n\n" + PROMPT_ENGLISH_CONVERSATION

        contents = history_messages.copy()
        contents.append(
            types.Content(
                role="user", parts=[types.Part.from_text(text=text)]
            )
        )

        try:
            from services.gemini_model_resolver import resolve_gemini_model
            _m = await resolve_gemini_model("partner_chat", default_pro=True)

            # 検索＋関数呼び出し併用時は include_server_side_tool_invocations が必要
            # （Gemini API の制約）。SDK バージョン差を吸収するため try/except で構築。
            cfg_kwargs = {
                "system_instruction": system_prompt,
                "tools": self._get_function_tools(enable_search=enable_search),
            }
            if enable_search:
                try:
                    cfg_kwargs["tool_config"] = types.ToolConfig(
                        include_server_side_tool_invocations=True,
                    )
                except TypeError:
                    # SDK が当該フィールドを未サポート → 検索と関数呼び出しは併用不可。
                    # 検索 grounding を諦め、関数ツール（ask_log_question / カレンダー登録など）を優先する。
                    # 検索のみにすると ask_log_question 等が呼べず、モデルがツール呼び出しを
                    # 本文テキストとして漏らす不具合（謎の生文字列・ボタン）につながるため。
                    cfg_kwargs["tools"] = self._get_function_tools(enable_search=False)
                    cfg_kwargs.pop("tool_config", None)
                    logging.info("SDK が include_server_side_tool_invocations 未対応のため、関数ツール優先で実行（検索は無効）")

            response = await self.gemini_client.aio.models.generate_content(
                model=_m,
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )

            if response.function_calls:
                contents.append(response.candidates[0].content)
                f_responses = []
                tool_result_texts: list[str] = []
                for fc in response.function_calls:
                    res = await self._dispatch_tool_call(fc)
                    tool_result_texts.append(str(res))
                    f_responses.append(
                        types.Part.from_function_response(
                            name=fc.name, response={"result": str(res)}
                        )
                    )

                contents.append(types.Content(role="user", parts=f_responses))
                # 二回目の生成では [ACTION:...] マーカーを文面に保つよう明示指示する。
                # これがないと Gemini が「登録したよ！」のように言うだけで
                # 確認ボタン用マーカーを省略してしまい、実際の登録が行われない事故が起こる。
                second_instruction = system_prompt + (
                    "\n\n【重要】ツールの戻り値に `[ACTION:...]` マーカーが含まれている場合、"
                    "**必ずそのマーカーを返信文の末尾にそのままコピーして残してください**。"
                    "このマーカーはユーザーの画面に確認ボタンを表示するために使われます。"
                    "勝手に『登録したよ』『追加したよ』のように完了形で答えてはいけません。"
                    "実際の登録/追加はユーザーがボタンを押して初めて行われます。"
                )
                final_res = await self.gemini_client.aio.models.generate_content(
                    model=_m,
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=second_instruction),
                )
                if final_res.text and final_res.text.strip():
                    reply_text = final_res.text.strip()
                else:
                    fallback = " / ".join(r for r in tool_result_texts if r and r != "None")
                    reply_text = fallback if fallback else "処理したよ！"

                # 安全網: ツール結果に [ACTION:...] が含まれているのに、
                # 最終返信から欠落していたら自動で末尾に補完する。
                import re as _re
                # ACTION（確認ボタン）と QUESTIONS（回答欄）の両マーカーを取りこぼさない
                action_pat = _re.compile(r"\[(?:ACTION|QUESTIONS):[^\]]+\]")
                missing_actions = []
                for tr in tool_result_texts:
                    for m in action_pat.findall(tr or ""):
                        if m not in reply_text:
                            missing_actions.append(m)
                if missing_actions:
                    reply_text = reply_text.rstrip() + "\n\n" + "\n".join(missing_actions)
                    logging.info(f"[partner] 欠落していた ACTION マーカーを {len(missing_actions)} 件補完")
            else:
                reply_text = response.text.strip() if response.text else "了解！"

            # Google Search grounding が走った場合、citation を簡易付与
            try:
                gmeta = getattr(response.candidates[0], "grounding_metadata", None) if response.candidates else None
                if gmeta:
                    chunks = getattr(gmeta, "grounding_chunks", None) or []
                    sources = []
                    for ch in chunks[:5]:
                        web = getattr(ch, "web", None)
                        if web and getattr(web, "uri", None):
                            title = getattr(web, "title", None) or web.uri
                            sources.append(f"- [{title}]({web.uri})")
                    if sources:
                        reply_text = reply_text + "\n\n🔎 参考:\n" + "\n".join(sources)
            except Exception as _se:
                logging.debug(f"grounding citation extract failed: {_se}")

            safe_create_task(
                self._save_contextual_user_log(text, reply_text),
                name="partner-contextual-log",
            )
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

        # context_text が空のときは「【状況】」見出しを付けない
        # （付けると AI がそのまま出力に混入させてしまうことがあるため）
        if context_text and context_text.strip():
            prompt = f"{routine_prompt}\n\n【状況】\n{context_text}"
        else:
            prompt = routine_prompt
        try:
            from services.gemini_model_resolver import resolve_gemini_model
            _m = await resolve_gemini_model("routines", default_pro=False)
            response = await self.gemini_client.aio.models.generate_content(
                model=_m,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system_prompt),
            )
            if response.text:
                reply_text = response.text.strip()
                from api.notification_service import save_message_and_notify as _save_msg
                await _save_msg("assistant", reply_text, proactive=True)
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
