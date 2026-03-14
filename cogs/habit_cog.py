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

    async def complete_habit(self, habit_name_or_keyword: str):
        """指定された習慣を完了にし、ストリーク（連続日数）を返す"""
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
            target_habit = {"id": new_id, "name": habit_name_or_keyword}
            data['habits'].append(target_habit)

        h_id = target_habit['id']
        if today_str not in data['logs']:
            data['logs'][today_str] = []
        
        if h_id not in data['logs'][today_str]:
            data['logs'][today_str].append(h_id)
            await self._save_data(data)
            
            await self._sync_to_obsidian(today_str, data)
            
            streak = self._calculate_streak(data, h_id, today_str)
            return f"習慣「{target_habit['name']}」を完了にしました！（現在 {streak} 日連続達成中！）"
        else:
            return f"習慣「{target_habit['name']}」は既に今日完了しています。"

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
        """今日の未完了の習慣リストを取得する"""
        data = await self._load_data()
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        today_logs = data['logs'].get(today_str, [])
        
        incomplete = [h['name'] for h in data['habits'] if h['id'] not in today_logs]
        return incomplete

    def _calculate_streak(self, data, habit_id, today_str):
        streak = 0
        current_date = datetime.strptime(today_str, '%Y-%m-%d').date()
        while True:
            d_str = current_date.strftime('%Y-%m-%d')
            if d_str in data['logs'] and habit_id in data['logs'][d_str]:
                streak += 1
                current_date -= timedelta(days=1)
            else:
                break
        return streak

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