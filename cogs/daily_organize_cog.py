import os
import logging
import datetime
import json
import re

import discord
from discord.ext import commands, tasks
from google.genai import types

from config import JST
from utils.obsidian_utils import update_section, update_frontmatter
from prompts import PROMPT_DAILY_ORGANIZE
from info_service import InfoService

class DailyOrganizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client
        self.tasks_service = getattr(bot, 'tasks_service', None)
        self.info_service = getattr(bot, 'info_service', InfoService())

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.daily_organize_task.is_running(): 
            self.daily_organize_task.start()

    def cog_unload(self):
        self.daily_organize_task.cancel()

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        channel = self.bot.get_channel(self.memo_channel_id)
        partner_cog = self.bot.get_cog("PartnerCog")
        if not channel or not partner_cog: return

        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        
        current_tasks_text = "タスクAPIに接続されていません。"
        if self.tasks_service:
            current_tasks_text = await self.tasks_service.get_uncompleted_tasks()

        log_text = await partner_cog.fetch_todays_chat_log(channel)
        
        # InfoServiceを使って天気を取得
        weather, max_t, min_t = await self.info_service.get_weather()

        location_log_text = "（記録なし）"
        service = self.drive_service.get_service()
        if service:
            daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
            if daily_folder:
                daily_file = await self.drive_service.find_file(service, daily_folder, f"{today_str}.md")
                if daily_file:
                    try:
                        raw_content = await self.drive_service.read_text_file(service, daily_file)
                        match = re.search(r'## 📍 Location History\n(.*?)(?=\n## |\Z)', raw_content, re.DOTALL)
                        if match and match.group(1).strip():
                            location_log_text = match.group(1).strip()
                    except Exception as e: logging.error(f"DailyOrganize: Location read error: {e}")

        result = {"journal": "", "events": [], "insights": [], "next_actions": [], "message": "（今日の会話とデータをノートにまとめたよ🌙 おやすみ！）"}
        
        if log_text.strip():
            prompt = f"{PROMPT_DAILY_ORGANIZE}\n【現在の未完了タスク】\n{current_tasks_text}\n\n【今日の移動記録】\n{location_log_text}\n\n--- Chat Log ---\n{log_text}"
            try:
                if self.gemini_client:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro",
                        contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json")
                    )
                    res_data = json.loads(response.text)
                    result.update(res_data)
            except Exception as e: logging.error(f"DailyOrganize: JSON Error: {e}")

        result['meta'] = {'weather': weather, 'temp_max': max_t, 'temp_min': min_t}
        await self._execute_organization(result, today_str)
        
        if result.get('next_actions') and self.tasks_service:
            clean_actions = [re.sub(r'^-\s*', '', act).strip() for act in result['next_actions']]
            for act in clean_actions:
                if act:
                    try: await self.tasks_service.add_task(title=act)
                    except Exception as e: logging.error(f"Google Tasks自動登録エラー: {e}")

        send_msg = result.get('message', '（今日の会話とデータをノートにまとめたよ🌙 今日も一日お疲れ様、おやすみ！）')
        await channel.send(send_msg)

    async def _execute_organization(self, data, date_str):
        service = self.drive_service.get_service()
        if not service: return

        daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: daily_folder = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")
            
        f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
        
        content = f"# Daily Note {date_str}\n"
        if f_id:
            try:
                raw_content = await self.drive_service.read_text_file(service, f_id)
                if raw_content: content = raw_content
            except: pass

        meta = data.get('meta', {})
        updates_fm = {'date': date_str}
        if meta.get('weather') != 'N/A': updates_fm['weather'] = meta.get('weather')
        if meta.get('temp_max') != 'N/A': updates_fm['temp_max'] = meta.get('temp_max')
        if meta.get('temp_min') != 'N/A': updates_fm['temp_min'] = meta.get('temp_min')
        content = update_frontmatter(content, updates_fm)

        if data.get('journal'): content = update_section(content, data['journal'], "## 📔 Daily Journal")
        if data.get('events') and len(data['events']) > 0: content = update_section(content, "\n".join(data['events']) if isinstance(data['events'], list) else str(data['events']), "## 📝 Events & Actions")
        if data.get('insights') and len(data['insights']) > 0: content = update_section(content, "\n".join(data['insights']) if isinstance(data['insights'], list) else str(data['insights']), "## 💡 Insights & Thoughts")
        if data.get('next_actions') and len(data['next_actions']) > 0: content = update_section(content, "\n".join(data['next_actions']) if isinstance(data['next_actions'], list) else str(data['next_actions']), "## ➡️ Next Actions")
        
        if f_id: await self.drive_service.update_text(service, f_id, content)
        else: await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", content)

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyOrganizeCog(bot))