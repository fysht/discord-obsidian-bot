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
import base64

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# 外部ライブラリ (BeautifulSoupの読み込みを復元)
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try: 
    from web_parser import parse_url_with_readability
except ImportError: 
    parse_url_with_readability = None

try: 
    from utils.obsidian_utils import update_section
except ImportError: 
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

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
YOUTUBE_REGEX = re.compile(r'(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})')
REMINDER_REGEX_MIN = re.compile(r'(\d+)分後')
REMINDER_REGEX_TIME = re.compile(r'(\d{1,2})[:時](\d{0,2})')

class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("MEMO_CHANNEL_ID") or os.getenv("PARTNER_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        
        # Fitbit Credentials
        self.fitbit_client_id = os.getenv("FITBIT_CLIENT_ID")
        self.fitbit_client_secret = os.getenv("FITBIT_CLIENT_SECRET")
        self.fitbit_refresh_token = os.getenv("FITBIT_REFRESH_TOKEN")

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

    async def _create_folder(self, service, parent_id, name):
        loop = asyncio.get_running_loop()
        meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        file = await loop.run_in_executor(None, lambda: service.files().create(body=meta, fields='id').execute())
        return file.get('id')

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
                
                if 'fitbit_refresh_token' in data:
                    self.fitbit_refresh_token = data['fitbit_refresh_token']
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
            'last_interaction': self.last_interaction.isoformat(),
            'fitbit_refresh_token': self.fitbit_refresh_token
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
        existing_id = await self._find_file(service, parent_id, name)
        if existing_id:
            await loop.run_in_executor(None, lambda: service.files().update(fileId=existing_id, media_body=media).execute())
            return existing_id
        else:
            f = await loop.run_in_executor(None, lambda: service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=media, fields='id, webViewLink').execute())
            return f.get('id'), f.get('webViewLink')

    # --- Fitbit Logic ---
    async def _refresh_fitbit_token(self):
        if not (self.fitbit_client_id and self.fitbit_client_secret and self.fitbit_refresh_token): return None
        
        auth_header = base64.b64encode(f"{self.fitbit_client_id}:{self.fitbit_client_secret}".encode()).decode()
        headers = {'Authorization': f'Basic {auth_header}', 'Content-Type': 'application/x-www-form-urlencoded'}
        data = {'grant_type': 'refresh_token', 'refresh_token': self.fitbit_refresh_token}
        
        try:
            async with self.session.post('https://api.fitbit.com/oauth2/token', headers=headers, data=data) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    self.fitbit_refresh_token = token_data['refresh_token']
                    await self._save_data_to_drive()
                    return token_data['access_token']
                else:
                    logging.error(f"Fitbit Refresh Error: {await resp.text()}")
                    return None
        except Exception as e:
            logging.error(f"Fitbit Connection Error: {e}")
            return None

    async def _get_fitbit_stats(self, date_str):
        token = await self._refresh_fitbit_token()
        if not token: return {}
        headers = {'Authorization': f'Bearer {token}'}
        stats = {}
        try:
            url = f"https://api.fitbit.com/1/user/-/activities/date/{date_str}.json"
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    s = data.get('summary', {})
                    stats['steps'] = s.get('steps', 0)
                    stats['calories'] = s.get('caloriesOut', 0)
                    distances = s.get('distances', [])
                    stats['distance'] = next((d['distance'] for d in distances if d['activity'] == 'total'), 0)
                    stats['floors'] = s.get('floors', 0)
        except Exception as e: logging.error(f"Fitbit Act Error: {e}")

        try:
            url = f"https://api.fitbit.com/1/user/-/activities/heart/date/{date_str}/1d.json"
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    try: stats['resting_hr'] = data['activities-heart'][0]['value'].get('restingHeartRate', 'N/A')
                    except: stats['resting_hr'] = "N/A"
        except: pass

        try:
            url = f"https://api.fitbit.com/1.2/user/-/sleep/date/{date_str}.json"
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    summary = data.get('summary', {})
                    stats['sleep_minutes'] = summary.get('totalMinutesAsleep', 0)
        except: pass
        return stats

    # --- Tool Functions ---
    async def _search_drive_notes(self, keywords: str):
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

    async def _check_schedule(self, date_str: str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_calendar_service)
        if not service: return "エラー: カレンダーに接続できません"
        try:
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            time_min = dt.replace(hour=0, minute=0, second=0).isoformat() + 'Z'
            time_max = dt.replace(hour=23, minute=59, second=59).isoformat() + 'Z'
            events_result = await loop.run_in_executor(None, lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime').execute())
            events = events_result.get('items', [])
            if not events: return f"{date_str} の予定は特にないみたいだよ。"
            result_text = f"【{date_str} の予定】\n"
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                summary = event.get('summary', '(タイトルなし)')
                if 'T' in start:
                    t = datetime.datetime.fromisoformat(start).strftime('%H:%M')
                    result_text += f"- {t} : {summary}\n"
                else:
                    result_text += f"- 終日 : {summary}\n"
            return result_text
        except Exception as e: return f"スケジュール確認エラー: {e}"

    async def _create_calendar_event(self, summary: str, start_time: str, end_time: str, location: str = "", description: str = ""):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_calendar_service)
        if not service: return "エラー: カレンダーに接続できません"
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
        except Exception as e: return f"カレンダー登録エラー: {e}"

    # --- Content Processing ---
    async def _fetch_url_metadata(self, url):
        if BeautifulSoup:
            try:
                async with self.session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        title = soup.title.string if soup.title else "No Title"
                        if YOUTUBE_REGEX.search(url): return "YouTube", title, None
                        else:
                            for script in soup(["script", "style"]): script.extract()
                            text = soup.get_text(separator="\n")
                            lines = [line.strip() for line in text.splitlines() if line.strip()]
                            return "Web", title, "\n".join(lines)[:8000]
            except: pass
        return "Web", "Unknown Title", "(本文取得失敗)"

    async def _process_and_save_content(self, message, url, content_type, title, raw_text):
        date_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:30]
        filename = f"{date_str}-{safe_title}.md"
        
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        final_content = ""
        folder_name = ""

        if content_type == "YouTube":
            folder_name = "YouTube"
            user_comment = message.content.replace(url, "").strip()
            final_content = f"# {title}\n\n## Note\n{user_comment}\n\n---\n**URL:** {url}\n**Saved at:** {datetime.datetime.now(JST)}"
        else:
            folder_name = "WebClips"
            if len(raw_text) < 50: return 
            prompt = f"以下のWeb記事をObsidian保存用にMarkdownで整理。\nタイトル: {title}\nURL: {url}\n\n{raw_text}"
            try:
                response = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=prompt)
                final_content = f"{response.text}\n\n---\n**Saved at:** {datetime.datetime.now(JST)}"
            except: return

        folder_id = await self._find_file(service, self.drive_folder_id, folder_name)
        if not folder_id: folder_id = await self._create_folder(service, self.drive_folder_id, folder_name)
        await self._upload_text(service, folder_id, filename, final_content)
        await message.reply(f"✅ {content_type}情報を保存したよ！\n📂 `{folder_name}/{filename}`")

    # --- Helpers ---
    async def _get_weather_stats(self):
        try:
            async with self.session.get(JMA_URL) as resp:
                if resp.status != 200: return "Unknown", "N/A", "N/A"
                data = await resp.json()
                weather = data[0]["timeSeries"][0]["areas"][0]["weathers"][0]
                temps = data[0]["timeSeries"][2]["areas"][0].get("temps", [])
                max_t = temps[1] if len(temps) > 1 else "N/A"
                min_t = temps[0] if len(temps) > 0 else "N/A"
                return weather.replace("\u3000", " "), max_t, min_t
        except: return "Unknown", "N/A", "N/A"

    async def _get_weather_info(self):
        w, max_t, _ = await self._get_weather_stats()
        return f"{w} (最高{max_t}℃)" if max_t != "N/A" else w
    
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

        tools = [
            types.Tool(function_declarations=[
                types.FunctionDeclaration(name="search_memory", description="過去の日記を検索", parameters=types.Schema(type=types.Type.OBJECT, properties={"keywords": types.Schema(type=types.Type.STRING)}, required=["keywords"])),
                types.FunctionDeclaration(name="check_schedule", description="指定日のカレンダー確認", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING)}, required=["date"])),
                types.FunctionDeclaration(name="create_calendar_event", description="カレンダー登録", parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "summary": types.Schema(type=types.Type.STRING), "start_time": types.Schema(type=types.Type.STRING), "end_time": types.Schema(type=types.Type.STRING),
                    "location": types.Schema(type=types.Type.STRING), "description": types.Schema(type=types.Type.STRING)}, required=["summary", "start_time"]))
            ])
        ]

        system_prompt = f"""
        あなたはユーザー（{self.user_name}）の親しいパートナー（20代女性）です。
        温かみのあるタメ口で話してください。
        **状況:** 時刻:{now_str}, 天気:{weather}, 状態:{task_info}
        {extra_context} {yesterday_memory}
        **指針:**
        1. 短く自然な会話。
        2. 過去のことは `search_memory`。
        3. 予定作成時はまず `check_schedule` で確認、空いていれば `Calendar`。重複時は確認。
        4. アドバイス禁止。
        """

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        recent_msgs = await self._build_conversation_context(channel, limit=30)
        for msg in recent_msgs: contents.append(types.Content(role=msg['role'], parts=[types.Part.from_text(text=msg['text'])]))
        user_parts = []
        for inp in inputs:
            if isinstance(inp, str): user_parts.append(types.Part.from_text(text=inp))
            else: user_parts.append(inp)
        if user_parts: contents.append(types.Content(role="user", parts=user_parts))
        else: contents.append(types.Content(role="user", parts=[types.Part.from_text(text="(きっかけ)")]))

        config = types.GenerateContentConfig(tools=tools, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))

        try:
            response = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=contents, config=config)
            if response.function_calls:
                function_call = response.function_calls[0]
                tool_result = ""
                if function_call.name == "search_memory": tool_result = await self._search_drive_notes(function_call.args["keywords"])
                elif function_call.name == "check_schedule": tool_result = await self._check_schedule(function_call.args["date"])
                elif function_call.name == "create_calendar_event":
                    args = function_call.args
                    summary = args.get("summary")
                    start_time = args.get("start_time")
                    end_time = args.get("end_time")
                    if summary and start_time:
                        if not end_time:
                            try: end_time = (datetime.datetime.fromisoformat(start_time) + timedelta(hours=1)).isoformat()
                            except: end_time = start_time
                        tool_result = await self._create_calendar_event(summary, start_time, end_time, args.get("location",""), args.get("description",""))
                    else: tool_result = "エラー: 引数不足"
                
                contents.append(response.candidates[0].content)
                contents.append(types.Content(role="user", parts=[types.Part.from_function_response(name=function_call.name, response={"result": tool_result})]))
                response_final = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=contents)
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
            extra_ctx += f"\n【システム通知】リマインダーセット完了（時間: {reminder_time}）。"
            await self._save_data_to_drive()
        url_match = URL_REGEX.search(text)
        if url_match:
            async with message.channel.typing():
                url = url_match.group(0)
                content_type, title, raw_text = await self._fetch_url_metadata(url)
                await self._process_and_save_content(message, url, content_type, title, raw_text)
                text += f"\n(URL情報: {content_type} '{title}' 保存済み)"
        if text: input_parts.append(text)
        for att in message.attachments:
            if att.content_type.startswith(('image/', 'audio/')):
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
            if reply: await message.channel.send(reply)

    # --- Interim Summary ---
    async def _show_interim_summary(self, message):
        async with message.channel.typing():
            log_text = await self._fetch_todays_chat_log(message.channel)
            if not log_text:
                await message.reply("今日はまだ何も話してないね！")
                return
            prompt = f"今日の会話ログを整理して表示。\n--- Chat Log ---\n{log_text}"
            try:
                response = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=prompt)
                await message.reply(f"今のところこんな感じ！👇\n\n{response.text}")
            except Exception as e: await message.reply(f"エラー: {e}")

    # --- Tasks ---
    @tasks.loop(minutes=1)
    async def reminder_check_task(self):
        now = datetime.datetime.now(JST)
        remaining = []
        changed = False
        for rem in self.reminders:
            target = datetime.datetime.fromisoformat(rem['time'])
            if now >= target:
                channel = self.bot.get_channel(self.channel_id)
                if channel: await channel.send(f"{self.bot.get_user(rem['user_id']).mention if self.bot.get_user(rem['user_id']) else ''} ⏰ **{rem.get('content')}** ({target.strftime('%H:%M')})")
                changed = True
            else: remaining.append(rem)
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
            events = (await loop.run_in_executor(None, lambda: service.events().list(calendarId=self.calendar_id, timeMin=now.isoformat(), timeMax=(now+timedelta(minutes=15)).isoformat(), singleEvents=True).execute())).get('items', [])
            for event in events:
                if 'dateTime' not in event.get('start', {}): continue
                start = datetime.datetime.fromisoformat(event['start']['dateTime'])
                if 540 <= (start - now).total_seconds() <= 660:
                    if event['id'] not in self.notified_event_ids:
                        self.notified_event_ids.add(event['id'])
                        channel = self.bot.get_channel(self.channel_id)
                        if channel: await channel.send(f"ねえ、あと10分で「{event['summary']}」だよ！")
        except: pass

    @tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=JST))
    async def morning_greeting_task(self):
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        news = await self._get_news_headlines()
        prompt = f"(朝6時。天気・予定・昨日の日記・ニュース({news})を含めて起こして)"
        reply = await self._generate_reply(channel, [prompt], trigger_type="morning")
        if reply: await channel.send(reply)

    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def nightly_reflection_task(self):
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        today_log = await self._fetch_todays_chat_log(channel)
        reply = await self._generate_reply(channel, ["(22時。今日の会話ログ全体を踏まえて1日を振り返る質問をして)"], trigger_type="nightly_reflection", extra_context=f"Log:\n{today_log}")
        if reply: await channel.send(reply)

    @tasks.loop(minutes=60)
    async def inactivity_check_task(self):
        if not self.channel_id: return
        now = datetime.datetime.now(JST)
        if (now - self.last_interaction) > timedelta(hours=12) and not (1 <= now.hour <= 6):
            channel = self.bot.get_channel(self.channel_id)
            if not channel: return
            async for m in channel.history(limit=1):
                if m.author.id == self.bot.user.id: return
            reply = await self._generate_reply(channel, ["(12時間連絡がない。軽く声かけて)"], trigger_type="inactivity")
            if reply: await channel.send(reply)
            self.last_interaction = now
            await self._save_data_to_drive()

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        
        # データ収集
        log_text = await self._fetch_todays_chat_log(channel)
        weather, max_t, min_t = await self._get_weather_stats()
        
        # Fitbit (全データ)
        fitbit_stats = await self._get_fitbit_stats(datetime.datetime.now(JST).strftime('%Y-%m-%d'))
        
        prompt = f"""
        今日のログを整理。JSON形式。
        ルール: memosは省略せず全発言をカテゴリ分けしてリスト化。
        JSON: {{ "diary": "...", "memos": ["- [Cat] text..."], "links": ["Title - URL"] }}
        --- Log ---
        {log_text}
        """
        try:
            response = await self.gemini_client.aio.models.generate_content(model='gemini-2.5-pro', contents=prompt, config=types.GenerateContentConfig(response_mime_type='application/json'))
            result = json.loads(response.text)
            
            # フロントマター用データ統合
            result['meta'] = {
                'weather': weather,
                'temp_max': max_t,
                'temp_min': min_t,
                **fitbit_stats # 辞書を展開して統合
            }
            
            today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            await self._execute_organization(result, today_str)
            await channel.send("（日記にまとめたよ🌙）")
        except Exception as e: logging.error(f"Nightly Error: {e}")

    async def _execute_organization(self, data, date_str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        daily_folder = await self._find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: daily_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")
        f_id = await self._find_file(service, daily_folder, f"{date_str}.md")
        
        # フロントマター作成 (Fitbit項目を追加)
        meta = data.get('meta', {})
        frontmatter = "---\n"
        frontmatter += f"date: {date_str}\n"
        frontmatter += f"weather: {meta.get('weather', 'N/A')}\n"
        frontmatter += f"temp_max: {meta.get('temp_max', 'N/A')}\n"
        frontmatter += f"temp_min: {meta.get('temp_min', 'N/A')}\n"
        # Fitbit Metrics
        if 'steps' in meta: frontmatter += f"steps: {meta['steps']}\n"
        if 'calories' in meta: frontmatter += f"calories: {meta['calories']}\n"
        if 'distance' in meta: frontmatter += f"distance: {meta['distance']}\n"
        if 'floors' in meta: frontmatter += f"floors: {meta['floors']}\n"
        if 'resting_hr' in meta: frontmatter += f"resting_hr: {meta['resting_hr']}\n"
        if 'sleep_minutes' in meta: frontmatter += f"sleep_time: {meta['sleep_minutes']}\n"
        
        frontmatter += "---\n\n"
        
        current_body = f"# Daily Note {date_str}\n"
        if f_id:
            from googleapiclient.http import MediaIoBaseDownload
            req = service.files().get_media(fileId=f_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done: _, done = downloader.next_chunk()
            raw_content = fh.getvalue().decode('utf-8')
            if raw_content.startswith("---"):
                parts = raw_content.split("---", 2)
                if len(parts) >= 3: current_body = parts[2].strip()
                else: current_body = raw_content
            else: current_body = raw_content

        updates = []
        if data.get('diary'): updates.append(f"## 📝 Journal\n{data['diary']}")
        if data.get('memos'): updates.append("## 📌 Memos\n" + "\n".join(data['memos']))
        if data.get('links'): updates.append("## 🔗 Links\n" + "\n".join([f"- {l}" for l in data['links']]))

        new_content = frontmatter + current_body + "\n\n" + "\n\n".join(updates)
        
        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [daily_folder]}, media_body=media).execute())

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))