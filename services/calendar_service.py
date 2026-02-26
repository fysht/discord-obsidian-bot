import json
import re
import datetime
import zoneinfo
import logging

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
DATA_FILE_NAME = "partner_data.json"
BOT_FOLDER = ".bot"
TASKS_FOLDER = "Tasks"
TASK_FILE_NAME = "TaskLog.md"

class TaskService:
    def __init__(self, drive_service):
        self.drive_service = drive_service
        self.reminders = []
        self.current_task = None
        self.notified_event_ids = set()
        self.last_interaction = datetime.datetime.now(JST)

    async def load_data(self):
        service = self.drive_service.get_service()
        if not service: return
        try:
            b_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, BOT_FOLDER)
            if not b_folder: return
            f_id = await self.drive_service.find_file(service, b_folder, DATA_FILE_NAME)
            if f_id:
                content = await self.drive_service.read_text_file(service, f_id)
                data = json.loads(content)
                self.reminders = data.get('reminders', [])
                ct = data.get('current_task')
                if ct: self.current_task = {'name': ct['name'], 'start': datetime.datetime.fromisoformat(ct['start'])}
                li = data.get('last_interaction')
                if li:
                    try: self.last_interaction = datetime.datetime.fromisoformat(li)
                    except ValueError: self.last_interaction = datetime.datetime.now(JST)
        except Exception as e:
            logging.error(f"TaskService Load Error: {e}")

    async def save_data(self):
        service = self.drive_service.get_service()
        if not service: return
        try:
            ct_save = None
            if self.current_task: ct_save = {'name': self.current_task['name'], 'start': self.current_task['start'].isoformat()}
            data = {'reminders': self.reminders, 'current_task': ct_save, 'last_interaction': self.last_interaction.isoformat()}
            json_str = json.dumps(data, ensure_ascii=False, indent=2)

            b_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, BOT_FOLDER)
            if not b_folder: b_folder = await self.drive_service.create_folder(service, self.drive_service.folder_id, BOT_FOLDER)

            f_id = await self.drive_service.find_file(service, b_folder, DATA_FILE_NAME)
            if f_id: await self.drive_service.update_text(service, f_id, json_str, mime_type='application/json')
            else: await self.drive_service.upload_text(service, b_folder, DATA_FILE_NAME, json_str)
        except Exception as e:
            logging.error(f"TaskService Save Error: {e}")

    def update_last_interaction(self):
        self.last_interaction = datetime.datetime.now(JST)

    async def add_reminders(self, reminders_data, user_id):
        added = []
        for r in reminders_data:
            self.reminders.append({'time': r['time'], 'content': r['content'], 'user_id': user_id})
            dt = datetime.datetime.fromisoformat(r['time'])
            added.append(f"{dt.strftime('%m/%d %H:%M')} に「{r['content']}」")
        await self.save_data()
        return "セット完了！\n・" + "\n・".join(added)

    def get_reminders_list(self):
        if not self.reminders: return "現在設定されているリマインダーはないよ！"
        res = ["【現在のリマインダー】"]
        for idx, r in enumerate(self.reminders):
            dt = datetime.datetime.fromisoformat(r['time'])
            res.append(f"{idx + 1}. {dt.strftime('%m/%d %H:%M')} - {r['content']}")
        return "\n".join(res)

    async def delete_reminders(self, indices):
        if not self.reminders: return "削除するリマインダーがないよ。"
        indices = sorted(list(set(indices)), reverse=True)
        deleted = []
        for idx in indices:
            if 0 <= idx - 1 < len(self.reminders):
                deleted.append(self.reminders.pop(idx - 1)['content'])
        await self.save_data()
        return f"削除したよ！: {', '.join(deleted)}" if deleted else "番号が見つからなかったよ。"

    def check_due_reminders(self):
        now = datetime.datetime.now(JST)
        due, remaining, is_changed = [], [], False
        for rem in self.reminders:
            target = datetime.datetime.fromisoformat(rem['time'])
            # 修正：タイムゾーンがない場合はJSTを付与する
            if target.tzinfo is None:
                target = target.replace(tzinfo=JST)
                
            if now >= target:
                due.append(rem)
                is_changed = True
            else:
                remaining.append(rem)
        self.reminders = remaining
        return due, is_changed

    async def _get_task_file_id(self, service):
        # 修正：run_in_executor を外して直接 await します
        tasks_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, TASKS_FOLDER)
        if not tasks_folder: 
            tasks_folder = await self.drive_service.create_folder(service, self.drive_service.folder_id, TASKS_FOLDER)
        task_file = await self.drive_service.find_file(service, tasks_folder, TASK_FILE_NAME)
        return task_file, tasks_folder

    async def get_task_list(self) -> str:
        service = self.drive_service.get_service()
        if not service: return "エラー"
        file_id, _ = await self._get_task_file_id(service)
        if not file_id: return "現在、タスクはないよ！"
        content = await self.drive_service.read_text_file(service, file_id)
        tasks = [line.strip() for line in content.split('\n') if re.match(r'^\s*-\s*\[ \]', line)]
        if not tasks: return "現在、未完了のタスクはないよ！"
        res = ["【現在の未完了タスク】"]
        for idx, t in enumerate(tasks):
            res.append(f"{idx + 1}. {re.sub(r'^\s*-\s*\[ \]\s*', '', t)}")
        return "\n".join(res)

    async def add_tasks(self, task_names: list):
        service = self.drive_service.get_service()
        if not service: return "エラー"
        file_id, folder_id = await self._get_task_file_id(service)
        content = await self.drive_service.read_text_file(service, file_id) if file_id else ""
        append_str = "\n".join([f"- [ ] {name}" for name in task_names])
        new_content = content + f"\n{append_str}" if content and not content.endswith('\n') else content + f"{append_str}\n"
        
        if file_id: await self.drive_service.update_text(service, file_id, new_content)
        else: await self.drive_service.upload_text(service, folder_id, TASK_FILE_NAME, new_content)
        return "タスクを追加したよ！\n・" + "\n・".join(task_names)

    async def modify_tasks(self, indices: list, action: str):
        service = self.drive_service.get_service()
        if not service: return "エラー"
        file_id, _ = await self._get_task_file_id(service)
        if not file_id: return "タスクがないよ。"
        
        content = await self.drive_service.read_text_file(service, file_id)
        lines = content.split('\n')
        task_line_indices = [i for i, line in enumerate(lines) if re.match(r'^\s*-\s*\[ \]', line)]
        
        affected = []
        for t_idx in sorted([idx - 1 for idx in indices], reverse=True):
            if 0 <= t_idx < len(task_line_indices):
                line_idx = task_line_indices[t_idx]
                affected.append(re.sub(r'^\s*-\s*\[ \]\s*', '', lines[line_idx]))
                if action == 'complete': lines[line_idx] = re.sub(r'\[ \]', '[x]', lines[line_idx], count=1)
                elif action == 'delete': lines.pop(line_idx)
                    
        if not affected: return "番号が見つからなかったよ。"
        await self.drive_service.update_text(service, file_id, "\n".join(lines))
        word = "完了にした" if action == 'complete' else "削除した"
        return f"以下のタスクを{word}よ！\n・" + "\n・".join(affected)

    def start_task(self, task_name):
        self.current_task = {'name': task_name, 'start': datetime.datetime.now(JST)}
    
    def finish_task(self):
        if not self.current_task: return None
        end_time = datetime.datetime.now(JST)
        start_time = self.current_task['start']
        duration = int((end_time - start_time).total_seconds() / 60)
        task_name = self.current_task['name']
        self.current_task = None
        return task_name, duration