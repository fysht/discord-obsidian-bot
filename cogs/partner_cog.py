import os
import json
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types
import datetime
from datetime import timedelta
import zoneinfo
import re
import aiohttp
import io
import xml.etree.ElementTree as ET

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# 外部ライブラリ
try: 
    from web_parser import parse_url_with_readability
except ImportError: 
    parse_url_with_readability = None

try: 
    from utils.obsidian_utils import update_section
except ImportError: 
    def update_section(content, text, header):
        return f"{content}\n\n{header}\n{text}"

# --- 定数 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
DATA_FILE_NAME = "partner_data.json"
HISTORY_FILE_NAME = "partner_chat_history.json"
BOT_FOLDER = ".bot"
TOKEN_FILE = 'token.json'

SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar']

JMA_AREA_CODE = "330000" 
JMA_URL = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"
NEWS_RSS_URL = "https://news.yahoo.co.jp/rss/topics/top-picks.xml"

URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:[/?][\w\-.?=&%@+]*)?')
YOUTUBE_REGEX = re.compile(r'(youtube\.com|youtu\.be)')
REMINDER_REGEX_MIN = re.compile(r'(\d+)分後')
REMINDER_REGEX_TIME = re.compile(r'(\d{1,2})[:時](\d{0,2})')

class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("MEMO_CHANNEL_ID") or os.getenv("PARTNER_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None

        self.session = aiohttp.ClientSession()
        
        # State
        self.reminders = []
        self.current_task = None
        self.last_interaction = datetime.datetime.now(JST)
        self.user_name = "あなた"
        self.notified_event_ids = set()
        self.is_ready = False

    async def cog_load(self):
        await self._load_data_from_drive()
        self.inactivity_check_task.start()
        self.daily_organize_task.start()
        self.morning_greeting_task.start()
        self.calendar_check_task.start()
        self.reminder_check_task.start()
        self.nightly_reflection_task.start()
        self.is_ready = True

    async def cog_unload(self):
        self.inactivity_check_task.cancel()
        self.nightly_reflection_task.cancel()
        self.daily_organize_task.cancel()
        self.morning_greeting_task.cancel()
        self.calendar_check_task.cancel()
        self.reminder_check_task.cancel()
        await self.session.close()
        await self._save_data_to_drive()

    # --- Drive / Calendar I/O ---
    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request()); open(TOKEN_FILE,'w').write(creds.to_json())
                except: return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    def _get_calendar_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE): creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        return build('calendar', 'v3', credentials=creds) if creds else None

    async def _find_file(self, service, parent_id, name):
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute())
            files = res.get('files', [])
            return files[0]['id'] if files else None
        except: return None

    async def _load_data_from_drive(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        b_folder = await self._find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return

        f_id = await self._find_file(service, b_folder, DATA_FILE_NAME)
        if f_id:
            try:
                request = service.files().get_media(fileId=f_id)
                fh = io.BytesIO()
                from googleapiclient.http import MediaIoBaseDownload
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                data = json.loads(fh.getvalue().decode('utf-8'))
                
                self.reminders = data.get('reminders', [])
                ct = data.get('current_task')
                if ct: self.current_task = {'name': ct['name'], 'start': datetime.datetime.fromisoformat(ct['start'])}
                li = data.get('last_interaction')
                if li: self.last_interaction = datetime.datetime.fromisoformat(li)
            except: pass

    async def _save_data_to_drive(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        ct_save = None
        if self.current_task:
            ct_save = {'name': self.current_task['name'], 'start': self.current_task['start'].isoformat()}

        data = {
            'reminders': self.reminders,
            'current_task': ct_save,
            'last_interaction': self.last_interaction.isoformat()
        }

        b_folder = await self._find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return 
        
        f_id = await self._find_file(service, b_folder, DATA_FILE_NAME)
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')), mimetype='application/json')
        
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': DATA_FILE_NAME, 'parents': [b_folder]}, media_body=media).execute())

    async def _upload_text(self, service, parent_id, name, content):
        loop = asyncio.get_running_loop()
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        await loop.run_in_executor(None, lambda: service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=media).execute())

    # --- Tool Functions ---
    async def _search_drive_notes(self, keywords: str):
        """Google Drive検索"""
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return "検索エラー: Driveに接続できません"
        query = f"fullText contains '{keywords}' and mimeType = 'text/markdown' and trashed = false"
        try:
            results = await loop.run_in_executor(None, lambda: service.files().list(q=query, pageSize=3, fields="files(id, name)").execute())
            files = results.get('files', [])
            if not files: return f"「{keywords}」は見つからなかったよ。"
            search_results = []
            for file in files:
                try:
                    from googleapiclient.http import MediaIoBaseDownload
                    request = service.files().get_media(fileId=file['id'])
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: _, done = downloader.next_chunk()
                    content = fh.getvalue().decode('utf-8')
                    snippet = content[:800] 
                    search_results.append(f"【{file['name']}】\n{snippet}\n")
                except: continue
            return f"検索結果:\n" + "\n---\n".join(search_results)
        except Exception as e: return f"エラー: {e}"

    async def _create_calendar_event(self, summary: str, start_time: str, end_time: str, location: str = "", description: str = ""):
        """カレンダー登録"""
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_calendar_service)
        if not service: return "エラー: カレンダーに接続できません（権限を確認してください）"

        event_body = {
            'summary': summary,
            'location': location,
            'description': description,
            'start': {'dateTime': start_time, 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': end_time, 'timeZone': 'Asia/Tokyo'},
        }

        try:
            event = await loop.run_in_executor(None, lambda: service.events().insert(calendarId=self.calendar_id, body=event_body).execute())
            return f"予定を作成しました: {event.get('htmlLink')}"
        except Exception as e:
            return f"カレンダー登録エラー: {e}"

    # --- Helpers ---
    async def _get_weather_info(self):
        try:
            async with self.session.get(JMA_URL) as resp:
                if resp.status != 200: return "不明"
                data = await resp.json()
                weather = data[0]["timeSeries"][0]["areas"][0]["weathers"][0]
                temps = data[0]["timeSeries"][2]["areas"][0].get("temps", [])
                temp_str = f"最高{temps[1]}℃" if len(temps) > 1 else ""
                return f"{weather} {temp_str}".strip()
        except: return "不明"
    
    async def _get_news_headlines(self):
        try:
            async with self.session.get(NEWS_RSS_URL) as resp:
                if resp.status != 200: return "（取得失敗）"
                content = await resp.text()
                root = ET.fromstring(content)
                items = root.findall('.//item')
                headlines = [item.find('title').text for item in items[:3]]
                return "\n".join([f"・{h}" for h in headlines])
        except: return "（ニュースなし）"

    async def _analyze_url_content(self, url):
        info = {"type": "unknown", "title": "URL", "content": ""}
        if YOUTUBE_REGEX.search(url):
            info["type"] = "youtube"
            try:
                oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
                async with self.session.get(oembed_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        info["title"] = data.get("title", "YouTube")
                        info["content"] = f"Channel: {data.get('author_name')}"
            except: pass
        elif parse_url_with_readability:
            try:
                title, content = await asyncio.to_thread(parse_url_with_readability, url)
                info["type"] = "web"
                info["title"] = title
                info["content"] = content[:800] + "..."
            except: pass
        return info

    def _parse_reminder(self, text, user_id):
        now = datetime.datetime.now(JST)
        target_time = None
        content = "時間だよ！"
        m_match = REMINDER_REGEX_MIN.search(text)
        if m_match:
            mins = int(m_match.group(1))
            target_time = now + timedelta(minutes=mins)
            content = text.replace(m_match.group(0), "").strip() or "指定の時間だよ！"
        t_match = REMINDER_REGEX_TIME.search(text)
        if t_match:
            hour = int(t_match.group(1))
            minute = int(t_match.group(2)) if t_match.group(2) else 0
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time < now: target_time += timedelta(days=1)
            content = text.replace(t_match.group(0), "").strip() or "指定の時間だよ！"
        if target_time:
            self.reminders.append({'time': target_time.isoformat(), 'content': content, 'user_id': user_id})
            return target_time.strftime('%H:%M')
        return None

    # --- Context ---
    async def _build_conversation_context(self, channel, limit=50):
        messages = []
        async for msg in channel.history(limit=limit, oldest_first=False):
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments: text += " [メディア送信]"
            messages.append({'role': role, 'text': text})
        return list(reversed(messages))

    async def _fetch_yesterdays_journal(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return ""
        yesterday = (datetime.datetime.now(JST) - timedelta(days=1)).strftime('%Y-%m-%d')
        daily_folder = await self._find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: return ""
        f_id = await self._find_file(service, daily_folder, f"{yesterday}.md")
        if not f_id: return ""
        try:
            from googleapiclient.http import MediaIoBaseDownload
            request = service.files().get_media(fileId=f_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            content = fh.getvalue().decode('utf-8')
            match = re.search(r'## 📝 Journal\s*(.*?)(?=\n##|$)', content, re.DOTALL)
            if match: return f"【昨日の日記】\n{match.group(1).strip()}"
        except: pass
        return ""

    async def _fetch_todays_chat_log(self, channel):
        today_start = datetime.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        logs = []
        async for msg in channel.history(after=today_start, limit=None, oldest_first=True):
            if msg.content.startswith("/"): continue
            role = "AI" if msg.author.id == self.bot.user.id else "User"
            logs.append(f"{role}: {msg.content}")
        return "\n".join(logs)

    # --- Chat Generation ---
    async def _generate_reply(self, channel, inputs: list, trigger_type="reply", extra_context=""):
        if not self.gemini_client: return None
        
        weather = await self._get_weather_info()
        now = datetime.datetime.now(JST)
        now_str = now.strftime('%Y-%m-%d %H:%M')
        yesterday_memory = await self._fetch_yesterdays_journal()
        
        task_info = "特になし"
        if self.current_task:
            elapsed = int((datetime.datetime.now(JST) - self.current_task['start']).total_seconds() / 60)
            task_info = f"「{self.current_task['name']}」を実行中（{elapsed}分経過）"

        # ツール定義
        tools = [
            types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name="search_memory",
                    description="過去の日記やメモをGoogle Driveから検索する。",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={"keywords": types.Schema(type=types.Type.STRING, description="検索キーワード")},
                        required=["keywords"]
                    )
                ),
                types.FunctionDeclaration(
                    name="create_calendar_event",
                    description="Googleカレンダーに予定を追加する。ユーザーから日時と内容を聞き出してから実行すること。",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "summary": types.Schema(type=types.Type.STRING, description="予定のタイトル"),
                            "start_time": types.Schema(type=types.Type.STRING, description="開始日時 (ISO 8601: YYYY-MM-DDTHH:MM:SS)"),
                            "end_time": types.Schema(type=types.Type.STRING, description="終了日時 (ISO 8601). 指定がなければ開始の1時間後"),
                            "location": types.Schema(type=types.Type.STRING, description="場所"),
                            "description": types.Schema(type=types.Type.STRING, description="詳細メモ"),
                        },
                        required=["summary", "start_time", "end_time"]
                    )
                )
            ])
        ]

        system_prompt = f"""
        あなたはユーザー（{self.user_name}）の親しいパートナー（20代女性）です。
        LINEでやり取りするような、**温かみのあるタメ口**で話してください。
        
        **現在の状況:**
        - 現在日時: {now_str}
        - 天気: {weather}
        - ユーザーの状態: {task_info}
        {extra_context}
        {yesterday_memory}

        **行動指針:**
        1. **自然な会話:** 短く（1〜3文）、共感やリアクションを入れる。
        2. **記憶:** 過去のことを聞かれたら `search_memory` で調べて。
        3. **予定作成:** ユーザーが「予定を入れたい」と言ったら、**日時・内容・場所**を聞き出し、揃ったら `Calendar` を実行して。
           ※ 日時が曖昧（例：「来週の土曜」）なら、具体的な日付を計算してツールに渡すこと。
        4. **リマインダー:** セットされたら快諾して。
        5. **アドバイス禁止:** 「日記に書こう」などは言わない。

        **トリガー:** {trigger_type}
        （morning: 天気・予定・昨日の日記・ニュースを含めて起こして）
        （nightly_reflection: 今日の会話を踏まえて1日を振り返る質問をして）
        """

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        
        recent_msgs = await self._build_conversation_context(channel, limit=30)
        for msg in recent_msgs:
            contents.append(types.Content(role=msg['role'], parts=[types.Part.from_text(text=msg['text'])]))
        
        user_parts = []
        for inp in inputs:
            if isinstance(inp, str): user_parts.append(types.Part.from_text(text=inp))
            else: user_parts.append(inp)
        
        if user_parts: contents.append(types.Content(role="user", parts=user_parts))
        else: contents.append(types.Content(role="user", parts=[types.Part.from_text(text="(きっかけ)")]))

        config = types.GenerateContentConfig(
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
        )

        try:
            # 1st Generate
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.5-pro',
                contents=contents,
                config=config
            )

            # Function Call Check
            if response.function_calls:
                function_call = response.function_calls[0]
                tool_result = ""
                
                if function_call.name == "search_memory":
                    tool_result = await self._search_drive_notes(function_call.args["keywords"])
                
                elif function_call.name == "create_calendar_event":
                    args = function_call.args
                    tool_result = await self._create_calendar_event(
                        summary=args["summary"],
                        start_time=args["start_time"],
                        end_time=args["end_time"],
                        location=args.get("location", ""),
                        description=args.get("description", "")
                    )

                # Send result back
                contents.append(response.candidates[0].content)
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_function_response(
                        name=function_call.name,
                        response={"result": tool_result}
                    )]
                ))
                
                # 2nd Generate (Final Response)
                response_final = await self.gemini_client.aio.models.generate_content(
                    model='gemini-2.5-pro',
                    contents=contents
                )
                return response_final.text

            return response.text

        except Exception as e:
            logging.error(f"GenAI Error: {e}")
            return None

    # --- Event Listener ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.channel_id: return

        self.user_name = message.author.display_name
        text = message.content.strip()
        
        if text in ["まとめ", "途中経過", "整理して", "今の状態"]:
            await self._show_interim_summary(message)
            return

        input_parts = []
        extra_ctx = ""

        reminder_time = self._parse_reminder(text, message.author.id)
        if reminder_time:
            extra_ctx += f"\n【システム通知】リマインダーセット完了（時間: {reminder_time}）。「了解！」と返して。"
            await self._save_data_to_drive()

        url_match = URL_REGEX.search(text)
        if url_match:
            async with message.channel.typing():
                url_info = await self._analyze_url_content(url_match.group(0))
                text += f"\n(URL情報: {url_info['title']} - {url_info['content']})"

        if text: input_parts.append(text)
        for att in message.attachments:
            if att.content_type.startswith('image/'):
                input_parts.append(types.Part.from_bytes(data=await att.read(), mime_type=att.content_type))
            elif att.content_type.startswith('audio/'):
                input_parts.append(types.Part.from_bytes(data=await att.read(), mime_type=att.content_type))

        if not input_parts: return

        if any(w in text for w in ["開始", "やる", "読む", "作業"]):
            if not self.current_task: 
                self.current_task = {'name': text, 'start': datetime.datetime.now(JST)}
                await self._save_data_to_drive()
        elif any(w in text for w in ["終了", "終わった", "完了"]):
            self.current_task = None
            await self._save_data_to_drive()

        self.last_interaction = datetime.datetime.now(JST)
        await self._save_data_to_drive()

        async with message.channel.typing():
            reply = await self._generate_reply(message.channel, input_parts, trigger_type="reply", extra_context=extra_ctx)
            if reply:
                await message.channel.send(reply)

    # --- Interim Summary ---
    async def _show_interim_summary(self, message):
        async with message.channel.typing():
            log_text = await self._fetch_todays_chat_log(message.channel)
            if not log_text:
                await message.reply("今日はまだ何も話してないね！")
                return

            prompt = f"""
            以下は今日の会話ログです。現時点での情報を整理して、ユーザーに見せてください。
            
            **フォーマット:**
            ```markdown
            ## 📝 今日の日記（仮）
            (ここまでの出来事や感情のまとめ)

            ## 📎 クリップ＆メモ
            - [タイトル](URL) : 感想
            - メモ内容
            ```
            
            --- Chat Log ---
            {log_text}
            """
            try:
                response = await self.gemini_client.aio.models.generate_content(
                    model='gemini-2.5-pro',
                    contents=prompt
                )
                await message.reply(f"今のところ、こんな感じでまとまってるよ！👇\n\n{response.text}")
            except Exception as e:
                await message.reply(f"ごめん、うまくまとめられなかった💦 ({e})")

    # --- Scheduled Tasks ---

    @tasks.loop(minutes=1)
    async def reminder_check_task(self):
        now = datetime.datetime.now(JST)
        remaining = []
        changed = False
        for rem in self.reminders:
            target = datetime.datetime.fromisoformat(rem['time'])
            if now >= target:
                channel = self.bot.get_channel(self.channel_id)
                if channel:
                    user = self.bot.get_user(rem['user_id'])
                    mention = user.mention if user else ""
                    content = rem.get('content', '時間だよ！').replace("教えて", "").replace("声かけて", "")
                    await channel.send(f"{mention} ⏰ **{content}** ({target.strftime('%H:%M')})")
                    changed = True
            else:
                remaining.append(rem)
        self.reminders = remaining
        if changed: await self._save_data_to_drive()

    @tasks.loop(minutes=5)
    async def calendar_check_task(self):
        if not self.channel_id: return
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_calendar_service)
        if not service: return

        now = datetime.datetime.now(JST)
        try:
            events_res = await loop.run_in_executor(None, lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=now.isoformat(),
                timeMax=(now + timedelta(minutes=15)).isoformat(), singleEvents=True).execute())
            events = events_res.get('items', [])
            
            for event in events:
                if 'dateTime' not in event.get('start', {}): continue
                start = datetime.datetime.fromisoformat(event['start']['dateTime'])
                if 540 <= (start - now).total_seconds() <= 660:
                    eid = event['id']
                    if eid in self.notified_event_ids: continue
                    self.notified_event_ids.add(eid)
                    channel = self.bot.get_channel(self.channel_id)
                    if channel:
                        msg = f"ねえ、あと10分で「{event['summary']}」だよ！準備OK？"
                        await channel.send(msg)
        except: pass

    @tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=JST))
    async def morning_greeting_task(self):
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        news = await self._get_news_headlines()
        prompt = f"""
        (朝6時だよ。以下の情報を元に、パートナーとして爽やかに起こして)
        【今日のニュース】\n{news}
        **指示:**
        1. 天気と予定を伝える。
        2. 昨日の日記の内容（もしあれば）に触れて気遣う。
        3. ニュースの中から1つ選んで「そういえば〜」と話題を振る。
        4. 明るく送り出す。
        """
        reply = await self._generate_reply(channel, [prompt], trigger_type="morning")
        if reply: await channel.send(reply)

    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def nightly_reflection_task(self):
        """22時の振り返り質問"""
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        
        # 当日の全ログを参照
        today_log = await self._fetch_todays_chat_log(channel)
        context_str = f"【今日の会話ログ（朝〜現在）】\n{today_log}\n"
        
        prompt_msg = "(もう22時だね。上記の『今日の会話ログ』をすべて読み、その中にある具体的な出来事（食べたもの、行った場所、仕事の内容、感情など）をピックアップして、1日を振り返るための質問を1つ投げかけて。定型文ではなく、今日だけの質問にして)"
        
        reply = await self._generate_reply(channel, [prompt_msg], trigger_type="nightly_reflection", extra_context=context_str)
        if reply: await channel.send(reply)

    @tasks.loop(minutes=60)
    async def inactivity_check_task(self):
        if not self.channel_id: return
        now = datetime.datetime.now(JST)
        if (now - self.last_interaction) > timedelta(hours=12) and not (1 <= now.hour <= 6):
            channel = self.bot.get_channel(self.channel_id)
            if not channel: return
            last_msg = None
            async for m in channel.history(limit=1): last_msg = m
            if last_msg and last_msg.author.id == self.bot.user.id: return
            reply = await self._generate_reply(channel, ["(12時間連絡がないね。何かあった？軽く声かけて)"], trigger_type="inactivity")
            if reply:
                await channel.send(reply)
                self.last_interaction = now
                await self._save_data_to_drive()

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        log_text = await self._fetch_todays_chat_log(channel)
        if not log_text: return
        logging.info("Starting nightly organization...")
        prompt = f"""
        今日の会話ログを分析し、JSON形式で整理してください。
        1. `diary`: 今日の出来事や感情を「である調」で日記にする（300字）。
        2. `webclips`: URL情報のまとめ。
        3. `youtube`: 動画のまとめ。
        4. `recipes`: レシピまとめ。
        5. `memos`: その他メモ。
        JSON: {{ "diary": "...", "webclips": [], "youtube": [], "recipes": [], "memos": [] }}
        --- Chat Log ---
        {log_text}
        """
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.5-pro',
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type='application/json')
            )
            result = json.loads(response.text)
            today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            await self._execute_organization(result, today_str)
            await channel.send("（今日の分、日記にまとめておいたよ！おやすみ🌙）")
        except Exception as e:
            logging.error(f"Nightly Task Error: {e}")

    async def _execute_organization(self, data, date_str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        if data.get('webclips'):
            folder_id = await self._find_file(service, self.drive_folder_id, "WebClips")
            if not folder_id: folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "WebClips")
            for item in data['webclips']:
                t = item.get('title','Clip'); safe_t = re.sub(r'[\\/*?:"<>|]', "", t)[:30]
                await self._upload_text(service, folder_id, f"{date_str}-{safe_t}.md", f"# {t}\nURL: {item.get('url')}\n\n## Note\n{item.get('note','')}")

        if data.get('youtube'):
            folder_id = await self._find_file(service, self.drive_folder_id, "YouTube")
            if not folder_id: folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "YouTube")
            for item in data['youtube']:
                t = item.get('title','Video'); safe_t = re.sub(r'[\\/*?:"<>|]', "", t)[:30]
                await self._upload_text(service, folder_id, f"{date_str}-{safe_t}.md", f"# {t}\nURL: {item.get('url')}\n\n## Memo\n{item.get('note','')}")

        if data.get('recipes'):
            folder_id = await self._find_file(service, self.drive_folder_id, "Recipes")
            if not folder_id: folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "Recipes")
            for item in data['recipes']:
                t = item.get('name','Recipe'); safe_t = re.sub(r'[\\/*?:"<>|]', "", t)[:30]
                await self._upload_text(service, folder_id, f"{date_str}-{safe_t}.md", f"# {t}\nURL: {item.get('url')}\n\n## Note\n{item.get('note','')}")

        daily_folder = await self._find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: daily_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")
        f_id = await self._find_file(service, daily_folder, f"{date_str}.md")
        cur = f"# Daily Note {date_str}\n"
        if f_id:
            from googleapiclient.http import MediaIoBaseDownload
            req = service.files().get_media(fileId=f_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done: _, done = downloader.next_chunk()
            cur = fh.getvalue().decode('utf-8')

        updates = []
        if data.get('diary'): updates.append(f"## 📝 Journal\n{data['diary']}")
        links = []
        for cat in ['webclips','youtube','recipes']:
            for item in data.get(cat, []):
                t = item.get('title') or item.get('name')
                if t: links.append(f"- [{t}]({item.get('url')})")
        if links: updates.append("## 🔗 Links\n" + "\n".join(links))
        if data.get('memos'): updates.append("## 📌 Memos\n" + "\n".join([f"- {m}" for m in data['memos']]))

        new_c = cur + "\n\n" + "\n\n".join(updates)
        media = MediaIoBaseUpload(io.BytesIO(new_c.encode('utf-8')), mimetype='text/markdown', resumable=True)
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [daily_folder]}, media_body=media).execute())

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))