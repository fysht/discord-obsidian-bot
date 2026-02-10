import asyncio
import logging
import datetime
import zoneinfo
from googleapiclient.discovery import build

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class CalendarService:
    def __init__(self, creds, calendar_id="primary"):
        self.creds = creds
        self.calendar_id = calendar_id

    def get_service(self):
        if self.creds:
            return build('calendar', 'v3', credentials=self.creds)
        return None

    async def get_upcoming_events(self, minutes=15):
        """直近(指定分後まで)の予定を取得（通知用）"""
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
        """指定日の予定一覧を取得（チャット返答用）"""
        service = self.get_service()
        if not service: return "カレンダーに接続できませんでした。"

        try:
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            time_min = dt.replace(hour=0, minute=0, second=0).astimezone(JST).isoformat()
            time_max = dt.replace(hour=23, minute=59, second=59).astimezone(JST).isoformat()

            events_result = await asyncio.to_thread(lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime'
            ).execute())
            
            events = events_result.get('items', [])
            if not events: return f"{date_str} の予定は特にないみたいです。"

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

    async def create_event(self, summary, start_time, end_time, description=""):
        """予定を作成"""
        service = self.get_service()
        if not service: return "カレンダーに接続できませんでした。"

        event_body = {
            'summary': summary,
            'description': description,
            'start': {'dateTime': start_time, 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': end_time, 'timeZone': 'Asia/Tokyo'},
        }

        try:
            event = await asyncio.to_thread(lambda: service.events().insert(
                calendarId=self.calendar_id, body=event_body
            ).execute())
            return f"予定を作成しました: {event.get('htmlLink')}"
        except Exception as e:
            logging.error(f"Calendar Create Error: {e}")
            return f"作成に失敗しました: {e}"