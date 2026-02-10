import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types
import os
import datetime
import logging
import re
import zoneinfo

# Services
from services.drive_service import DriveService
from services.webclip_service import WebClipService
from services.calendar_service import CalendarService
from services.task_service import TaskService
from services.fitbit_service import FitbitService
from services.info_service import InfoService

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class PartnerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID") or os.getenv("DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        
        self.fitbit_client_id = os.getenv("FITBIT_CLIENT_ID")
        self.fitbit_client_secret = os.getenv("FITBIT_CLIENT_SECRET")
        self.fitbit_refresh_token = os.getenv("FITBIT_REFRESH_TOKEN")
        
        # Services Init
        self.drive_service = DriveService(self.drive_folder_id)
        self.calendar_service = CalendarService(self.drive_service.get_creds(), self.calendar_id)
        self.webclip_service = WebClipService(self.drive_service, self.gemini_api_key)
        self.task_service = TaskService(self.drive_service)
        self.info_service = InfoService()
        
        if all([self.fitbit_client_id, self.fitbit_client_secret, self.fitbit_refresh_token]):
            self.fitbit_service = FitbitService(
                self.drive_service, self.fitbit_client_id, self.fitbit_client_secret, self.fitbit_refresh_token
            )
        else:
            self.fitbit_service = None
        
        self.gemini_client = None
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)

    async def cog_load(self):
        await self.task_service.load_data()
        self.check_schedule_loop.start()
        self.check_reminders_loop.start()
        self.morning_greeting_loop.start()
        self.nightly_reflection_loop.start()
        self.daily_summary_loop.start()
        self.inactivity_check_loop.start()

    async def cog_unload(self):
        self.check_schedule_loop.cancel()
        self.check_reminders_loop.cancel()
        self.morning_greeting_loop.cancel()
        self.nightly_reflection_loop.cancel()
        self.daily_summary_loop.cancel()
        self.inactivity_check_loop.cancel()
        await self.task_service.save_data()

    # --- Helper ---
    async def _fetch_todays_chat_log(self, channel):
        today_start = datetime.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        logs = []
        async for msg in channel.history(after=today_start, limit=None, oldest_first=True):
            if msg.content.startswith("/"): continue
            role = "AI" if msg.author.id == self.bot.user.id else "User"
            content = msg.content
            if msg.attachments: content += " [画像/ファイル]"
            logs.append(f"{role}: {content}")
        return "\n".join(logs)

    # --- 定期タスク ---
    @tasks.loop(minutes=1)
    async def check_reminders_loop(self):
        due, changed = self.task_service.check_due_reminders()
        if due:
            ch = self.bot.get_channel(self.channel_id)
            if ch:
                for r in due:
                    u = self.bot.get_user(r['user_id'])
                    m = u.mention if u else ""
                    t = datetime.datetime.fromisoformat(r['time']).strftime('%H:%M')
                    await ch.send(f"{m} ⏰ **{r['content']}** ({t})")
        if changed: await self.task_service.save_data()

    @tasks.loop(minutes=5)
    async def check_schedule_loop(self):
        events = await self.calendar_service.get_upcoming_events(minutes=15)
        ch = self.bot.get_channel(self.channel_id)
        if not ch: return
        now = datetime.datetime.now(JST)
        for e in events:
            if 'dateTime' not in e.get('start', {}): continue
            start = datetime.datetime.fromisoformat(e['start']['dateTime'])
            if 540 <= (start - now).total_seconds() <= 660:
                if e['id'] not in self.task_service.notified_event_ids:
                    self.task_service.notified_event_ids.add(e['id'])
                    await ch.send(f"🔔 あと10分で「**{e.get('summary','予定')}**」の時間だよ！")

    @tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=JST))
    async def morning_greeting_loop(self):
        ch = self.bot.get_channel(self.channel_id)
        if not ch: return
        info_text = await self.info_service.get_info_summary()
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        schedule_text = await self.calendar_service.list_events_for_date(today_str)
        
        prompt = f"朝6時です。以下の情報で「おはよう」のメッセージを作成。\n\n【情報】\n{info_text}\n\n【予定】\n{schedule_text}\n\n指示: 短く明るく。"
        try:
            resp = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=prompt)
            await ch.send(resp.text)
            self.task_service.update_last_interaction()
            await self.task_service.save_data()
        except Exception as e: logging.error(f"Morning Error: {e}")

    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def nightly_reflection_loop(self):
        ch = self.bot.get_channel(self.channel_id)
        if not ch: return
        chat_log = await self._fetch_todays_chat_log(ch)
        prompt = f"夜22時です。今日のログを見て、労う質問を1つ投げかけて。\n\n{chat_log}\n\n指示: 親しみやすく。"
        try:
            resp = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=prompt)
            await ch.send(resp.text)
            self.task_service.update_last_interaction()
            await self.task_service.save_data()
        except Exception as e: logging.error(f"Nightly Error: {e}")

    @tasks.loop(minutes=60)
    async def inactivity_check_loop(self):
        ch = self.bot.get_channel(self.channel_id)
        if not ch: return
        now = datetime.datetime.now(JST)
        if 0 <= now.hour < 6: return # 深夜は停止

        # 最終会話から12時間経過で発動
        if (now - self.task_service.last_interaction) > datetime.timedelta(hours=12):
            # 直近が自分ならスキップ
            try:
                last_msg = [msg async for msg in ch.history(limit=1)]
                if last_msg and last_msg[0].author.id == self.bot.user.id: return
                
                prompt = "12時間返信がないユーザーに、短く声をかけて（例：生きてる？）。"
                resp = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=prompt)
                await ch.send(resp.text)
                
                self.task_service.update_last_interaction()
                await self.task_service.save_data()
            except Exception as e: logging.error(f"Inactivity Error: {e}")

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_summary_loop(self):
        ch = self.bot.get_channel(self.channel_id)
        if not ch: return
        today = datetime.datetime.now(JST)
        chat_log = await self._fetch_todays_chat_log(ch)
        weather_info, _, _ = await self.info_service.get_weather()
        fitbit_stats = {}
        if self.fitbit_service: fitbit_stats = await self.fitbit_service.get_stats(today) or {}

        prompt = f"日記作成。Markdown形式。\n天気:{weather_info}\nFitbit:{fitbit_stats}\nログ:\n{chat_log}\n指示:見出し整理、AIコメント含む。"
        
        try:
            resp = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=prompt)
            diary_body = resp.text
            
            # Obsidian保存
            if self.fitbit_service: await self.fitbit_service.update_daily_note_with_stats(today, fitbit_stats)
            
            service = self.drive_service.get_service()
            date_str = today.strftime("%Y-%m-%d")
            daily_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, "DailyNotes")
            f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
            
            if f_id:
                content = await self.drive_service.read_text_file(service, f_id)
                new_content = content + f"\n\n---\n### 🤖 AI Daily Report\n{diary_body}"
                await self.drive_service.update_text(service, f_id, new_content)
                await ch.send("✅ 日記を保存しました🌙")
        except Exception as e:
            logging.error(f"Daily Summary Error: {e}")
            await ch.send("⚠️ 日記保存エラー")

    # --- 会話生成 ---
    async def _generate_reply(self, channel, inputs: list, extra_context="", ignore_msg_id=None):
        if not self.gemini_client: return None
        now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')
        
        task_info = "特になし"
        if self.task_service.current_task:
            ct = self.task_service.current_task
            elapsed = int((datetime.datetime.now(JST) - ct['start']).total_seconds() / 60)
            task_info = f"「{ct['name']}」を実行中（{elapsed}分経過）"

        # ツール定義：検索機能(search_memory)を追加
        tools = [
            types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name="check_schedule", description="指定日の予定確認",
                    parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING)}, required=["date"])
                ),
                types.FunctionDeclaration(
                    name="create_calendar_event", description="予定作成",
                    parameters=types.Schema(type=types.Type.OBJECT, properties={
                        "summary": types.Schema(type=types.Type.STRING),
                        "start_time": types.Schema(type=types.Type.STRING),
                        "end_time": types.Schema(type=types.Type.STRING)
                    }, required=["summary", "start_time", "end_time"])
                ),
                types.FunctionDeclaration(
                    name="search_memory", description="過去のメモや日記をキーワード検索する",
                    parameters=types.Schema(type=types.Type.OBJECT, properties={"keywords": types.Schema(type=types.Type.STRING)}, required=["keywords"])
                )
            ])
        ]

        system_prompt = (
            f"あなたはユーザーの親しいパートナーAIです。\n"
            f"現在日時: {now_str}\n"
            f"現在のタスク状態: {task_info}\n"
            f"ユーザーの文脈: {extra_context}\n"
            f"過去のことは `search_memory` で検索可能。\n"
            f"返答は短く、親しみやすく。"
        )

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        recent_msgs = await self._build_conversation_context(channel, limit=20, ignore_msg_id=ignore_msg_id)
        for msg in recent_msgs:
            contents.append(types.Content(role=msg['role'], parts=[types.Part.from_text(text=msg['text'])]))
        
        user_parts = []
        for inp in inputs:
            if isinstance(inp, str): user_parts.append(types.Part.from_text(text=inp))
            else: user_parts.append(inp)
        if user_parts: contents.append(types.Content(role="user", parts=user_parts))

        config = types.GenerateContentConfig(tools=tools, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))

        try:
            response = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=contents, config=config)
            
            if response.function_calls:
                call = response.function_calls[0]
                tool_result = "実行失敗"
                
                if call.name == "check_schedule":
                    tool_result = await self.calendar_service.list_events_for_date(call.args.get("date"))
                elif call.name == "create_calendar_event":
                    tool_result = await self.calendar_service.create_event(call.args.get("summary"), call.args.get("start_time"), call.args.get("end_time"))
                elif call.name == "search_memory":
                    # DriveServiceの検索を実行
                    tool_result = await self.drive_service.search_markdown_files(call.args.get("keywords"))
                
                contents.append(response.candidates[0].content)
                contents.append(types.Content(role="user", parts=[types.Part.from_function_response(name=call.name, response={"result": tool_result})]))
                final_response = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=contents)
                return final_response.text
            
            return response.text
        except Exception as e:
            logging.error(f"GenAI Error: {e}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.channel_id: return
        
        # 最終会話日時を更新して保存
        self.task_service.update_last_interaction()
        # 頻繁な保存を避けるため、ここではメモリ更新のみとし、保存は定期タスクか重要なアクション時のみにする運用も可
        # ここでは確実性をとって保存しておく（IO負荷が気になるなら間引く）
        # await self.task_service.save_data() 
        
        text = message.content.strip()
        extra_ctx = ""

        # Task & Reminder
        rem_time = self.task_service.parse_and_add_reminder(text, message.author.id)
        if rem_time:
            extra_ctx += f"\n【リマインダー】{rem_time}にセットしたよ。"
            await self.task_service.save_data()

        if any(w in text for w in ["開始", "やる", "作業", "start"]):
            if not self.task_service.current_task:
                task_name = text.replace("開始", "").replace("やる", "").replace("作業", "").strip() or "作業"
                self.task_service.start_task(task_name)
                extra_ctx += f"\n【タスク】「{task_name}」を開始。"
                await self.task_service.save_data()
        elif any(w in text for w in ["終了", "終わった", "完了", "finish"]):
            if self.task_service.current_task:
                t_name, duration = self.task_service.finish_task() or ("", 0)
                extra_ctx += f"\n【タスク】「{t_name}」を終了（{duration}分）。"
                await self.task_service.save_data()

        # WebClip
        url_match = re.search(r'https?://\S+', text)
        if url_match:
            async with message.channel.typing():
                result = await self.webclip_service.process_url(url_match.group(), text, message)
                if result: extra_ctx += f"\n{result['summary']}"

        # Reply
        input_parts = [text]
        async with message.channel.typing():
            reply = await self._generate_reply(message.channel, input_parts, extra_context=extra_ctx, ignore_msg_id=message.id)
            if reply: await message.reply(reply)

async def setup(bot):
    await bot.add_cog(PartnerCog(bot))