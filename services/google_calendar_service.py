import asyncio
import logging
import datetime
import zoneinfo
from googleapiclient.discovery import build

from config import JST

class GoogleCalendarService:
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
                event_id = event.get('id')
                
                if 'T' in start:
                    t_obj = datetime.datetime.fromisoformat(start)
                    t_str = t_obj.strftime('%H:%M')
                    result.append(f"- {t_str} : {summary} (ID: {event_id})")
                else:
                    result.append(f"- 終日 : {summary} (ID: {event_id})")
            
            return f"【{date_str} の予定】\n" + "\n".join(result)
        except Exception as e:
            logging.error(f"Calendar List Error: {e}")
            return f"エラーが発生しました: {e}"

    # AIが生成したラフな日時文字列をGoogle Calendar向けの厳密な形式に直す
    def _format_iso_time(self, t_str):
        try:
            t_str = t_str.strip().replace("/", "-").replace("T", " ")
            if t_str.count(":") == 1:
                t_str += ":00"
            dt = datetime.datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=JST)
            return dt.isoformat()
        except Exception as e:
            logging.warning(f"日時フォーマットの変換に失敗しました: '{t_str}' - {e}")
            t_str = str(t_str).replace(" ", "T")
            if len(t_str) == 16:
                t_str += ":00"
            return t_str

    async def create_event(self, summary, start_time, end_time, description=""):
        service = self.get_service()
        if not service: return "カレンダーに接続できませんでした。"

        start_str = str(start_time).strip()
        end_str = str(end_time).strip()

        # --- 修正箇所：終日予定かどうかの判定 ---
        # 1. AIが "00:00" 〜 "23:59" のように送ってきている場合
        # 2. "2026-02-26" のように日付だけの場合
        is_all_day = False
        if ("00:00" in start_str and "23:59" in end_str) or (len(start_str) <= 10):
            is_all_day = True

        event_body = {
            'summary': summary,
            'description': description,
        }

        if is_all_day:
            # 終日の場合、'date' フィールドを使い 'YYYY-MM-DD' 形式にする
            s_date = start_str[:10].replace("/", "-")
            e_date = end_str[:10].replace("/", "-")
            
            # Googleカレンダーの終日予定は終了日を含まない(exclusive)仕様なので、
            # 開始日と同じ日なら、終了日を1日進める
            try:
                dt_start = datetime.datetime.strptime(s_date, "%Y-%m-%d")
                dt_end = datetime.datetime.strptime(e_date, "%Y-%m-%d")
                if dt_start >= dt_end:
                    dt_end = dt_start + datetime.timedelta(days=1)
                e_date = dt_end.strftime("%Y-%m-%d")
            except:
                pass # 変換に失敗した場合はそのまま送る

            event_body['start'] = {'date': s_date}
            event_body['end'] = {'date': e_date}
        else:
            # 時刻がある場合は通常通り
            event_body['start'] = {'dateTime': self._format_iso_time(start_time), 'timeZone': 'Asia/Tokyo'}
            event_body['end'] = {'dateTime': self._format_iso_time(end_time), 'timeZone': 'Asia/Tokyo'}

        try:
            event = await asyncio.to_thread(lambda: service.events().insert(
                calendarId=self.calendar_id, body=event_body
            ).execute())
            return f"予定を作成したよ！: {event.get('htmlLink')}"
        except Exception as e:
            logging.error(f"Calendar Create Error: {e}")
            return f"予定の作成に失敗しました。: {e}"

    # --- 新規追加：予定の削除機能 ---
    async def delete_event(self, event_id):
        """イベントIDを指定して予定を削除する"""
        service = self.get_service()
        if not service: return "カレンダーに接続できませんでした。"

        try:
            await asyncio.to_thread(lambda: service.events().delete(
                calendarId=self.calendar_id, eventId=event_id
            ).execute())
            return "カレンダーから予定を削除しました。"
        except Exception as e:
            logging.error(f"Calendar Delete Error: {e}")
            return f"予定の削除に失敗しました: {e}"

    async def delete_event_by_keyword(self, date_str, keyword):
        """指定した日付の中で、キーワードに一致する予定を探して削除する（AI向け）"""
        service = self.get_service()
        if not service: return "カレンダーに接続できませんでした。"
        
        try:
            # 指定日の予定を検索
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=JST)
            time_min = dt.replace(hour=0, minute=0, second=0).isoformat()
            time_max = dt.replace(hour=23, minute=59, second=59).isoformat()
            
            events_result = await asyncio.to_thread(lambda: service.events().list(
                calendarId=self.calendar_id, timeMin=time_min, timeMax=time_max,
                q=keyword, singleEvents=True, orderBy='startTime'
            ).execute())
            events = events_result.get('items', [])
            
            if not events:
                return f"{date_str} に「{keyword}」を含む予定は見つからなかったため、削除できませんでした。"
            
            # 見つかった最初の予定を削除
            target_event = events[0]
            event_id = target_event['id']
            summary = target_event.get('summary', '(タイトルなし)')
            
            await asyncio.to_thread(lambda: service.events().delete(
                calendarId=self.calendar_id, eventId=event_id
            ).execute())
            
            return f"カレンダーから予定「{summary}」を削除したよ！"
        except Exception as e:
            logging.error(f"Calendar Delete Keyword Error: {e}")
            return f"予定の検索または削除中にエラーが発生しました: {e}"