import os
import logging
import asyncio
import datetime
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

class GoogleTasksService:
    def __init__(self, creds):
        self.creds = creds

    def get_service(self):
        if self.creds:
            try:
                return build('tasks', 'v1', credentials=self.creds)
            except Exception as e:
                logging.error(f"Tasks API Auth Error: {e}")
        return None

    async def _get_tasklist_id(self, service, list_name: str) -> str:
        """指定された名前のリストIDを取得する。なければデフォルトを返す"""
        if not list_name: return '@default'
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, lambda: service.tasklists().list().execute())
            for item in res.get('items', []):
                if list_name.lower() in item['title'].lower():
                    return item['id']
            # 見つからなかった場合は新規作成する
            res = await loop.run_in_executor(None, lambda: service.tasklists().insert(body={'title': list_name}).execute())
            return res.get('id', '@default')
        except Exception as e:
            logging.error(f"Tasklist ID fetch error: {e}")
            return '@default'

    async def get_uncompleted_tasks(self, list_name: str = None):
        service = self.get_service()
        if not service: return "Tasks APIに接続できませんでした。"
        try:
            list_id = await self._get_tasklist_id(service, list_name)
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, 
                lambda: service.tasks().list(tasklist=list_id, showCompleted=False).execute()
            )
            items = res.get('items', [])
            if not items: return f"「{list_name or 'デフォルト'}」リストに未完了のタスクはありません。"
            
            tasks_text = [f"- {t['title']}" for t in items]
            return "\n".join(tasks_text)
        except Exception as e:
            return f"タスクの取得に失敗しました: {e}"

    async def add_task(self, title: str, notes: str = "", list_name: str = None):
        service = self.get_service()
        if not service: return "Tasks APIに接続できませんでした。"
        try:
            list_id = await self._get_tasklist_id(service, list_name)
            loop = asyncio.get_running_loop()
            body = {'title': title, 'notes': notes}
            await loop.run_in_executor(
                None,
                lambda: service.tasks().insert(tasklist=list_id, body=body).execute()
            )
            return f"リスト「{list_name or 'デフォルト'}」にタスク「{title}」を追加したよ！"
        except Exception as e:
            return f"タスクの追加に失敗しました: {e}"

    async def complete_task_by_keyword(self, keyword: str, list_name: str = None):
        service = self.get_service()
        if not service: return "Tasks APIに接続できませんでした。"
        try:
            list_id = await self._get_tasklist_id(service, list_name)
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, 
                lambda: service.tasks().list(tasklist=list_id, showCompleted=False).execute()
            )
            items = res.get('items', [])
            
            target_task = next((t for t in items if keyword.lower() in t['title'].lower()), None)
            if not target_task:
                return f"「{keyword}」を含む未完了タスクがリスト「{list_name or 'デフォルト'}」に見つからなかったよ。"
            
            await loop.run_in_executor(
                None,
                lambda: service.tasks().patch(tasklist=list_id, task=target_task['id'], body={'status': 'completed'}).execute()
            )
            return f"タスク「{target_task['title']}」を完了にしたよ！お疲れ様！"
        except Exception as e:
            return f"タスクの完了処理に失敗しました: {e}"

    async def get_completed_tasks_today(self, list_name: str = "習慣"):
        """今日完了になったタスク（カレンダー上でチェックされたもの）を取得する"""
        service = self.get_service()
        if not service: return []
        try:
            list_id = await self._get_tasklist_id(service, list_name)
            if list_id == '@default' and list_name == "習慣": return [] # 防御的処理
            
            jst = datetime.timezone(datetime.timedelta(hours=9))
            now = datetime.datetime.now(jst)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            updated_min = today_start.isoformat()
            
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, 
                lambda: service.tasks().list(tasklist=list_id, showCompleted=True, showHidden=True, updatedMin=updated_min).execute()
            )
            
            completed_titles = []
            for t in res.get('items', []):
                if t.get('status') == 'completed' and 'completed' in t:
                    try:
                        comp_time = datetime.datetime.fromisoformat(t['completed'].replace('Z', '+00:00'))
                        if comp_time.astimezone(jst).date() == now.date():
                            completed_titles.append(t['title'])
                    except: pass
            return completed_titles
        except Exception as e:
            logging.error(f"get_completed_tasks_today error: {e}")
            return []