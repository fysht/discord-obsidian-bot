import os
import json
import logging
from datetime import datetime, time, timedelta
import re
from discord.ext import commands, tasks

from config import JST, BOT_FOLDER

HABIT_DATA_FILE = "habit_data.json"


class HabitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_service = bot.drive_service
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.sync_habits_loop.start()
        self.nightly_habit_sync.start()

    def cog_unload(self):
        self.sync_habits_loop.cancel()
        self.nightly_habit_sync.cancel()

    async def _load_data(self):
        service = self.drive_service.get_service()
        if not service:
            return {"habits": [], "logs": {}}
        b_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, BOT_FOLDER
        )
        if not b_folder:
            b_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, BOT_FOLDER
            )
        f_id = await self.drive_service.find_file(service, b_folder, HABIT_DATA_FILE)
        if f_id:
            try:
                return json.loads(
                    await self.drive_service.read_text_file(service, f_id)
                )
            except Exception:
                pass
        return {"habits": [], "logs": {}}

    async def _save_data(self, data):
        service = self.drive_service.get_service()
        if not service:
            return
        b_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, BOT_FOLDER
        )
        f_id = await self.drive_service.find_file(service, b_folder, HABIT_DATA_FILE)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if f_id:
            await self.drive_service.update_text(service, f_id, content)
        else:
            await self.drive_service.upload_text(
                service, b_folder, HABIT_DATA_FILE, content
            )

    async def complete_habit(self, habit_name_or_keyword: str, frequency_days: int = 1):
        """Discordから完了報告された場合の処理"""
        if hasattr(self.bot, "tasks_service") and self.bot.tasks_service:
            await self.bot.tasks_service.complete_task_by_keyword(
                habit_name_or_keyword, list_name="習慣"
            )
        return await self._process_habit_completion(
            habit_name_or_keyword, frequency_days
        )

    async def _process_habit_completion(
        self, habit_name_or_keyword: str, frequency_days: int = 1
    ):
        data = await self._load_data()
        today_str = datetime.now(JST).strftime("%Y-%m-%d")

        target_habit = next(
            (
                h
                for h in data["habits"]
                if habit_name_or_keyword.lower() in h["name"].lower()
            ),
            None,
        )
        if not target_habit:
            existing_ids = [int(h["id"]) for h in data["habits"]]
            new_id = str(max(existing_ids) + 1) if existing_ids else "1"
            target_habit = {
                "id": new_id,
                "name": habit_name_or_keyword,
                "frequency_days": frequency_days,
            }
            data["habits"].append(target_habit)

        h_id = target_habit["id"]
        if today_str not in data["logs"]:
            data["logs"][today_str] = []

        if h_id not in data["logs"][today_str]:
            data["logs"][today_str].append(h_id)
            await self._save_data(data)
            stats_msg = self._get_habit_stats(data, h_id, today_str)
            # ★ 変更: AIに毎回違う言葉で褒めさせるための「システム通知」の裏プロンプト
            return f"【システム通知】習慣「{target_habit['name']}」の完了を記録しました。（{stats_msg}）\nこの記録を踏まえて、ユーザーをLINE風のタメ口で全力で褒めてください！毎回同じ定型文にならないよう表現を変え、もし3日、7日、14日、30日などのキリの良い記録であれば、さらにテンション高めでお祝いしてください。"
        else:
            stats_msg = self._get_habit_stats(data, h_id, today_str)
            # ★ 変更: すでに完了している場合もAIに優しく褒めさせる
            return f"【システム通知】習慣「{target_habit['name']}」はすでに今日の分が記録済みです。（{stats_msg}）\nこのことを踏まえて、ユーザーの継続をLINE風のタメ口で優しく褒めてあげてください。定型文は避けてください。"

    @tasks.loop(minutes=30)
    async def sync_habits_loop(self):
        """カレンダー上で完了したタスクを裏側で検知して褒めるループ"""
        if not hasattr(self.bot, "tasks_service") or not self.bot.tasks_service:
            return
        try:
            completed_titles = await self.bot.tasks_service.get_completed_tasks_today(
                list_name="習慣"
            )
            if not completed_titles:
                return

            data = await self._load_data()
            today_str = datetime.now(JST).strftime("%Y-%m-%d")
            if today_str not in data["logs"]:
                data["logs"][today_str] = []

            newly_completed = []
            for title in completed_titles:
                target_habit = next(
                    (
                        h
                        for h in data["habits"]
                        if title.lower() in h["name"].lower()
                        or h["name"].lower() in title.lower()
                    ),
                    None,
                )
                if target_habit:
                    h_id = target_habit["id"]
                    if h_id not in data["logs"][today_str]:
                        data["logs"][today_str].append(h_id)
                        newly_completed.append(target_habit)
                else:
                    existing_ids = [int(h["id"]) for h in data["habits"]]
                    new_id = str(max(existing_ids) + 1) if existing_ids else "1"
                    target_habit = {"id": new_id, "name": title, "frequency_days": 1}
                    data["habits"].append(target_habit)
                    data["logs"][today_str].append(new_id)
                    newly_completed.append(target_habit)

            if newly_completed:
                await self._save_data(data)

                channel = self.bot.get_channel(self.memo_channel_id)
                partner_cog = self.bot.get_cog("PartnerCog")
                if channel and partner_cog:
                    for h in newly_completed:
                        stats_msg = self._get_habit_stats(data, h["id"], today_str)
                        # ★ 変更: 定型文を禁止し、ダイナミックに褒める指示へ変更
                        instruction = f"ユーザーがGoogleカレンダー上で習慣「{h['name']}」を完了させたのを検知しました。現在の記録は「{stats_msg}」です。これを踏まえて、LINE風の温かいタメ口で全力で褒める短いメッセージを送ってください。定型文は禁止です。毎日違う言葉でモチベーションを上げ、3日や7日などキリの良い数字なら特別にお祝いしてください！"
                        await partner_cog.generate_and_send_routine_message(
                            "", instruction
                        )
        except Exception as e:
            logging.error(f"Habit sync error: {e}")

    @tasks.loop(time=time(hour=23, minute=50, tzinfo=JST))
    async def nightly_habit_sync(self):
        """1日の終わりにTasksの完了状況を最終チェックし、Obsidianに確実に書き込む"""
        logging.info("夜の習慣最終チェックを開始します。")
        if not hasattr(self.bot, "tasks_service") or not self.bot.tasks_service:
            return
        try:
            completed_titles = await self.bot.tasks_service.get_completed_tasks_today(
                list_name="習慣"
            )
            data = await self._load_data()
            today_str = datetime.now(JST).strftime("%Y-%m-%d")
            if today_str not in data["logs"]:
                data["logs"][today_str] = []

            if completed_titles:
                for title in completed_titles:
                    target_habit = next(
                        (
                            h
                            for h in data["habits"]
                            if title.lower() in h["name"].lower()
                            or h["name"].lower() in title.lower()
                        ),
                        None,
                    )
                    if target_habit:
                        h_id = target_habit["id"]
                        if h_id not in data["logs"][today_str]:
                            data["logs"][today_str].append(h_id)
                    else:
                        existing_ids = [int(h["id"]) for h in data["habits"]]
                        new_id = str(max(existing_ids) + 1) if existing_ids else "1"
                        target_habit = {
                            "id": new_id,
                            "name": title,
                            "frequency_days": 1,
                        }
                        data["habits"].append(target_habit)
                        data["logs"][today_str].append(new_id)

            await self._save_data(data)
            # ★ 1日1回、ここでのみObsidianのフロントマターへ一括記録する
            await self._sync_to_obsidian(today_str, data)
            logging.info("夜の習慣最終チェックとObsidianへの保存が完了しました。")
        except Exception as e:
            logging.error(f"Nightly habit sync error: {e}")

    @sync_habits_loop.before_loop
    @nightly_habit_sync.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()

    async def list_habits(self):
        data = await self._load_data()
        if not data.get("habits"):
            return "現在登録されている習慣はありません。"
        lines = []
        for h in data["habits"]:
            freq = h.get("frequency_days", 1)
            freq_str = (
                "毎日" if freq == 1 else ("週1回" if freq == 7 else f"{freq}日に1回")
            )
            lines.append(f"- {h['name']} ({freq_str})")
        return "【現在の習慣リスト】\n" + "\n".join(lines)

    async def delete_habit(self, habit_name_or_keyword: str):
        data = await self._load_data()
        target_habit = next(
            (
                h
                for h in data["habits"]
                if habit_name_or_keyword.lower() in h["name"].lower()
            ),
            None,
        )
        if target_habit:
            data["habits"].remove(target_habit)
            await self._save_data(data)
            return f"習慣リストから「{target_habit['name']}」を完全に削除しました！"
        return f"リストの中に「{habit_name_or_keyword}」に一致する習慣は見つかりませんでした。"

    def _get_habit_stats(self, data, habit_id, today_str):
        target_habit = next((h for h in data["habits"] if h["id"] == habit_id), None)
        freq = target_habit.get("frequency_days", 1) if target_habit else 1

        if freq == 1:
            streak = 0
            current_date = datetime.strptime(today_str, "%Y-%m-%d").date()
            while True:
                d_str = current_date.strftime("%Y-%m-%d")
                if d_str in data["logs"] and habit_id in data["logs"][d_str]:
                    streak += 1
                    current_date -= timedelta(days=1)
                else:
                    break
            return f"現在 {streak} 日連続達成中"
        else:
            total = sum(1 for logs in data["logs"].values() if habit_id in logs)
            return f"累計 {total} 回達成"

    async def _sync_to_obsidian(self, date_str, data):
        """デイリーノートのフロントマターを直接更新する"""
        service = self.drive_service.get_service()
        if not service:
            return
        daily_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, "DailyNotes"
        )
        if not daily_folder:
            daily_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, "DailyNotes"
            )

        f_id = await self.drive_service.find_file(
            service, daily_folder, f"{date_str}.md"
        )

        if f_id:
            content = await self.drive_service.read_text_file(service, f_id)
        else:
            content = f"---\ndate: {date_str}\n---\n\n# Daily Note {date_str}\n"

        today_logs = data["logs"].get(date_str, [])
        updates = {
            f"habit_{h['name']}": ("true" if h["id"] in today_logs else "false")
            for h in data["habits"]
        }

        frontmatter_pattern = r"^---\n(.*?)\n---"
        match = re.search(frontmatter_pattern, content, re.DOTALL)

        fm_map = {}
        body = content
        if match:
            fm_content = match.group(1)
            body = content[match.end() :]
            for line in fm_content.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    fm_map[key.strip()] = val.strip()
        else:
            fm_map["date"] = date_str

        for k, v in updates.items():
            fm_map[k] = v

        new_fm_lines = [f"{k}: {v}" for k, v in fm_map.items()]
        new_content = "---\n" + "\n".join(new_fm_lines) + "\n---\n" + body.lstrip()

        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(
                service, daily_folder, f"{date_str}.md", new_content
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(HabitCog(bot))
