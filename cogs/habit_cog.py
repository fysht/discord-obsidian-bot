import os
import json
import logging
from datetime import datetime, timedelta
import discord
from discord.ext import commands

from config import JST, BOT_FOLDER
from utils.obsidian_utils import update_frontmatter

HABIT_DATA_FILE = "habit_data.json"

class HabitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_service = bot.drive_service
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    async def _load_data(self):
        service = self.drive_service.get_service()
        if not service: return {"habits": [], "logs": {}}
        
        b_folder = await self.drive_service.find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder:
            b_folder = await self.drive_service.create_folder(service, self.drive_folder_id, BOT_FOLDER)
        
        f_id = await self.drive_service.find_file(service, b_folder, HABIT_DATA_FILE)
        if f_id:
            try:
                content = await self.drive_service.read_text_file(service, f_id)
                return json.loads(content)
            except: pass
        return {"habits": [], "logs": {}}

    async def _save_data(self, data):
        service = self.drive_service.get_service()
        if not service: return
        b_folder = await self.drive_service.find_file(service, self.drive_folder_id, BOT_FOLDER)
        f_id = await self.drive_service.find_file(service, b_folder, HABIT_DATA_FILE)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if f_id:
            await self.drive_service.update_text(service, f_id, content)
        else:
            await self.drive_service.upload_text(service, b_folder, HABIT_DATA_FILE, content)

    # --- AIから呼び出されるコア機能 ---

    async def complete_habit(self, habit_name_or_keyword: str, frequency_days: int = 1):
        """指定された習慣を完了にし、ストリーク（または累計回数）を返す"""
        data = await self._load_data()
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        
        target_habit = None
        for h in data['habits']:
            if habit_name_or_keyword.lower() in h['name'].lower():
                target_habit = h
                break
        
        if not target_habit:
            existing_ids = [int(h['id']) for h in data['habits']]
            new_id = str(max(existing_ids) + 1) if existing_ids else "1"
            # ★頻度（frequency_days）を保存
            target_habit = {"id": new_id, "name": habit_name_or_keyword, "frequency_days": frequency_days}
            data['habits'].append(target_habit)

        h_id = target_habit['id']
        if today_str not in data['logs']:
            data['logs'][today_str] = []
        
        if h_id not in data['logs'][today_str]:
            data['logs'][today_str].append(h_id)
            await self._save_data(data)
            
            await self._sync_to_obsidian(today_str, data)
            
            stats_msg = self._get_habit_stats(data, h_id, today_str)
            return f"習慣「{target_habit['name']}」を完了にしました！（{stats_msg}！）"
        else:
            return f"習慣「{target_habit['name']}」は既に今日完了しています。"

    async def list_habits(self):
        """現在登録されている習慣と頻度のリストを返す"""
        data = await self._load_data()
        if not data.get('habits'):
            return "現在登録されている習慣はありません。"
            
        lines = []
        for h in data['habits']:
            freq = h.get('frequency_days', 1)
            if freq == 1: freq_str = "毎日"
            elif freq == 7: freq_str = "週1回"
            else: freq_str = f"{freq}日に1回"
            lines.append(f"- {h['name']} ({freq_str})")
            
        return "【現在の習慣リスト】\n" + "\n".join(lines)

    async def delete_habit(self, habit_name_or_keyword: str):
        """指定された習慣をリストから削除する"""
        data = await self._load_data()
        
        target_habit = None
        for h in data['habits']:
            if habit_name_or_keyword.lower() in h['name'].lower():
                target_habit = h
                break
        
        if target_habit:
            data['habits'].remove(target_habit)
            await self._save_data(data)
            return f"習慣リストから「{target_habit['name']}」を完全に削除しました！"
        else:
            return f"リストの中に「{habit_name_or_keyword}」に一致する習慣は見つかりませんでした。"

    async def get_incomplete_habits(self):
        """今日の未完了の習慣リストを取得する（頻度を考慮）"""
        data = await self._load_data()
        today = datetime.now(JST).date()
        
        incomplete = []
        for h in data['habits']:
            h_id = h['id']
            freq = h.get('frequency_days', 1)
            
            # 頻度の日数分さかのぼり、1回でも完了していればOKとする
            is_completed = False
            for i in range(freq):
                d_str = (today - timedelta(days=i)).strftime('%Y-%m-%d')
                if d_str in data['logs'] and h_id in data['logs'][d_str]:
                    is_completed = True
                    break
                    
            if not is_completed:
                incomplete.append(h['name'])
                
        return incomplete

    def _get_habit_stats(self, data, habit_id, today_str):
        """毎日なら連続日数、週1回とかなら累計回数を返す"""
        target_habit = next((h for h in data['habits'] if h['id'] == habit_id), None)
        freq = target_habit.get('frequency_days', 1) if target_habit else 1
        
        if freq == 1:
            streak = 0
            current_date = datetime.strptime(today_str, '%Y-%m-%d').date()
            while True:
                d_str = current_date.strftime('%Y-%m-%d')
                if d_str in data['logs'] and habit_id in data['logs'][d_str]:
                    streak += 1
                    current_date -= timedelta(days=1)
                else:
                    break
            return f"現在 {streak} 日連続達成中"
        else:
            total = sum(1 for logs in data['logs'].values() if habit_id in logs)
            return f"累計 {total} 回達成"

    async def _sync_to_obsidian(self, date_str, data):
        """デイリーノートのプロパティ(YAML)に習慣の完了状況を反映"""
        service = self.drive_service.get_service()
        if not service: return
        daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: return
        f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
        if not f_id: return

        content = await self.drive_service.read_text_file(service, f_id)
        today_logs = data['logs'].get(date_str, [])
        
        updates = {}
        for h in data['habits']:
            key = f"habit_{h['name']}"
            updates[key] = "true" if h['id'] in today_logs else "false"
        
        new_content = update_frontmatter(content, updates)
        await self.drive_service.update_text(service, f_id, new_content)

async def setup(bot: commands.Bot):
    await bot.add_cog(HabitCog(bot))