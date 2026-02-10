import json
import re
import datetime
import zoneinfo
import logging

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
DATA_FILE_NAME = "partner_data.json"
BOT_FOLDER = ".bot"

REMINDER_REGEX_MIN = re.compile(r'(\d+)分後')
REMINDER_REGEX_TIME = re.compile(r'(\d{1,2})[:時](\d{0,2})')

class TaskService:
    def __init__(self, drive_service):
        self.drive_service = drive_service
        self.reminders = []
        self.current_task = None
        self.notified_event_ids = set()
        # デフォルトは現在時刻
        self.last_interaction = datetime.datetime.now(JST)

    async def load_data(self):
        """Driveからデータをロード"""
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
                if ct:
                    self.current_task = {
                        'name': ct['name'], 
                        'start': datetime.datetime.fromisoformat(ct['start'])
                    }
                else:
                    self.current_task = None
                
                # 最終会話日時の読み込み
                li = data.get('last_interaction')
                if li:
                    try:
                        self.last_interaction = datetime.datetime.fromisoformat(li)
                    except ValueError:
                        self.last_interaction = datetime.datetime.now(JST)
                
                logging.info("TaskService: Data loaded.")
        except Exception as e:
            logging.error(f"TaskService Load Error: {e}")

    async def save_data(self):
        """Driveへデータを保存"""
        service = self.drive_service.get_service()
        if not service: return

        try:
            ct_save = None
            if self.current_task:
                ct_save = {
                    'name': self.current_task['name'], 
                    'start': self.current_task['start'].isoformat()
                }

            data = {
                'reminders': self.reminders,
                'current_task': ct_save,
                'last_interaction': self.last_interaction.isoformat() # 追加
            }
            json_str = json.dumps(data, ensure_ascii=False, indent=2)

            b_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, BOT_FOLDER)
            if not b_folder:
                b_folder = await self.drive_service.create_folder(service, self.drive_service.folder_id, BOT_FOLDER)

            f_id = await self.drive_service.find_file(service, b_folder, DATA_FILE_NAME)
            if f_id:
                await self.drive_service.update_text(service, f_id, json_str, mime_type='application/json')
            else:
                await self.drive_service.upload_text(service, b_folder, DATA_FILE_NAME, json_str)
                
        except Exception as e:
            logging.error(f"TaskService Save Error: {e}")

    def update_last_interaction(self):
        """最終会話日時を更新"""
        self.last_interaction = datetime.datetime.now(JST)

    def parse_and_add_reminder(self, text, user_id):
        now = datetime.datetime.now(JST)
        target_time = None
        content = "時間だよ！"

        m_match = REMINDER_REGEX_MIN.search(text)
        if m_match:
            mins = int(m_match.group(1))
            target_time = now + datetime.timedelta(minutes=mins)
            content = text.replace(m_match.group(0), "").strip() or "指定の時間だよ！"

        t_match = REMINDER_REGEX_TIME.search(text)
        if not target_time and t_match:
            hour = int(t_match.group(1))
            minute = int(t_match.group(2)) if t_match.group(2) else 0
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time < now:
                target_time += datetime.timedelta(days=1)
            content = text.replace(t_match.group(0), "").strip() or "指定の時間だよ！"

        if target_time:
            self.reminders.append({
                'time': target_time.isoformat(), 
                'content': content, 
                'user_id': user_id
            })
            return target_time.strftime('%H:%M')
        return None

    def check_due_reminders(self):
        now = datetime.datetime.now(JST)
        due = []
        remaining = []
        is_changed = False

        for rem in self.reminders:
            target = datetime.datetime.fromisoformat(rem['time'])
            if now >= target:
                due.append(rem)
                is_changed = True
            else:
                remaining.append(rem)
        
        self.reminders = remaining
        return due, is_changed

    def start_task(self, task_name):
        self.current_task = {
            'name': task_name,
            'start': datetime.datetime.now(JST)
        }
    
    def finish_task(self):
        if not self.current_task: return None
        end_time = datetime.datetime.now(JST)
        start_time = self.current_task['start']
        duration = int((end_time - start_time).total_seconds() / 60)
        task_name = self.current_task['name']
        self.current_task = None
        return task_name, duration