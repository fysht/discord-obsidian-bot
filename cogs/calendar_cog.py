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
from google_auth_oauthlib.flow import InstalledAppFlow
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

# Google Calendar APIのスコープ
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

class CalendarCog(commands.Cog):
    """
    Googleカレンダーと連携し、タスク管理を自動化するCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        if not self._are_credentials_valid():
            logging.error("CalendarCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return
        try:
            self.creds = self._get_google_credentials()
            if not self.creds:
                logging.error("CalendarCog: Google Calendarの認証に失敗しました。")
                return
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.dbx = self._initialize_dropbox_client()
            self.is_ready = True
            logging.info("✅ CalendarCogが正常に初期化され、準備が完了しました。")
        except Exception as e:
            logging.error(f"❌ CalendarCogの初期化中にエラーが発生しました: {e}", exc_info=True)

    def _load_environment_variables(self):
        self.calendar_channel_id = int(os.getenv("CALENDAR_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH")
        self.google_credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        self.google_token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")

    def _are_credentials_valid(self) -> bool:
        return all([
            self.calendar_channel_id, self.gemini_api_key, self.dropbox_refresh_token,
            self.dropbox_vault_path, self.google_credentials_path, self.google_token_path
        ])

    def _initialize_dropbox_client(self) -> dropbox.Dropbox:
        return dropbox.Dropbox(
            app_key=self.dropbox_app_key,
            app_secret=self.dropbox_app_secret,
            oauth2_refresh_token=self.dropbox_refresh_token
        )

    def _get_google_credentials(self):
        creds = None
        if os.path.exists(self.google_token_path):
            creds = Credentials.from_authorized_user_file(self.google_token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logging.error(f"Googleトークンのリフレッシュに失敗しました: {e}")
                    os.remove(self.google_token_path)
                    return self._get_google_credentials()
            else:
                if not os.path.exists(self.google_credentials_path):
                    logging.error(f"{self.google_credentials_path} が見つかりません。")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(self.google_credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.google_token_path, 'w') as token:
                token.write(creds.to_json())
        return creds

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

            await self._update_obsidian_today_task_list(today, events)
            
            # 通知したイベントをログに記録
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
                    return
                raise

            if not daily_events: return

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
        except Exception as e:
            logging.error(f"[CalendarCog] 振り返り通知中にエラー: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or payload.channel_id != self.calendar_channel_id:
            return
        try:
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if message.author.id != self.bot.user.id or not message.embeds: return
            
            embed = message.embeds[0]
            if not embed.title or not embed.title.startswith("タスク: "): return

            task_summary = embed.title.replace("タスク: ", "")
            today_str = datetime.now(JST).strftime('%Y-%m-%d')
            target_date = datetime.strptime(today_str, '%Y-%m-%d').date()

            task_list_md = f"- [{ 'x' if str(payload.emoji) == '✅' else ' ' }] {task_summary}\n"
            await self._update_obsidian_task_log(target_date, task_list_md)
            
            user = self.bot.get_user(payload.user_id)
            if user:
                await message.remove_reaction(payload.emoji, user)
            
            feedback_msg = await channel.send(f"{user.mention}さん、「{task_summary}」のフィードバックありがとうございます！", delete_after=10)
            await message.delete(delay=10)

        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"[CalendarCog] リアクション処理中にエラー: {e}", exc_info=True)

    # --- ヘルパー関数 ---
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
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text
        except Exception as e:
            logging.error(f"総合アドバイスの生成に失敗: {e}")
            return "アドバイスの生成中にエラーが発生しました。"

    def _create_today_embed(self, date: datetime.date, events: list, advice: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"🗓️ {date.strftime('%Y-%m-%d')} の予定",
            description=f"**🤖 AIによる一日の過ごし方アドバイス**\n{advice}",
            color=discord.Color.green()
        )
        event_list = ""
        for event in events:
            start_str = self._format_datetime(event.get('start'))
            event_list += f"**{start_str}** {event.get('summary', '名称未設定')}\n"
        embed.add_field(name="タイムライン", value=event_list, inline=False)
        return embed

    def _format_datetime(self, dt_obj: dict) -> str:
        if 'dateTime' in dt_obj:
            dt = datetime.fromisoformat(dt_obj['dateTime']).astimezone(JST)
            return dt.strftime('%H:%M')
        elif 'date' in dt_obj:
            return "終日"
        return ""

    async def _add_to_daily_log(self, event: dict):
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        log_path = f"{self.dropbox_vault_path}/.bot/calendar_log/{today_str}.json"
        
        try:
            _, res = self.dbx.files_download(log_path)
            daily_events = json.loads(res.content.decode('utf-8'))
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                daily_events = []
            else:
                logging.error(f"デイリーログの読み込みに失敗: {e}")
                return

        if not any(e['id'] == event['id'] for e in daily_events):
            daily_events.append({
                'id': event['id'],
                'summary': event.get('summary', '名称未設定')
            })
            try:
                self.dbx.files_upload(
                    json.dumps(daily_events, indent=2, ensure_ascii=False).encode('utf-8'),
                    log_path,
                    mode=WriteMode('overwrite')
                )
            except Exception as e:
                logging.error(f"デイリーログの保存に失敗: {e}")
            
    async def _update_obsidian_today_task_list(self, date: datetime.date, events: list):
        date_str = date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        task_list_md = ""
        for event in events:
            task_list_md += f"- [ ] {event.get('summary', '名称未設定')}\n"
        
        for attempt in range(3):
            try:
                try:
                    _, res = self.dbx.files_download(daily_note_path)
                    current_content = res.content.decode('utf-8')
                except ApiError as e:
                    if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        current_content = ""
                    else: raise

                new_content = update_section(current_content, task_list_md.strip(), "## Task List")

                self.dbx.files_upload(
                    new_content.encode('utf-8'),
                    daily_note_path,
                    mode=WriteMode('overwrite')
                )
                logging.info(f"Obsidianの今日のデイリーノートを更新しました: {daily_note_path}")
                return
            except Exception as e:
                logging.error(f"Obsidianの今日のデイリーノート更新に失敗 (試行 {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    logging.error("リトライの上限に達しました。アップロードを断念します。")

    async def _update_obsidian_task_log(self, date: datetime.date, log_content: str):
        date_str = date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"

        for attempt in range(3):
            try:
                try:
                    _, res = self.dbx.files_download(daily_note_path)
                    current_content = res.content.decode('utf-8')
                except ApiError as e:
                    if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        current_content = ""
                    else: raise

                new_content = update_section(current_content, log_content.strip(), "## Task Log")

                self.dbx.files_upload(
                    new_content.encode('utf-8'),
                    daily_note_path,
                    mode=WriteMode('overwrite')
                )
                logging.info(f"Obsidianのタスクログを更新しました: {daily_note_path}")
                return
            except Exception as e:
                logging.error(f"Obsidianタスクログの更新に失敗 (試行 {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    logging.error("リトライの上限に達しました。アップロードを断念します。")

async def setup(bot: commands.Bot):
    """Cogをボットに登録するためのセットアップ関数"""
    await bot.add_cog(CalendarCog(bot))