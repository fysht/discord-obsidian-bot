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

# 外部ライブラリ
try: from web_parser import parse_url_with_readability
except ImportError: parse_url_with_readability = None
try: from utils.obsidian_utils import update_section
except ImportError: def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HISTORY_FILE_NAME = "partner_chat_history.json"
REMINDER_FILE_NAME = "partner_reminders.json" # リマインダー永続化用
BOT_FOLDER = ".bot"
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar.readonly']

# JMA 天気
JMA_AREA_CODE = "330000" 
JMA_URL = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"

# Regex
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
        self.history = [] 
        self.reminders = [] # [{'time': isoformat, 'content': str, 'user_id': int}]
        self.last_interaction = datetime.datetime.now(JST)
        self.user_name = "あなた"
        self.current_task = None
        self.notified_event_ids = set()

        self.is_ready = False

    async def cog_load(self):
        await self._load_data_from_drive()
        self.inactivity_check_task.start()
        self.daily_organize_task.start()
        self.morning_greeting_task.start()
        self.calendar_check_task.start()
        self.reminder_check_task.start()
        self.is_ready = True

    async def cog_unload(self):
        self.inactivity_check_task.cancel()
        self.daily_organize_task.cancel()
        self.morning_greeting_task.cancel()
        self.calendar_check_task.cancel()
        self.reminder_check_task.cancel()
        await self.session.close()
        await self._save_data_to_drive()

    # --- Drive I/O Helpers ---
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
        """履歴とリマインダーをロード"""
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        b_folder = await self._find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return

        # Load History
        f_id = await self._find_file(service, b_folder, HISTORY_FILE_NAME)
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
            except: pass

        # Load Reminders
        r_id = await self._find_file(service, b_folder, REMINDER_FILE_NAME)
        if r_id:
            try:
                request = service.files().get_media(fileId=r_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                self.reminders = json.loads(fh.getvalue().decode('utf-8'))
            except: pass

    async def _save_data_to_drive(self):
        """履歴とリマインダーを保存"""
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        # Save History
        h_data = {'history': self.history[-100:], 'last_interaction': self.last_interaction.isoformat()}
        b_folder = await self._find_file(service, self.drive_folder_id, BOT_FOLDER)
        
        # Helper for update/create
        async def upload_json(fname, content):
            f_id = await self._find_file(service, b_folder, fname)
            media = MediaIoBaseUpload(io.BytesIO(json.dumps(content, ensure_ascii=False, indent=2).encode('utf-8')), mimetype='application/json')
            if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
            else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': fname, 'parents': [b_folder]}, media_body=media).execute())

        await upload_json(HISTORY_FILE_NAME, h_data)
        await upload_json(REMINDER_FILE_NAME, self.reminders)

    async def _upload_text(self, service, parent_id, name, content):
        loop = asyncio.get_running_loop()
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        await loop.run_in_executor(None, lambda: service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=media).execute())

    # --- Utilities ---
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
        """テキストからリマインダー時間を抽出して登録"""
        now = datetime.datetime.now(JST)
        target_time = None
        content = "時間だよ！"
        
        # XX分後
        m_match = REMINDER_REGEX_MIN.search(text)
        if m_match:
            mins = int(m_match.group(1))
            target_time = now + timedelta(minutes=mins)
            content = text.replace(m_match.group(0), "").strip() or "指定の時間だよ！"
        
        # XX時(XX分)
        t_match = REMINDER_REGEX_TIME.search(text)
        if t_match:
            hour = int(t_match.group(1))
            minute = int(t_match.group(2)) if t_match.group(2) else 0
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time < now: target_time += timedelta(days=1) # 過去なら明日
            content = text.replace(t_match.group(0), "").strip() or "指定の時間だよ！"

        if target_time:
            self.reminders.append({
                'time': target_time.isoformat(),
                'content': content,
                'user_id': user_id
            })
            return target_time.strftime('%H:%M')
        return None

    # --- Chat Generation ---
    async def _generate_reply(self, inputs: list, trigger_type="reply", extra_context=""):
        if not self.gemini_client: return None
        
        weather = await self._get_weather_info()
        now_str = datetime.datetime.now(JST).strftime('%H:%M')
        
        system_prompt = f"""
        あなたはユーザー（{self.user_name}）の親しいパートナー（20代女性）です。
        LINEでやり取りするような、**温かみのあるタメ口**で話してください。
        絵文字も適度に使って、感情豊かに接してください。

        **現在の状況:**
        - 時刻: {now_str}
        - 天気: {weather}
        - ユーザーの状態: {f'「{self.current_task["name"]}」中' if self.current_task else '特になし'}
        {extra_context}

        **絶対のルール:**
        1. **自然な会話:** そっけなくならないように。「へー」「すごいね！」「わかる！」など共感を入れる。
        2. **アドバイス禁止:** 「日記に書こう」「忘れないで」のような指導者っぽい発言はNG。
        3. **リマインダー:** もし今回のやり取りでリマインダーがセットされた場合（contextに記載あり）は、「わかった！〇〇時に教えるね👍」のように快諾して。
        4. **長さ:** 基本は1〜3文。長くなりすぎないように。

        **トリガー:** {trigger_type}
        """

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        
        for h in self.history[-10:]:
            role = "user" if h['role'] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h['text'])]))
        
        user_parts = []
        for inp in inputs:
            if isinstance(inp, str): user_parts.append(types.Part.from_text(text=inp))
            else: user_parts.append(inp)
        
        if user_parts: contents.append(types.Content(role="user", parts=user_parts))
        else: contents.append(types.Content(role="user", parts=[types.Part.from_text(text="(きっかけ)")]))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.0-flash',
                contents=contents
            )
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
        
        # 0. 途中経過の表示コマンド
        if text in ["まとめ", "途中経過", "整理して", "今の状態"]:
            await self._show_interim_summary(message)
            return

        input_parts = []
        extra_ctx = ""

        # 1. リマインダー登録チェック
        reminder_time = self._parse_reminder(text, message.author.id)
        if reminder_time:
            extra_ctx += f"\n【システム通知】ユーザーがリマインダーをセットしました（時間: {reminder_time}）。「了解！その時間に教えるね」といって安心させてください。"
            # リマインダー永続化
            await self._save_data_to_drive()

        # 2. URL解析
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

        # 3. タスク状態管理
        if any(w in text for w in ["開始", "やる", "読む", "作業"]):
            if not self.current_task: self.current_task = {'name': text, 'start': datetime.datetime.now(JST)}
        elif any(w in text for w in ["終了", "終わった", "完了"]):
            self.current_task = None

        # 4. 履歴保存 & 応答
        self.history.append({'role': 'user', 'text': text, 'timestamp': datetime.datetime.now(JST).isoformat()})
        self.last_interaction = datetime.datetime.now(JST)

        async with message.channel.typing():
            reply = await self._generate_reply(input_parts, trigger_type="reply", extra_context=extra_ctx)
            if reply:
                await message.channel.send(reply)
                self.history.append({'role': 'model', 'text': reply, 'timestamp': datetime.datetime.now(JST).isoformat()})
                await self._save_history_to_drive()

    # --- Interim Summary (途中経過) ---
    async def _show_interim_summary(self, message):
        if not self.history:
            await message.reply("まだ会話してないから、まとめるものがないよ！")
            return

        async with message.channel.typing():
            today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            todays_logs = [h for h in self.history if h['timestamp'].startswith(today_str)]
            
            if not todays_logs:
                await message.reply("今日はまだ何も話してないね！")
                return

            log_text = "\n".join([f"{'User' if l['role']=='user' else 'AI'}: {l['text']}" for l in todays_logs])
            
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
                    model='gemini-2.0-flash',
                    contents=prompt
                )
                await message.reply(f"今のところ、こんな感じでまとまってるよ！👇\n\n{response.text}")
            except Exception as e:
                await message.reply(f"ごめん、うまくまとめられなかった💦 ({e})")

    # --- Scheduled Tasks ---

    @tasks.loop(minutes=1)
    async def reminder_check_task(self):
        """リマインダーの確認"""
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
                    # メッセージを工夫
                    content = rem.get('content', '時間だよ！').replace("教えて", "").replace("声かけて", "")
                    await channel.send(f"{mention} ⏰ **{content}** ({target.strftime('%H:%M')})")
                    changed = True
            else:
                remaining.append(rem)
        
        self.reminders = remaining
        if changed: await self._save_data_to_drive()

    @tasks.loop(minutes=5)
    async def calendar_check_task(self):
        """カレンダー通知"""
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
                # 10分前通知
                if 540 <= (start - now).total_seconds() <= 660:
                    eid = event['id']
                    if eid in self.notified_event_ids: continue
                    self.notified_event_ids.add(eid)
                    
                    channel = self.bot.get_channel(self.channel_id)
                    if channel:
                        msg = f"ねえ、あと10分で「{event['summary']}」だよ！準備OK？"
                        await channel.send(msg)
                        self.history.append({'role': 'model', 'text': msg, 'timestamp': now.isoformat()})
        except: pass

    @tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=JST))
    async def morning_greeting_task(self):
        """朝の挨拶"""
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        
        reply = await self._generate_reply(["(朝だよ。天気と予定を教えて、明るく起こして)"], trigger_type="morning")
        if reply:
            await channel.send(reply)
            self.history.append({'role': 'model', 'text': reply, 'timestamp': datetime.datetime.now(JST).isoformat()})

    @tasks.loop(minutes=60)
    async def inactivity_check_task(self):
        if not self.channel_id: return
        now = datetime.datetime.now(JST)
        if (now - self.last_interaction) > timedelta(hours=12) and not (1 <= now.hour <= 6):
            if self.history and self.history[-1]['role'] == 'model': return
            channel = self.bot.get_channel(self.channel_id)
            if not channel: return
            
            reply = await self._generate_reply(["(12時間連絡がないね。何かあった？軽く声かけて)"], trigger_type="inactivity")
            if reply:
                await channel.send(reply)
                self.history.append({'role': 'model', 'text': reply, 'timestamp': now.isoformat()})
                self.last_interaction = now

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        """夜のまとめ"""
        if not self.history: return
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        todays_logs = [h for h in self.history if h['timestamp'].startswith(today_str)]
        if not todays_logs: return

        log_text = "\n".join([f"{'User' if l['role']=='user' else 'AI'}: {l['text']}" for l in todays_logs])
        logging.info("Starting nightly organization...")
        
        prompt = f"""
        今日の会話ログを分析し、JSON形式で整理してください。
        
        1. `diary`: 今日の出来事や感情を「である調」で日記にする（300字）。
        2. `webclips`: URL情報のまとめ。
        3. `youtube`: 動画のまとめ。
        4. `recipes`: レシピまとめ。
        5. `memos`: その他メモ。

        JSON:
        {{ "diary": "...", "webclips": [], "youtube": [], "recipes": [], "memos": [] }}
        
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
            if channel: await channel.send("（今日の分、日記にまとめておいたよ！おやすみ🌙）")

        except Exception as e:
            logging.error(f"Nightly Task Error: {e}")

    async def _execute_organization(self, data, date_str):
        # ... (保存ロジックは前回と同じため省略なしで実装) ...
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        # WebClips
        if data.get('webclips'):
            folder_id = await self._find_file(service, self.drive_folder_id, "WebClips")
            if not folder_id: folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "WebClips")
            for item in data['webclips']:
                t = item.get('title','Clip'); safe_t = re.sub(r'[\\/*?:"<>|]', "", t)[:30]
                await self._upload_text(service, folder_id, f"{date_str}-{safe_t}.md", f"# {t}\nURL: {item.get('url')}\n\n## Note\n{item.get('note','')}")

        # YouTube, Recipesも同様（省略せず実装）
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

        # Daily Note
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