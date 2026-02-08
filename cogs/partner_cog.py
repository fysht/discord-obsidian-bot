import os
import json
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from discord import app_commands
from google import genai
from google.genai import types
import datetime
from datetime import timedelta
import zoneinfo
import re
import aiohttp

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

# 外部ライブラリ (Web解析用)
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
HISTORY_FILE_NAME = "partner_chat_history.json"
BOT_FOLDER = ".bot"
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar.readonly']

# URL検出用
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:[/?][\w\-.?=&%@+]*)?')
YOUTUBE_REGEX = re.compile(r'(youtube\.com|youtu\.be)')

class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 統合チャンネルID (MEMO_CHANNEL_ID または PARTNER_CHANNEL_ID を使用)
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
        
        # 会話履歴 (メモリ上に保持し、ファイルにもバックアップ)
        self.history = [] 
        self.last_interaction = datetime.datetime.now(JST)
        self.user_name = "あなた"

        self.is_ready = False

    async def cog_load(self):
        await self._load_history_from_drive()
        self.inactivity_check_task.start()
        self.daily_organize_task.start()
        self.is_ready = True

    async def cog_unload(self):
        self.inactivity_check_task.cancel()
        self.daily_organize_task.cancel()
        await self.session.close()
        await self._save_history_to_drive()

    # --- Drive / Google API ---
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
        # 簡易実装: Driveと同じ認証情報を使用
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

        data = {'history': self.history[-100:], 'last_interaction': self.last_interaction.isoformat()} # 直近100件保持
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        # フォルダ作成省略(ある前提)
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, HISTORY_FILE_NAME)
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')), mimetype='application/json')
        
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': HISTORY_FILE_NAME, 'parents': [b_folder]}, media_body=media).execute())

    # --- URL Parsing ---
    async def _analyze_url_content(self, url):
        """URLの中身を簡易解析してAIへのヒントにする"""
        info = {"type": "unknown", "title": "URL", "content": ""}
        
        if YOUTUBE_REGEX.search(url):
            info["type"] = "youtube"
            # oEmbedでタイトル取得
            try:
                oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
                async with self.session.get(oembed_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        info["title"] = data.get("title", "YouTube Video")
                        info["content"] = f"Channel: {data.get('author_name')}"
            except: pass
        
        elif parse_url_with_readability:
            # 一般Webサイト
            try:
                title, content = await asyncio.to_thread(parse_url_with_readability, url)
                info["type"] = "web"
                info["title"] = title
                info["content"] = content[:500] + "..." # 長すぎるので冒頭のみ
            except: pass
            
        return info

    async def _get_calendar_context(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_calendar_service)
        if not service: return ""
        
        now = datetime.datetime.now(JST)
        try:
            events_res = await loop.run_in_executor(None, lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=now.replace(hour=0,minute=0).isoformat(),
                timeMax=now.replace(hour=23,minute=59).isoformat(), singleEvents=True, orderBy='startTime').execute())
            events = events_res.get('items', [])
            if not events: return "今日の予定: なし"
            return "今日の予定:\n" + "\n".join([f"- {e.get('summary')}" for e in events])
        except: return ""

    # --- Chat Generation ---
    async def _generate_reply(self, user_input, url_info=None):
        if not self.gemini_client: return None
        
        calendar_ctx = await self._get_calendar_context()
        url_ctx = ""
        if url_info:
            url_ctx = f"\n【ユーザーが送信したURL情報】\n種類: {url_info['type']}\nタイトル: {url_info['title']}\n内容抜粋: {url_info['content']}\nこのリンクについて話題を振ってください。"

        system_prompt = f"""
        あなたはユーザー（{self.user_name}）の親しいパートナー（20代女性、フレンドリーなタメ口）です。
        ユーザーの発言やURL投稿に対し、会話を盛り上げてください。
        
        **重要な役割:**
        ユーザーの投稿（テキスト、URL）は後で「日記」や「データベース」に整理されます。
        そのため、保存する価値のある詳細情報（感想、目的、評価など）を会話の中で自然に引き出してください。
        例: URLが貼られたら「これ何の記事？」「面白かった？」と聞く。
        
        **コンテキスト:**
        {calendar_ctx}
        {url_ctx}
        """

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        
        # 履歴追加
        for h in self.history[-15:]:
            role = "user" if h['role'] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h['text'])]))
        
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_input)]))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.0-flash',
                contents=contents
            )
            return response.text
        except Exception as e:
            logging.error(f"GenAI Error: {e}")
            return None

    # --- Main Event Listener ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.channel_id: return

        self.user_name = message.author.display_name
        content = message.content.strip()
        
        # URLチェック
        url_match = URL_REGEX.search(content)
        url_info = None
        if url_match:
            url = url_match.group(0)
            async with message.channel.typing():
                url_info = await self._analyze_url_content(url)
        
        # 履歴追加
        self.history.append({'role': 'user', 'text': content, 'timestamp': datetime.datetime.now(JST).isoformat()})
        self.last_interaction = datetime.datetime.now(JST)

        # 返信生成
        async with message.channel.typing():
            reply = await self._generate_reply(content, url_info)
            if reply:
                await message.channel.send(reply)
                self.history.append({'role': 'model', 'text': reply, 'timestamp': datetime.datetime.now(JST).isoformat()})
                await self._save_history_to_drive()

    @tasks.loop(hours=12) 
    async def inactivity_check_task(self):
        # 12時間以上会話がない場合に話しかける（ロジックは前述同様、省略または適宜調整）
        pass

    # --- Nightly Organization Task ---
    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        if not self.history: return
        
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        # 今日のログを抽出
        todays_logs = [h for h in self.history if h['timestamp'].startswith(today_str)]
        if not todays_logs: return

        # ログを文字列化
        log_text = "\n".join([f"{'User' if l['role']=='user' else 'AI'}: {l['text']}" for l in todays_logs])

        logging.info("Starting nightly organization...")
        
        # Geminiに構造化データを要求
        prompt = f"""
        以下は今日1日のユーザーとのチャットログです。
        この内容を分析し、以下の5つのカテゴリに分類・整理してJSON形式で出力してください。
        
        **カテゴリ:**
        1. `diary`: 今日の出来事、感情、考えをまとめた「である調」の日記（300字程度）。
        2. `webclips`: URLが含まれる投稿と、それに対するユーザーの感想やコメント。
        3. `youtube`: YouTubeのURLと、感想やメモ。
        4. `recipes`: レシピのURLや料理の話題。
        5. `memos`: 上記に当てはまらない一般的なメモやタスク。

        **JSONフォーマット:**
        ```json
        {{
          "diary": "今日は...",
          "webclips": [
            {{"url": "...", "title": "...", "summary": "ユーザーのコメントなど"}}
          ],
          "youtube": [
            {{"url": "...", "title": "...", "note": "..."}}
          ],
          "recipes": [
            {{"url": "...", "name": "...", "note": "..."}}
          ],
          "memos": [
            "メモ内容1", "メモ内容2"
          ]
        }}
        ```
        
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
            if channel: await channel.send("（今日の会話を整理してノートにまとめておいたよ！おやすみ🌙）")

        except Exception as e:
            logging.error(f"Nightly Task Error: {e}")

    async def _execute_organization(self, data, date_str):
        """AIの分析結果に基づいて実際にファイルを作成・更新する"""
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        # 1. 各フォルダへの保存
        # WebClips
        if data.get('webclips'):
            folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "WebClips")
            # なければ作る（省略、ある前提）
            for item in data['webclips']:
                title = item.get('title', 'WebClip')
                safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:30]
                filename = f"{date_str}-{safe_title}.md"
                content = f"# {title}\nURL: {item.get('url')}\n\n## Note\n{item.get('summary')}"
                await loop.run_in_executor(None, self._upload_text, service, folder_id, filename, content)

        # YouTube
        if data.get('youtube'):
            folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "YouTube")
            for item in data['youtube']:
                title = item.get('title', 'Video')
                safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:30]
                filename = f"{date_str}-{safe_title}.md"
                content = f"# {title}\nURL: {item.get('url')}\n\n## Memo\n{item.get('note')}"
                await loop.run_in_executor(None, self._upload_text, service, folder_id, filename, content)

        # Recipes
        if data.get('recipes'):
            folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "Recipes")
            for item in data['recipes']:
                title = item.get('name', 'Recipe')
                safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:30]
                filename = f"{date_str}-{safe_title}.md"
                content = f"# {title}\nURL: {item.get('url')}\n\n## Cooking Note\n{item.get('note')}"
                await loop.run_in_executor(None, self._upload_text, service, folder_id, filename, content)

        # 2. Daily Noteの更新 (Diary & Memos & Links)
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

        # 更新内容の構築
        updates = []
        if data.get('diary'):
            updates.append(f"## 📝 Journal\n{data['diary']}")
        if data.get('memos'):
            updates.append("## 📌 Memos\n" + "\n".join([f"- {m}" for m in data['memos']]))
        
        # リンク追記 (簡易実装: 各項目のタイトルを列挙)
        collected_links = []
        for cat in ['webclips', 'youtube', 'recipes']:
            for item in data.get(cat, []):
                title = item.get('title') or item.get('name')
                if title: collected_links.append(f"- [[{cat}/{date_str}-{re.sub(r'[\\/*?:\'<>|]', '', title)[:30]}|{title}]]")
        
        if collected_links:
            updates.append("## 🔗 Links\n" + "\n".join(collected_links))

        # 追記実行
        new_content = current_content + "\n\n" + "\n\n".join(updates)
        
        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        if f_id:
            await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else:
            await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [daily_folder]}, media_body=media).execute())

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))