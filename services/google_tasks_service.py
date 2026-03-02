import os
import logging
import asyncio
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

class GoogleTasksService:
    # ★ 修正: 初期化時に creds を受け取るように変更
    def __init__(self, creds):
        self.creds = creds

    def get_service(self):
        # ★ 修正: ファイルからではなく、受け取った creds を使って構築
        if self.creds:
            try:
                return build('tasks', 'v1', credentials=self.creds)
            except Exception as e:
                logging.error(f"Tasks API Auth Error: {e}")
        return None

    async def get_uncompleted_tasks(self):
        service = self.get_service()
        if not service: return "Tasks APIに接続できませんでした。"
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, 
                lambda: service.tasks().list(tasklist='@default', showCompleted=False).execute()
            )
            items = res.get('items', [])
            if not items: return "現在、未完了のタスクはありません。"
            
            tasks_text = []
            for t in items:
                tasks_text.append(f"- {t['title']}")
            return "\n".join(tasks_text)
        except Exception as e:
            return f"タスクの取得に失敗しました: {e}"

    async def add_task(self, title: str, notes: str = ""):
        service = self.get_service()
        if not service: return "Tasks APIに接続できませんでした。"
        try:
            loop = asyncio.get_running_loop()
            body = {'title': title, 'notes': notes}
            await loop.run_in_executor(
                None,
                lambda: service.tasks().insert(tasklist='@default', body=body).execute()
            )
            return f"タスク「{title}」をGoogle ToDoリストに追加したよ！"
        except Exception as e:
            return f"タスクの追加に失敗しました: {e}"

    async def complete_task_by_keyword(self, keyword: str):
        service = self.get_service()
        if not service: return "Tasks APIに接続できませんでした。"
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, 
                lambda: service.tasks().list(tasklist='@default', showCompleted=False).execute()
            )
            items = res.get('items', [])
            
            # キーワードに部分一致するタスクを探す
            target_task = next((t for t in items if keyword.lower() in t['title'].lower()), None)
            if not target_task:
                return f"「{keyword}」を含む未完了タスクが見つからなかったよ。"
            
            # ステータスを 'completed' (完了) に更新
            await loop.run_in_executor(
                None,
                lambda: service.tasks().patch(tasklist='@default', task=target_task['id'], body={'status': 'completed'}).execute()
            )
            return f"タスク「{target_task['title']}」を完了にしたよ！お疲れ様！"
        except Exception as e:
            return f"タスクの完了処理に失敗しました: {e}"