import os
import logging
import datetime
import json
import aiohttp
import re

import discord
from discord.ext import commands, tasks
from google.genai import types

from config import JST
from utils.obsidian_utils import update_section, update_frontmatter

class DailyOrganizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client
        self.tasks_service = getattr(bot, 'tasks_service', None)

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
        
        # Google Tasks（未完了）を取得して重複を防ぐ
        current_tasks_text = "タスクAPIに接続されていません。"
        if self.tasks_service:
            current_tasks_text = await self.tasks_service.get_uncompleted_tasks()

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
                        if valid_temps: max_t, min_t = int(max(valid_temps)), int(min(valid_temps))
        except: pass

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
            prompt = f"""今日の会話ログを整理し、JSON形式で出力してください。
【指示】
1. 1日のジャーナルと箇条書きのメモの文末はすべて「である調（〜である、〜だ）」で統一すること。
2. ログの中から「User（私）」の投稿内容のみを抽出し、AIの発言内容は一切メモに含めないこと。
3. 私自身が書いたメモとして整理すること。
4. 箇条書きのメモは可能な限り私の投稿をすべて拾うこととし、整理はしますが、要約や大幅な削除は絶対にしないでください。
5. 全体の内容を振り返る短い日記を「journal」として作成してください。【今日の移動記録】がある場合はそれも踏まえて書いてください。
6. 【最重要】「next_actions」には、会話内で明示的に「タスクに追加して」と依頼した事柄や、以下の【現在の未完了タスク】に既に登録されている内容は **絶対に含めない** でください。会話の中でふと呟いた潜在的なアクションのみを抽出してください。見つからない場合は空配列 [] にしてください。

【現在の未完了タスク（Google ToDo リスト）】
{current_tasks_text}

【今日の移動記録】
{location_log_text}

【出力フォーマット】
以下のキーを持つJSONで出力してください（各値は箇条書きの配列形式、journalは文字列）。該当内容がない項目は空にしてください。
{{
  "journal": "今日一日の振り返り日記",
  "events": ["- 行動や出来事1", "- 行動や出来事2..."],
  "insights": ["- 気づきや考えたこと1", "- 気づきや考えたこと2..."],
  "next_actions": ["- アクション1", "- アクション2..."],
  "message": "最後に私へ一言、親密なタメ口でポジティブなおやすみの挨拶を書いてください"
}}
--- Chat Log ---
{log_text}"""
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
                    try:
                        await self.tasks_service.add_task(title=act)
                    except Exception as e:
                        logging.error(f"Google Tasks自動登録エラー: {e}")

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
                if raw_content:
                    content = raw_content
            except: pass

        # 1. フロントマター（プロパティ）の更新
        meta = data.get('meta', {})
        updates_fm = {'date': date_str}
        if meta.get('weather') != 'N/A': updates_fm['weather'] = meta.get('weather')
        if meta.get('temp_max') != 'N/A': updates_fm['temp_max'] = meta.get('temp_max')
        if meta.get('temp_min') != 'N/A': updates_fm['temp_min'] = meta.get('temp_min')
        content = update_frontmatter(content, updates_fm)

        # 2. 各セクションの更新（空白行や順序は utils が自動調整）
        if data.get('journal'):
            content = update_section(content, data['journal'], "## 📔 Daily Journal")
            
        if data.get('events') and len(data['events']) > 0:
            events_text = "\n".join(data['events']) if isinstance(data['events'], list) else str(data['events'])
            content = update_section(content, events_text, "## 📝 Events & Actions")
            
        if data.get('insights') and len(data['insights']) > 0:
            insights_text = "\n".join(data['insights']) if isinstance(data['insights'], list) else str(data['insights'])
            content = update_section(content, insights_text, "## 💡 Insights & Thoughts")
            
        if data.get('next_actions') and len(data['next_actions']) > 0:
            actions_text = "\n".join(data['next_actions']) if isinstance(data['next_actions'], list) else str(data['next_actions'])
            content = update_section(content, actions_text, "## ➡️ Next Actions")
        
        if f_id: 
            await self.drive_service.update_text(service, f_id, content)
        else: 
            await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", content)

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyOrganizeCog(bot))