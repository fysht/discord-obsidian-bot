# ---------------------------------------------------------
# 1. インポート処理の整理
# ---------------------------------------------------------
import os
import logging
import datetime
import json
import aiohttp
import re

import discord
from discord.ext import commands, tasks
from google.genai import types

# ---------------------------------------------------------
# ローカルモジュールのインポート
# ---------------------------------------------------------
from config import JST
from services.task_service import TaskService

class DailyOrganizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

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

        # ---------------------------------------------------------
        # ★ 改善ポイント1: 処理開始前に現在のタスク一覧を取得しておく
        # ---------------------------------------------------------
        ts = TaskService(self.drive_service)
        await ts.load_data()
        current_tasks_text = await ts.get_task_list()

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
            # ---------------------------------------------------------
            # ★ 改善ポイント2: AIへのプロンプトで「既存タスクの除外」を強く指示
            # ---------------------------------------------------------
            prompt = f"""今日の会話ログを整理し、JSON形式で出力してください。
【指示】
1. メモの文末はすべて「である調（〜である、〜だ）」で統一すること。
2. ログの中から「User（私）」の投稿内容のみを抽出し、AIの発言内容は一切メモに含めないでください。
3. 私自身が書いたメモとして整理すること。「AIに話した」などの表現は完全に排除し、一人称視点（「〇〇をした」「〇〇について考えた」など）の事実や思考として記述してください。
4. 可能な限り私の投稿内容をすべて拾うこと。
5. 情報の整理はするが、要約や大幅な削除はしないこと。
6. 全体の内容を振り返る、読みやすくて感情豊かな短い日記（1〜2段落程度）を「journal」として作成してください。これも一人称の「である調」とします。
7. 【最重要】「next_actions」には、会話内で「タスクに追加して」と明示的に依頼した事柄や、既に以下の【現在のタスク一覧】に含まれている内容は **絶対に含めない** でください。会話の中でふと呟いた「明日〇〇しよう」「今度〇〇について調べよう」といった、まだタスク化されていない潜在的なアクションのみを抽出してください。見つからない場合は空配列 [] にしてください。

【現在のタスク一覧】
{current_tasks_text}

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
            except Exception as e: 
                logging.error(f"DailyOrganize: JSON Error: {e}")

        result['meta'] = {'weather': weather, 'temp_max': max_t, 'temp_min': min_t, **fitbit_stats}
        await self._execute_organization(result, datetime.datetime.now(JST).strftime('%Y-%m-%d'))
        
        # ---------------------------------------------------------
        # ★ 改善ポイント3: Python側での文字列比較フィルター（迎撃処理）
        # ---------------------------------------------------------
        if result.get('next_actions'):
            clean_actions = [re.sub(r'^-\s*', '', act).strip() for act in result['next_actions']]
            
            # 既存タスクとの部分一致チェック
            existing_tasks_lower = current_tasks_text.lower()
            unique_actions = []
            
            for act in clean_actions:
                # すでに存在するタスク名に似ていなければ新規として扱う
                if act and act.lower() not in existing_tasks_lower:
                    unique_actions.append(act)

            if unique_actions:
                try:
                    await ts.add_tasks(unique_actions)
                    await ts.save_data()
                except Exception as e:
                    logging.error(f"Next Action自動登録エラー: {e}")

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
        if data.get('journal'): updates.append(f"## 📔 Daily Journal\n{data['journal']}")
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