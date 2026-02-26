import asyncio
import logging
import datetime
import zoneinfo
from googleapiclient.discovery import build

from config import JST

class CalendarService:
    def __init__(self, creds, calendar_id="primary"):
        self.creds = creds
        self.calendar_id = calendar_id

    def get_service(self):
        if self.creds:
            return build('calendar', 'v3', credentials=self.creds)
        return None

    async def get_upcoming_events(self, minutes=15):
        service = self.get_service()
        if not service: return []

        now = datetime.datetime.now(JST)
        time_min = now.isoformat()
        time_max = (now + datetime.timedelta(minutes=minutes)).isoformat()

        try:
            events_result = await asyncio.to_thread(lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime'
            ).execute())
            return events_result.get('items', [])
        except Exception as e:
            logging.error(f"Calendar Fetch Error: {e}")
            return []

    async def list_events_for_date(self, date_str):
        service = self.get_service()
        if not service: return "エラー: カレンダーに接続できません。"

        try:
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=JST)
            time_min = dt.replace(hour=0, minute=0, second=0).isoformat()
            time_max = dt.replace(hour=23, minute=59, second=59).isoformat()
            
            events_result = await asyncio.to_thread(lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime'
            ).execute())
            events = events_result.get('items', [])
            
            if not events: return f"{date_str} の予定は特にないみたいだよ。"
            
            result = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                summary = event.get('summary', '(タイトルなし)')
                if 'T' in start:
                    t_obj = datetime.datetime.fromisoformat(start)
                    t_str = t_obj.strftime('%H:%M')
                    result.append(f"- {t_str} : {summary}")
                else:
                    result.append(f"- 終日 : {summary}")
            
            return f"【{date_str} の予定】\n" + "\n".join(result)
        except Exception as e:
            logging.error(f"Calendar List Error: {e}")
            return f"エラーが発生しました: {e}"

    # 修正：AIが生成したラフな日時文字列をGoogle Calendar向けの厳密な形式に直す
    def _format_iso_time(self, t_str):
        try:
            # 1. 余計な空白を取り除き、スラッシュをハイフンに、Tを空白に統一
            t_str = t_str.strip().replace("/", "-").replace("T", " ")
            
            # 2. "2026-02-26 10:00" のように秒がない場合は ":00" を足す
            if t_str.count(":") == 1:
                t_str += ":00"
            
            # 3. 文字列を datetime オブジェクトに変換
            dt = datetime.datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
            
            # 4. タイムゾーン(JST)を設定してISOフォーマットで出力
            dt = dt.replace(tzinfo=JST)
            return dt.isoformat()
            
        except Exception as e:
            # 万が一、AIが想定外の文字列を出した時のフォールバック（保険）
            logging.warning(f"日時フォーマットの変換に失敗しました: '{t_str}' - {e}")
            t_str = str(t_str).replace(" ", "T")
            if len(t_str) == 16:
                t_str += ":00"
            return t_str

    async def create_event(self, summary, start_time, end_time, description=""):
        service = self.get_service()
        if not service: return "カレンダーに接続できませんでした。"

        event_body = {
            'summary': summary,
            'description': description,
            # 修正：厳密なISOフォーマットにパースして送る
            'start': {'dateTime': self._format_iso_time(start_time), 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': self._format_iso_time(end_time), 'timeZone': 'Asia/Tokyo'},
        }

        try:
            event = await asyncio.to_thread(lambda: service.events().insert(
                calendarId=self.calendar_id, body=event_body
            ).execute())
            return f"予定を作成したよ！: {event.get('htmlLink')}"
        except Exception as e:
            logging.error(f"Calendar Create Error: {e}")
            return f"予定の作成に失敗しました。: {e}"