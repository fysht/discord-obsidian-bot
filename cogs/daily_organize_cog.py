import os
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types
import logging
import datetime
import zoneinfo
import json
import io
import aiohttp
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

class DailyOrganizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.daily_organize_task.is_running(): self.daily_organize_task.start()

    def cog_unload(self):
        self.daily_organize_task.cancel()

    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.valid: return build('drive', 'v3', credentials=creds)
        elif creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                open(TOKEN_FILE, 'w').write(creds.to_json())
                return build('drive', 'v3', credentials=creds)
            except: pass
        return None

    async def _find_file(self, service, parent_id, name):
        import asyncio
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute())
            files = res.get('files', [])
            return files[0]['id'] if files else None
        except: return None

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        channel = self.bot.get_channel(self.memo_channel_id)
        partner_cog = self.bot.get_cog("PartnerCog")
        if not channel or not partner_cog: return

        log_text = await partner_cog.fetch_todays_chat_log(channel)
        weather, max_t, min_t = "N/A", "N/A", "N/A"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://www.jma.go.jp/bosai/forecast/data/forecast/330000.json") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        weather = data[0]["timeSeries"][0]["areas"][0]["weathers"][0].replace("\u3000", " ")
                        temps = data[0]["timeSeries"][2]["areas"][0].get("temps", [])
                        valid_temps = [float(t) for t in temps if t and t != "--"]
                        if valid_temps:
                            max_t, min_t = int(max(valid_temps)), int(min(valid_temps))
        except: pass

        fitbit_stats = {}
        fitbit_cog = self.bot.get_cog("FitbitCog")
        if fitbit_cog and hasattr(fitbit_cog, 'fitbit_client'):
            client = fitbit_cog.fitbit_client
            target_date = datetime.datetime.now(JST).date()
            try:
                sleep_data = await client.get_sleep_data(target_date)
                if sleep_data and 'summary' in sleep_data: fitbit_stats['sleep_minutes'] = sleep_data['summary'].get('totalMinutesAsleep', 0)
                act_data = await client.get_activity_summary(target_date)
                if act_data and 'summary' in act_data:
                    s = act_data['summary']
                    fitbit_stats['steps'] = s.get('steps', 0)
                    fitbit_stats['calories'] = s.get('caloriesOut', 0)
                    distances = s.get('distances', [])
                    fitbit_stats['distance'] = next((d['distance'] for d in distances if d['activity'] == 'total'), 0)
                    fitbit_stats['floors'] = s.get('floors', 0)
                    fitbit_stats['resting_hr'] = s.get('restingHeartRate', 'N/A')
            except: pass

        result = {"diary": "", "memos": [], "links": []}
        if log_text.strip():
            prompt = f"今日の会話ログを整理し、JSON形式で出力。\nルール: memosは省略せず全発言をカテゴリ分けしてリスト化。\nフォーマット: {{ \"diary\": \"...\", \"memos\": [\"- [カテゴリ] 内容...\"], \"links\": [\"タイトル - URL\"] }}\n--- Chat Log ---\n{log_text}"
            try:
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                result = json.loads(response.text)
            except Exception as e: logging.error(f"DailyOrganize: JSON Error: {e}")

        result['meta'] = {'weather': weather, 'temp_max': max_t, 'temp_min': min_t, **fitbit_stats}
        await self._execute_organization(result, datetime.datetime.now(JST).strftime('%Y-%m-%d'))
        await channel.send("（今日の会話とデータをノートにまとめたよ🌙 おやすみ！）")

    async def _execute_organization(self, data, date_str):
        import asyncio
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        daily_folder = await self._find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: 
            meta = {'name': "DailyNotes", 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}
            folder_obj = await loop.run_in_executor(None, lambda: service.files().create(body=meta, fields='id').execute())
            daily_folder = folder_obj.get('id')
            
        f_id = await self._find_file(service, daily_folder, f"{date_str}.md")
        meta = data.get('meta', {})
        frontmatter = "---\n" + f"date: {date_str}\n" + f"weather: {meta.get('weather', 'N/A')}\n" + f"temp_max: {meta.get('temp_max', 'N/A')}\n" + f"temp_min: {meta.get('temp_min', 'N/A')}\n"
        if 'steps' in meta: frontmatter += f"steps: {meta['steps']}\n"
        if 'calories' in meta: frontmatter += f"calories: {meta['calories']}\n"
        if 'distance' in meta: frontmatter += f"distance: {meta['distance']}\n"
        if 'floors' in meta: frontmatter += f"floors: {meta['floors']}\n"
        if 'resting_hr' in meta: frontmatter += f"resting_hr: {meta['resting_hr']}\n"
        if 'sleep_minutes' in meta: frontmatter += f"sleep_time: {meta['sleep_minutes']}\n"
        frontmatter += "---\n\n"
        
        current_body = f"# Daily Note {date_str}\n"
        if f_id:
            try:
                request = service.files().get_media(fileId=f_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                raw_content = fh.getvalue().decode('utf-8')
                if raw_content.startswith("---"):
                    parts = raw_content.split("---", 2)
                    if len(parts) >= 3: current_body = parts[2].strip()
                    else: current_body = raw_content
                else: current_body = raw_content
            except: pass

        updates = []
        if data.get('diary'): updates.append(f"## 📝 Journal\n{data['diary']}")
        if data.get('memos') and len(data['memos']) > 0: updates.append("## 📌 Memos\n" + ("\n".join(data['memos']) if isinstance(data['memos'], list) else str(data['memos'])))
        if data.get('links') and len(data['links']) > 0: updates.append("## 🔗 Links\n" + ("\n".join([f"- {l}" for l in data['links']]) if isinstance(data['links'], list) else str(data['links'])))

        new_content = frontmatter + current_body + "\n\n" + "\n\n".join(updates)
        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [daily_folder]}, media_body=media).execute())

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyOrganizeCog(bot))