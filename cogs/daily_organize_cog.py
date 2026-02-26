import os
import discord
from discord.ext import commands, tasks
from google.genai import types
import logging
import datetime
import json
import aiohttp
import re

from config import JST
from services.task_service import TaskService

class DailyOrganizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        # Bot本体から共通サービスを受け取る
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.daily_organize_task.is_running(): self.daily_organize_task.start()

    def cog_unload(self):
        self.daily_organize_task.cancel()

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

        result = {"journal": "", "events": [], "insights": [], "next_actions": [], "message": "（今日の会話とデータをノートにまとめたよ🌙 おやすみ！）"}
        if log_text.strip():
            prompt = f"""今日の会話ログを整理し、JSON形式で出力してください。
【指示】
1. メモの文末はすべて「である調（〜である、〜だ）」で統一すること。
2. 【最重要】ログの中から「User（私）」の投稿内容のみを抽出し、AIの発言内容は一切メモに含めないでください。
3. 【重要】私自身が書いたメモとして整理すること。「AIに話した」「AIが〜と言った」などの表現は完全に排除し、一人称視点（「〇〇をした」「〇〇について考えた」など）の事実や思考として記述してください。
4. 可能な限り私の投稿内容をすべて拾うこと。
5. 情報の整理はするが、要約や大幅な削除はしないこと。
6. 全体の内容を振り返る、読みやすくて感情豊かな短い日記（1〜2段落程度）を「journal」として作成してください。これも一人称の「である調」とします。

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
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                res_data = json.loads(response.text)
                result.update(res_data)
            except Exception as e: logging.error(f"DailyOrganize: JSON Error: {e}")

        result['meta'] = {'weather': weather, 'temp_max': max_t, 'temp_min': min_t, **fitbit_stats}
        await self._execute_organization(result, datetime.datetime.now(JST).strftime('%Y-%m-%d'))
        
        # --- 追加：Next ActionsをTaskLog.mdに自動登録 ---
        if result.get('next_actions'):
            # 行頭の "- " や箇条書きの記号を除去して純粋なタスク名にする
            clean_actions = [re.sub(r'^-\s*', '', act).strip() for act in result['next_actions']]
            if clean_actions:
                try:
                    ts = TaskService(self.drive_service)
                    await ts.add_tasks(clean_actions)
                except Exception as e:
                    logging.error(f"Next Action自動登録エラー: {e}")
        # ---------------------------------------------

        send_msg = result.get('message', '（今日の会話とデータをノートにまとめたよ🌙 今日も一日お疲れ様、おやすみ！）')
        await channel.send(send_msg)

    async def _execute_organization(self, data, date_str):
        service = self.drive_service.get_service()
        if not service: return

        daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: 
            daily_folder = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")
            
        f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
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
                raw_content = await self.drive_service.read_text_file(service, f_id)
                if raw_content.startswith("---"):
                    parts = raw_content.split("---", 2)
                    if len(parts) >= 3: current_body = parts[2].strip()
                    else: current_body = raw_content
                else: current_body = raw_content
            except: pass

        updates = []
        if data.get('journal'):
            updates.append(f"## 📔 Daily Journal\n{data['journal']}")
        if data.get('events') and len(data['events']) > 0:
            events_text = "\n".join(data['events']) if isinstance(data['events'], list) else str(data['events'])
            updates.append(f"## 📝 Events & Actions\n{events_text}")
        if data.get('insights') and len(data['insights']) > 0:
            insights_text = "\n".join(data['insights']) if isinstance(data['insights'], list) else str(data['insights'])
            updates.append(f"## 💡 Insights & Thoughts\n{insights_text}")
        if data.get('next_actions') and len(data['next_actions']) > 0:
            actions_text = "\n".join(data['next_actions']) if isinstance(data['next_actions'], list) else str(data['next_actions'])
            updates.append(f"## ➡️ Next Actions\n{actions_text}")

        new_content = frontmatter + current_body + "\n\n" + "\n\n".join(updates)
        
        if f_id: await self.drive_service.update_text(service, f_id, new_content)
        else: await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", new_content)

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyOrganizeCog(bot))