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

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# 外部ライブラリ (Web解析用)
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
HISTORY_FILE_NAME = "partner_chat_history.json"
BOT_FOLDER = ".bot"
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar.readonly']

# JMA 天気 (東京:130000, 大阪:270000, 岡山:330000 など地域に合わせて変更可)
JMA_AREA_CODE = "330000" 
JMA_URL = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"

# URL検出
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:[/?][\w\-.?=&%@+]*)?')
YOUTUBE_REGEX = re.compile(r'(youtube\.com|youtu\.be)')

class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("MEMO_CHANNEL_ID") or os.getenv("PARTNER_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

        # Gemini Client
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None
            logging.warning("PartnerCog: GEMINI_API_KEY not set.")

        self.session = aiohttp.ClientSession()
        
        # State
        self.history = [] 
        self.last_interaction = datetime.datetime.now(JST)
        self.user_name = "あなた"
        self.current_task = None # {'name': '読書', 'start': datetime}
        self.notified_event_ids = set()

        self.is_ready = False

    async def cog_load(self):
        await self._load_history_from_drive()
        self.inactivity_check_task.start()
        self.daily_organize_task.start()
        self.morning_greeting_task.start()
        self.calendar_check_task.start()
        self.is_ready = True

    async def cog_unload(self):
        self.inactivity_check_task.cancel()
        self.daily_organize_task.cancel()
        self.morning_greeting_task.cancel()
        self.calendar_check_task.cancel()
        await self.session.close()
        await self._save_history_to_drive()

    # --- Google API Helpers ---
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

    def _find_file(self, service, parent_id, name):
        try:
            res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
            files = res.get('files', [])
            return files[0]['id'] if files else None
        except: return None

    def _create_folder(self, service, parent_id, name):
        f = service.files().create(body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id').execute()
        return f.get('id')

    def _upload_text(self, service, parent_id, name, content):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=media).execute()

    # --- History I/O ---
    async def _load_history_from_drive(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return

        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, HISTORY_FILE_NAME)
        if f_id:
            try:
                request = service.files().get_media(fileId=f_id)
                fh = io.BytesIO()
                from googleapiclient.http import MediaIoBaseDownload
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                data = json.loads(fh.getvalue().decode('utf-8'))
                self.history = data.get('history', [])
                ts = data.get('last_interaction')
                if ts: self.last_interaction = datetime.datetime.fromisoformat(ts)
            except Exception as e: logging.error(f"History load error: {e}")

    async def _save_history_to_drive(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        data = {'history': self.history[-100:], 'last_interaction': self.last_interaction.isoformat()}
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, HISTORY_FILE_NAME)
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')), mimetype='application/json')
        
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': HISTORY_FILE_NAME, 'parents': [b_folder]}, media_body=media).execute())

    # --- Information Gathering Helpers ---
    async def _get_weather_info(self):
        try:
            async with self.session.get(JMA_URL) as resp:
                if resp.status != 200: return "天気情報取得失敗"
                data = await resp.json()
                weather = data[0]["timeSeries"][0]["areas"][0]["weathers"][0]
                temps = data[0]["timeSeries"][2]["areas"][0].get("temps", [])
                temp_str = f"最高{temps[1]}℃" if len(temps) > 1 else ""
                return f"{weather} {temp_str}".strip()
        except: return "天気不明"

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

    async def _get_calendar_events(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_calendar_service)
        if not service: return []
        now = datetime.datetime.now(JST)
        try:
            events_res = await loop.run_in_executor(None, lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=now.replace(hour=0,minute=0).isoformat(),
                timeMax=now.replace(hour=23,minute=59).isoformat(), singleEvents=True, orderBy='startTime').execute())
            return events_res.get('items', [])
        except: return []

    # --- Chat Generation Core ---
    async def _generate_reply(self, inputs: list, trigger_type="reply"):
        if not self.gemini_client: return None
        
        # コンテキスト情報の収集
        weather = await self._get_weather_info()
        events = await self._get_calendar_events()
        schedule_text = "なし"
        if events:
            schedule_text = "\n".join([f"- {e.get('summary')} ({e['start'].get('dateTime','終日')[11:16]})" for e in events])
        
        task_status = "特になし"
        if self.current_task:
            elapsed = int((datetime.datetime.now(JST) - self.current_task['start']).total_seconds() / 60)
            task_status = f"現在「{self.current_task['name']}」を実行中（経過: {elapsed}分）"

        system_prompt = f"""
        あなたはユーザー（{self.user_name}）の親しいパートナー（20代女性）です。
        LINEで会話するように、**タメ口**で、**1〜2文の短い文章**で返信してください。

        **現在の状況:**
        - 時刻: {datetime.datetime.now(JST).strftime('%H:%M')}
        - 天気: {weather}
        - 今日の予定: {schedule_text}
        - ユーザーの状態: {task_status}

        **絶対のルール:**
        1. **アドバイス禁止:** 指導やメタな発言（「日記に書こう」等）はしない。
        2. **自然な会話:** 共感、リアクション、軽い質問を中心に。
        3. **マルチモーダル対応:** 画像や音声が送られた場合は、その内容（「写真見たよ！美味しそう」「声聞いたよ」等）に必ず触れる。
        4. **タスク管理:** ユーザーが「〜する」「〜始める」と言ったら「いってらっしゃい」、「終わった」と言ったら「お疲れ様」と声をかける（Bot内部で時間は記録しているため、時間を聞く必要はない）。

        **トリガー:** {trigger_type}
        """

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        
        # 履歴追加 (Text only for history context to save tokens/complexity)
        for h in self.history[-10:]:
            role = "user" if h['role'] == "user" else "model"
            # 履歴内のテキストのみ使用
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h['text'])]))
        
        # 今回の入力 (Text + Media parts)
        user_content_parts = []
        for inp in inputs:
            if isinstance(inp, str): user_content_parts.append(types.Part.from_text(text=inp))
            else: user_content_parts.append(inp) # Image/Audio Part
        
        if user_content_parts:
            contents.append(types.Content(role="user", parts=user_content_parts))
        else:
            # 自発的発言用
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text="(きっかけの言葉)")]))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.0-flash',
                contents=contents
            )
            return response.text
        except Exception as e:
            logging.error(f"GenAI Error: {e}")
            return None

    # --- Event Listener (Main Interface) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.channel_id: return

        self.user_name = message.author.display_name
        text_content = message.content.strip()
        input_parts = []
        
        # 1. URL解析
        url_match = URL_REGEX.search(text_content)
        if url_match:
            async with message.channel.typing():
                url_info = await self._analyze_url_content(url_match.group(0))
                text_content += f"\n(URL情報: {url_info['title']} - {url_info['content']})"

        if text_content:
            input_parts.append(text_content)

        # 2. 添付ファイル処理 (画像・音声)
        for attachment in message.attachments:
            # 画像
            if any(attachment.content_type.startswith(t) for t in ['image/', 'application/pdf']):
                try:
                    img_data = await attachment.read()
                    input_parts.append(types.Part.from_bytes(data=img_data, mime_type=attachment.content_type))
                    text_content += " [画像送信]"
                except: pass
            # 音声
            elif any(attachment.content_type.startswith(t) for t in ['audio/']):
                try:
                    audio_data = await attachment.read()
                    input_parts.append(types.Part.from_bytes(data=audio_data, mime_type=attachment.content_type))
                    text_content += " [音声送信]"
                except: pass

        if not input_parts: return

        # 3. タスク状態の簡易管理
        if "開始" in text_content or "やる" in text_content or "読む" in text_content:
            if not self.current_task:
                self.current_task = {'name': text_content, 'start': datetime.datetime.now(JST)}
        elif "終了" in text_content or "終わった" in text_content:
            self.current_task = None

        # 4. 履歴保存 & 応答生成
        self.history.append({'role': 'user', 'text': text_content, 'timestamp': datetime.datetime.now(JST).isoformat()})
        self.last_interaction = datetime.datetime.now(JST)

        async with message.channel.typing():
            reply = await self._generate_reply(input_parts, trigger_type="reply")
            if reply:
                await message.channel.send(reply)
                self.history.append({'role': 'model', 'text': reply, 'timestamp': datetime.datetime.now(JST).isoformat()})
                await self._save_history_to_drive()

    # --- Scheduled Tasks ---
    
    @tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=JST))
    async def morning_greeting_task(self):
        """朝の挨拶"""
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        
        reply = await self._generate_reply(["(朝6時になりました。起きたユーザーに、天気と予定を伝えて爽やかに挨拶して)"], trigger_type="morning")
        if reply:
            await channel.send(reply)
            self.history.append({'role': 'model', 'text': reply, 'timestamp': datetime.datetime.now(JST).isoformat()})

    @tasks.loop(minutes=5)
    async def calendar_check_task(self):
        """カレンダーの直前の予定を通知"""
        if not self.channel_id: return
        events = await self._get_calendar_events()
        now = datetime.datetime.now(JST)
        
        for event in events:
            if 'dateTime' not in event.get('start', {}): continue
            start_dt = datetime.datetime.fromisoformat(event['start']['dateTime'])
            # 10分前〜5分前なら通知
            if 300 <= (start_dt - now).total_seconds() <= 600:
                eid = event['id']
                if eid in self.notified_event_ids: continue
                
                self.notified_event_ids.add(eid)
                channel = self.bot.get_channel(self.channel_id)
                if channel:
                    msg = f"ねえ、あと少しで「{event['summary']}」の時間だよ！({start_dt.strftime('%H:%M')})"
                    await channel.send(msg)
                    self.history.append({'role': 'model', 'text': msg, 'timestamp': now.isoformat()})

    @tasks.loop(minutes=60)
    async def inactivity_check_task(self):
        """長時間発言がない場合"""
        if not self.channel_id: return
        now = datetime.datetime.now(JST)
        # 12時間経過 & 夜中以外
        if (now - self.last_interaction) > timedelta(hours=12) and not (1 <= now.hour <= 6):
            if self.history and self.history[-1]['role'] == 'model': return # 連投防止
            
            channel = self.bot.get_channel(self.channel_id)
            if not channel: return
            
            reply = await self._generate_reply(["(12時間以上会話がない。気遣う言葉をかけて)"], trigger_type="inactivity")
            if reply:
                await channel.send(reply)
                self.history.append({'role': 'model', 'text': reply, 'timestamp': now.isoformat()})
                self.last_interaction = now

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        """1日の終わりに情報を整理して保存"""
        if not self.history: return
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        todays_logs = [h for h in self.history if h['timestamp'].startswith(today_str)]
        if not todays_logs: return

        log_text = "\n".join([f"{'User' if l['role']=='user' else 'AI'}: {l['text']}" for l in todays_logs])
        logging.info("Starting nightly organization...")
        
        prompt = f"""
        以下は今日1日のチャットログです。これを分析し、5つのカテゴリに分類・整理してJSONで出力してください。
        
        1. `diary`: 今日の出来事や感情をまとめた「である調」の日記（300字程度）。
        2. `webclips`: URL付き投稿のまとめ。
        3. `youtube`: YouTube動画のまとめ。
        4. `recipes`: レシピのまとめ。
        5. `memos`: その他のメモやタスク、手書きメモや音声メモの内容。

        **JSONフォーマット:**
        {{
          "diary": "...",
          "webclips": [{{"title": "...", "url": "...", "note": "..."}}],
          "youtube": [{{"title": "...", "url": "...", "note": "..."}}],
          "recipes": [{{"name": "...", "url": "...", "note": "..."}}],
          "memos": ["..."]
        }}
        
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
            await self._execute_organization(result, today_str)
            
            channel = self.bot.get_channel(self.channel_id)
            if channel: await channel.send("（今日の思い出を日記にまとめておいたよ。おやすみ🌙）")

        except Exception as e:
            logging.error(f"Nightly Task Error: {e}")

    async def _execute_organization(self, data, date_str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        # 1. 個別ファイル保存 (WebClip, YouTube, Recipe)
        # (必要に応じて各フォルダにMD作成)
        
        # 2. Daily Note更新
        daily_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        f_id = await loop.run_in_executor(None, self._find_file, service, daily_folder, f"{date_str}.md")
        
        current_content = f"# Daily Note {date_str}\n"
        if f_id:
            from googleapiclient.http import MediaIoBaseDownload
            req = service.files().get_media(fileId=f_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done: _, done = downloader.next_chunk()
            current_content = fh.getvalue().decode('utf-8')

        updates = []
        if data.get('diary'): updates.append(f"## 📝 Journal\n{data['diary']}")
        
        links = []
        for cat in ['webclips', 'youtube', 'recipes']:
            for item in data.get(cat, []):
                title = item.get('title') or item.get('name')
                note = item.get('note', '')
                links.append(f"- [{title}]({item.get('url')}) : {note}")
        
        if links: updates.append("## 🔗 Links\n" + "\n".join(links))
        if data.get('memos'): updates.append("## 📌 Memos\n" + "\n".join([f"- {m}" for m in data['memos']]))

        new_content = current_content + "\n\n" + "\n\n".join(updates)
        
        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [daily_folder]}, media_body=media).execute())

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))