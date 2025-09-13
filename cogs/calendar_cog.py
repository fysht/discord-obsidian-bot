import os
import json
import logging
import asyncio
from datetime import datetime, time, timedelta, timezone

import discord
from discord.ext import commands, tasks
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode, DownloadError

from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = timezone(timedelta(hours=+9), 'JST')
TODAY_SCHEDULE_TIME = time(hour=7, minute=0, tzinfo=JST)
DAILY_REVIEW_TIME = time(hour=21, minute=30, tzinfo=JST)
MEMO_TO_CALENDAR_EMOJI = '📅'

# Google Calendar APIのスコープ
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/calendar.events']

class CalendarCog(commands.Cog):
    """
    Googleカレンダーと連携し、タスク管理を自動化するCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.uncompleted_tasks = []
        if not self._are_credentials_valid():
            logging.error("CalendarCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return
        try:
            self.creds = self._get_google_credentials()
            if not self.creds or not self.creds.valid:
                 # トークンが期限切れの可能性があればリフレッシュを試みる
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    self.creds.refresh(Request())
                    logging.info("Google APIのアクセストークンをリフレッシュしました。")
                else:
                    raise Exception("Google Calendarの認証情報が無効です。")

            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.dbx = self._initialize_dropbox_client()
            self.is_ready = True
            logging.info("✅ CalendarCogが正常に初期化され、準備が完了しました。")
        except Exception as e:
            logging.error(f"❌ CalendarCogの初期化中にエラーが発生しました: {e}", exc_info=True)

    def _load_environment_variables(self):
        self.calendar_channel_id = int(os.getenv("CALENDAR_CHANNEL_ID", 0))
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH")
        # .envファイルからtoken.jsonのパスを取得（ローカル用）、なければ'token.json'をデフォルトに
        self.google_token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")

    def _are_credentials_valid(self) -> bool:
        return all([
            self.calendar_channel_id, self.memo_channel_id, self.gemini_api_key, self.dropbox_refresh_token,
            self.dropbox_vault_path, self.google_token_path
        ])

    def _initialize_dropbox_client(self) -> dropbox.Dropbox:
        return dropbox.Dropbox(
            app_key=self.dropbox_app_key,
            app_secret=self.dropbox_app_secret,
            oauth2_refresh_token=self.dropbox_refresh_token
        )

    def _get_google_credentials(self):
        """RenderのSecret Fileまたはローカルのファイルから認証情報を読み込む"""
        if not os.path.exists(self.google_token_path):
            logging.error(f"Googleの認証ファイルが見つかりません。パス: {self.google_token_path}")
            return None
        try:
            creds = Credentials.from_authorized_user_file(self.google_token_path, SCOPES)
            return creds
        except Exception as e:
            logging.error(f"認証ファイルからの認証情報読み込みに失敗しました: {e}")
            return None

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.notify_today_events.is_running():
                self.notify_today_events.start()
            if not self.send_daily_review.is_running():
                self.send_daily_review.start()

    def cog_unload(self):
        self.notify_today_events.cancel()
        self.send_daily_review.cancel()

    @tasks.loop(time=TODAY_SCHEDULE_TIME)
    async def notify_today_events(self):
        logging.info("[CalendarCog] 今日の予定の通知タスクを開始します...")
        try:
            today = datetime.now(JST).date()
            time_min_dt = datetime.combine(today, time.min, tzinfo=JST)
            time_max_dt = datetime.combine(today, time.max, tzinfo=JST)

            service = build('calendar', 'v3', credentials=self.creds)
            events_result = service.events().list(
                calendarId='primary', timeMin=time_min_dt.isoformat(), timeMax=time_max_dt.isoformat(),
                singleEvents=True, orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])

            if not events:
                logging.info(f"[CalendarCog] {today} の予定はありませんでした。")
                return

            advice = await self._generate_overall_advice(events)
            embed = self._create_today_embed(today, events, advice)
            
            channel = self.bot.get_channel(self.calendar_channel_id)
            if channel:
                await channel.send(embed=embed)
            
            for event in events:
                await self._add_to_daily_log(event)

        except Exception as e:
            logging.error(f"[CalendarCog] 今日の予定通知中にエラー: {e}", exc_info=True)

    @tasks.loop(time=DAILY_REVIEW_TIME)
    async def send_daily_review(self):
        logging.info("[CalendarCog] 一日の振り返り通知タスクを開始します...")
        try:
            today_str = datetime.now(JST).strftime('%Y-%m-%d')
            log_path = f"{self.dropbox_vault_path}/.bot/calendar_log/{today_str}.json"
            
            try:
                _, res = self.dbx.files_download(log_path)
                daily_events = json.loads(res.content.decode('utf-8'))
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    logging.info(f"[CalendarCog] {today_str} の通知ログは見つかりませんでした。")
                    await self._carry_over_uncompleted_tasks()
                    return
                raise

            if not daily_events: 
                await self._carry_over_uncompleted_tasks()
                return

            channel = self.bot.get_channel(self.calendar_channel_id)
            if channel:
                await channel.send(f"--- **🗓️ {today_str} のタスクレビュー** ---\nお疲れ様でした！今日のタスクの達成度をリアクションで教えてください。")
                for event in daily_events:
                    embed = discord.Embed(
                        title=f"タスク: {event['summary']}",
                        color=discord.Color.gold()
                    )
                    message = await channel.send(embed=embed)
                    await message.add_reaction("✅")
                    await message.add_reaction("❌")
                await channel.send("--------------------")

            await self._carry_over_uncompleted_tasks()

        except Exception as e:
            logging.error(f"[CalendarCog] 振り返り通知中にエラー: {e}", exc_info=True)


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if payload.channel_id == self.calendar_channel_id:
            await self._handle_calendar_reaction(payload)
        elif payload.channel_id == self.memo_channel_id:
            await self._handle_memo_reaction(payload)

    async def _handle_calendar_reaction(self, payload: discord.RawReactionActionEvent):
        try:
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if message.author.id != self.bot.user.id or not message.embeds: return
            
            embed = message.embeds[0]
            if not embed.title or not embed.title.startswith("タスク: "): return

            task_summary = embed.title.replace("タスク: ", "")
            today_str = datetime.now(JST).strftime('%Y-%m-%d')
            target_date = datetime.strptime(today_str, '%Y-%m-%d').date()

            if str(payload.emoji) == '❌':
                self.uncompleted_tasks.append(task_summary)
                logging.info(f"[CalendarCog] 未完了タスクを追加: {task_summary}")

            task_list_md = f"- [{ 'x' if str(payload.emoji) == '✅' else ' ' }] {task_summary}\n"
            await self._update_obsidian_task_log(target_date, task_list_md)
            
            user = self.bot.get_user(payload.user_id)
            feedback_msg_content = f"「{task_summary}」のフィードバックありがとうございます！"
            if user:
                feedback_msg_content = f"{user.mention}さん、{feedback_msg_content}"
            
            feedback_msg = await channel.send(feedback_msg_content, delete_after=10)
            await message.delete(delay=10)

        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"[CalendarCog] カレンダーリアクション処理中にエラー: {e}", exc_info=True)
            
    async def _handle_memo_reaction(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != MEMO_TO_CALENDAR_EMOJI:
            return
            
        try:
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            
            if not message.content:
                return

            tasks = [line.strip() for line in message.content.split('\n') if line.strip()]
            if not tasks:
                return
            
            await message.add_reaction("⏳")
            
            today = datetime.now(JST).date()
            for task in tasks:
                await self._create_google_calendar_event(task, today)

            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
            
            feedback_msg = await channel.send(f"`{len(tasks)}`件のタスクを本日の終日予定としてGoogleカレンダーに登録しました。", delete_after=15)

        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"[CalendarCog] メモリアクション処理中にエラー: {e}", exc_info=True)
            if 'message' in locals():
                await message.add_reaction("❌")

    async def _carry_over_uncompleted_tasks(self):
        if not self.uncompleted_tasks:
            logging.info("[CalendarCog] 繰り越す未完了タスクはありません。")
            return

        logging.info(f"[CalendarCog] {len(self.uncompleted_tasks)}件の未完了タスクを翌日に繰り越します...")
        tomorrow = datetime.now(JST).date() + timedelta(days=1)
        
        for task in self.uncompleted_tasks:
            await self._create_google_calendar_event(task, tomorrow)
        
        self.uncompleted_tasks.clear()
        logging.info("[CalendarCog] 未完了タスクの繰り越しが完了しました。")

    async def _create_google_calendar_event(self, summary: str, date: datetime.date):
        event = {
            'summary': summary,
            'start': {'date': date.isoformat()},
            'end': {'date': date.isoformat()},
            'reminders': {
                'useDefault': False,
                'overrides': [],
            },
        }
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            logging.info(f"[CalendarCog] Googleカレンダーに予定を追加しました: '{summary}' on {date}")
        except HttpError as e:
            logging.error(f"Googleカレンダーへのイベント作成中にエラー: {e}")
        except Exception as e:
            logging.error(f"予期せぬエラーが発生しました: {e}", exc_info=True)

    async def _generate_overall_advice(self, events: list) -> str:
        event_list_str = ""
        for event in events:
            start = self._format_datetime(event.get('start'))
            event_list_str += f"- {start}: {event.get('summary', '名称未設定')}\n"
        prompt = f"""
        以下の今日の予定リスト全体を見て、一日を最も生産的に過ごすための総合的なアドバイスを提案してください。
        # 指示
        - 挨拶や前置きは不要です。
        - 箇条書きで、簡潔に3点ほどアドバイスを生成してください。
        # 今日の予定リスト
        {event_list_str}
        """
        try:
            response = await self.gem