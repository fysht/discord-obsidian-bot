import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import json
from datetime import datetime, date, timedelta, time
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import google.generativeai as genai
import re

# Google Calendar Imports
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("LifeLogCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
ACTIVE_LOGS_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/active_lifelogs.json"
PLANNING_STATE_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/planning_state.json"
DAILY_NOTE_HEADER = "## Life Logs"
SUMMARY_NOTE_HEADER = "## Life Logs Summary"
PLANNING_HEADER = "## Planning"
READING_NOTES_PATH = "/Reading Notes"
DAILY_SUMMARY_TIME = time(hour=6, minute=0, tzinfo=JST)
DEFAULT_PLANNING_TIME = time(hour=7, minute=0, tzinfo=JST)

# --- 時間解析用の正規表現 ---
DURATION_REGEX = re.compile(r'\s+(\d+(?:\.\d+)?)(h|m|min|hour|時間|分)?$', re.IGNORECASE)

# ==========================================
# UI Components
# ==========================================

class LifeLogMemoModal(discord.ui.Modal, title="作業メモの入力"):
    memo_text = discord.ui.TextInput(
        label="メモ（詳細、進捗など）",
        placeholder="例: 今日のメニューはカレーとサラダ",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.cog.add_memo_to_task(interaction, self.memo_text.value)

class LifeLogPlanningModal(discord.ui.Modal, title="朝のプランニング"):
    highlight = discord.ui.TextInput(
        label="今日のハイライト (★最重要タスク)",
        placeholder="例: 企画書を完成させる",
        style=discord.TextStyle.short,
        required=False,
        max_length=200,
        row=0
    )
    schedule = discord.ui.TextInput(
        label="スケジュール (カレンダー同期済)",
        placeholder="09:00 朝会\n10:00 作業A...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=3000, 
        row=1
    )

    def __init__(self, cog, default_schedule="", default_highlight=""):
        super().__init__()
        self.cog = cog
        self.schedule.default = default_schedule
        self.highlight.default = default_highlight

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await self.cog.submit_planning(interaction, self.highlight.value, self.schedule.value)

class LifeLogConfirmTaskView(discord.ui.View):
    def __init__(self, cog, task_name: str, duration: int, original_message: discord.Message):
        super().__init__(timeout=60)
        self.cog = cog
        self.task_name = task_name
        self.duration = duration
        self.original_message = original_message
        self.bot_response_message: discord.Message = None

    @discord.ui.button(label="開始", style=discord.ButtonStyle.success)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return
        
        await interaction.response.defer()
        try:
            await interaction.delete_original_response() 
        except: pass
        
        await self.cog.switch_task(self.original_message, self.task_name, self.duration)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return
        
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except: pass
        self.stop()

    async def on_timeout(self):
        try:
            if self.bot_response_message:
                await self.bot_response_message.delete()
        except: pass
        await self.cog.switch_task(self.original_message, self.task_name, self.duration)

class LifeLogScheduleStartView(discord.ui.View):
    def __init__(self, cog, task_name, duration=30):
        super().__init__(timeout=300)
        self.cog = cog
        self.task_name = task_name
        self.duration = duration
        self.message: discord.Message = None

    @discord.ui.button(label="切り替えて開始", style=discord.ButtonStyle.success, emoji="▶️")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except: pass
        await self.cog.switch_task_from_interaction(interaction, self.task_name, self.duration)
        self.stop()

    @discord.ui.button(label="現在のタスクを継続", style=discord.ButtonStyle.secondary, emoji="👋")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except: pass
        self.stop()
    
    async def on_timeout(self):
        try:
            if self.message: await self.message.delete()
        except: pass

class LifeLogBookSelectView(discord.ui.View):
    def __init__(self, cog, book_options: list[discord.SelectOption], original_author: discord.User, duration: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.original_author = original_author
        self.duration = duration
        self.message = None
        
        select = discord.ui.Select(
            placeholder="読む書籍を選択してください...",
            options=book_options,
            custom_id="lifelog_book_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.original_author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return

        selected_book_name = interaction.data["values"][0]
        task_name = os.path.splitext(selected_book_name)[0]
        
        await interaction.response.defer()
        try:
             await interaction.delete_original_response()
        except: pass
        
        await self.cog.switch_task_from_interaction(interaction, task_name, self.duration)
        self.stop()

    async def on_timeout(self):
        try:
            if self.message: await self.message.delete()
        except: pass

class LifeLogPlanSelectView(discord.ui.View):
    def __init__(self, cog, task_options: list[discord.SelectOption], original_author: discord.User):
        super().__init__(timeout=60)
        self.cog = cog
        self.original_author = original_author
        self.message = None
        
        if not task_options:
            task_options = [discord.SelectOption(label="予定がありません", value="none", description="カレンダーに予定がありません")]

        select = discord.ui.Select(
            placeholder="開始するカレンダーの予定を選択...",
            options=task_options[:25],
            custom_id="lifelog_plan_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.original_author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return
        
        value = interaction.data["values"][0]
        if value == "none":
            await interaction.response.send_message("予定がありません。", ephemeral=True)
            return

        task_name = value
        duration = 30
        
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except: pass

        await self.cog.switch_task_from_interaction(interaction, task_name, duration)
        self.stop()

    async def on_timeout(self):
        try:
            if self.message: await self.message.delete()
        except: pass

class LifeLogTimeUpView(discord.ui.View):
    def __init__(self, cog, user_id: str, task_name: str, alert_message: discord.Message = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.user_id = user_id
        self.task_name = task_name
        self.alert_message = alert_message 

    async def _delete_alert(self):
        if self.alert_message:
            try: await self.alert_message.delete()
            except: pass

    @discord.ui.button(label="延長する (+30分)", style=discord.ButtonStyle.primary, emoji="🔄")
    async def extend_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("他のユーザーのタスクです。", ephemeral=True)
            return
        
        await interaction.response.defer()
        await self._delete_alert() 
        
        await self.cog.extend_task(interaction, minutes=30)
        await interaction.followup.send(f"✅ タスク「{self.task_name}」を30分延長しました。", ephemeral=True)
        self.stop()

    @discord.ui.button(label="延長する (+10分)", style=discord.ButtonStyle.secondary, emoji="⏱️")
    async def extend_short_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("他のユーザーのタスクです。", ephemeral=True)
            return
        
        await interaction.response.defer()
        await self._delete_alert()
        
        await self.cog.extend_task(interaction, minutes=10)
        await interaction.followup.send(f"✅ タスク「{self.task_name}」を10分延長しました。", ephemeral=True)
        self.stop()

    @discord.ui.button(label="終了する", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def finish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("他のユーザーのタスクです。", ephemeral=True)
            return
        
        await interaction.response.defer()
        await self._delete_alert()
        
        await self.cog.finish_current_task(interaction.user, interaction)
        await interaction.followup.send(f"✅ タスク「{self.task_name}」を終了しました。", ephemeral=True)
        self.stop()

class LifeLogTaskView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None) # Persistent
        self.cog = cog

    @discord.ui.button(label="終了", style=discord.ButtonStyle.danger, custom_id="lifelog_finish")
    async def finish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.finish_current_task(interaction.user, interaction, next_task_name=None)
    
    @discord.ui.button(label="メモ", style=discord.ButtonStyle.primary, custom_id="lifelog_memo")
    async def memo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.prompt_memo_modal(interaction)

class LifeLogPlanningView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None) # Persistent
        self.cog = cog

    @discord.ui.button(label="プランニング作成", style=discord.ButtonStyle.success, custom_id="lifelog_create_plan", emoji="📝")
    async def create_plan_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_planning_modal(interaction)


# ==========================================
# Cog Class
# ==========================================

class LifeLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lifelog_channel_id = int(os.getenv("LIFELOG_CHANNEL_ID", 0))
        self.owner_id = int(os.getenv("OWNER_ID", os.getenv("USER_ID", 0)))
        
        # Dropbox config
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        # Google Calendar config
        self.google_service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        
        self.notified_event_ids = set()
        self.monitor_tasks = {}
        self.scheduled_start_tasks = {}
        self.current_planning_time = DEFAULT_PLANNING_TIME 
        
        self.dbx = None
        
        # Calendar Status
        self.calendar_service = None
        self.calendar_status = "未初期化"
        self.calendar_error_detail = None

        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
                self.is_ready = True
            except Exception as e:
                logging.error(f"LifeLogCog: Dropbox Init Error: {e}")
                self.is_ready = False
        else:
            self.is_ready = False
            logging.warning("LifeLogCog: 環境変数が不足しています。")

        # Init Calendar
        self._init_calendar_service()

    def _init_calendar_service(self):
        """カレンダーサービスの初期化 (User Token優先, Service Accountフォールバック)"""
        self.calendar_status = "初期化試行中"
        creds = None
        method = "None"
        
        # 1. Try User OAuth (token.json)
        if os.path.exists('token.json'):
            try:
                creds = Credentials.from_authorized_user_file('token.json', ['https://www.googleapis.com/auth/calendar'])
                if not creds.valid:
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        method = "User OAuth (Refreshed)"
                    else:
                        creds = None
                        self.calendar_error_detail = "token.json expired and cannot refresh"
                else:
                    method = "User OAuth"
            except RefreshError as e:
                logging.error(f"LifeLogCog: Token refresh error: {e}")
                self.calendar_error_detail = f"token.json expired/revoked: {e}"
                try:
                    os.rename('token.json', 'token.json.bak')
                    logging.info("Renamed invalid token.json to token.json.bak")
                except: pass
                creds = None
            except Exception as e:
                logging.error(f"LifeLogCog: Token load error: {e}")
                self.calendar_error_detail = f"token.json load error: {e}"
                creds = None
        
        # 2. Try Service Account if User OAuth failed/missing
        if not creds and self.google_service_account_json:
            try:
                if os.path.exists(self.google_service_account_json):
                    creds = service_account.Credentials.from_service_account_file(
                        self.google_service_account_json, 
                        scopes=['https://www.googleapis.com/auth/calendar']
                    )
                else:
                    info = json.loads(self.google_service_account_json)
                    creds = service_account.Credentials.from_service_account_info(
                        info,
                        scopes=['https://www.googleapis.com/auth/calendar']
                    )
                method = "Service Account"
            except Exception as e:
                logging.error(f"LifeLogCog: Service Account load error: {e}")
                self.calendar_error_detail = f"Service Account load error: {e}"
        
        if creds:
            try:
                self.calendar_service = build('calendar', 'v3', credentials=creds)
                self.calendar_status = f"接続成功 ({method})"
                self.calendar_error_detail = None
                logging.info(f"LifeLogCog: Google Calendar Service Initialized via {method}.")
            except Exception as e:
                logging.error(f"LifeLogCog: Calendar Build Error: {e}")
                self.calendar_service = None
                self.calendar_status = "ビルド失敗"
                self.calendar_error_detail = str(e)
        else:
            if not self.calendar_error_detail:
                self.calendar_error_detail = "No valid credentials found (token.json or Service Account)"
            self.calendar_status = "認証情報なし"
            logging.warning("LifeLogCog: Google Calendar Credentials not found.")

    async def on_ready(self):
        self.bot.add_view(LifeLogTaskView(self))
        self.bot.add_view(LifeLogPlanningView(self))
        
        if self.is_ready:
            await self.bot.wait_until_ready()
            
            state = await self._get_planning_state()
            saved_time_str = state.get("planning_time")
            if saved_time_str:
                try:
                    h, m = map(int, saved_time_str.split(":"))
                    self.current_planning_time = time(hour=h, minute=m, tzinfo=JST)
                except: pass

            if not self.daily_lifelog_summary.is_running():
                self.daily_lifelog_summary.start()
            
            if not self.daily_planning_prompt.is_running():
                self.daily_planning_prompt.change_interval(time=self.current_planning_time)
                self.daily_planning_prompt.start()
            
            await self._resume_active_task_monitoring()
            await self._refresh_schedule()

    def cog_unload(self):
        self.daily_planning_prompt.cancel() 
        self.daily_lifelog_summary.cancel()
        for task in self.monitor_tasks.values(): task.cancel()
        for task in self.scheduled_start_tasks.values(): task.cancel()

    # --- 自前でのカレンダー取得 (JournalCog非依存) ---
    async def _fetch_todays_events(self):
        """今日のイベントを直接取得してパースする"""
        if not self.calendar_service: return []
        try:
            now = datetime.now(JST)
            # 今日の00:00:00から23:59:59まで
            start_iso = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            end_iso = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
            
            res = await asyncio.to_thread(
                self.calendar_service.events().list(
                    calendarId=self.calendar_id, 
                    timeMin=start_iso, 
                    timeMax=end_iso, 
                    singleEvents=True, 
                    orderBy='startTime'
                ).execute
            )
            items = res.get('items', [])
            parsed_events = []
            
            for event in items:
                start = event.get('start', {})
                end = event.get('end', {})
                summary = event.get('summary', '予定')
                event_id = event.get('id')
                
                # datetimeがある場合（時間指定）
                if 'dateTime' in start:
                    dt_start = datetime.fromisoformat(start['dateTime']).astimezone(JST)
                    dt_end = datetime.fromisoformat(end['dateTime']).astimezone(JST) if 'dateTime' in end else None
                    parsed_events.append({'id': event_id, 'summary': summary, 'start': dt_start, 'end': dt_end, 'all_day': False})
                
                # dateのみの場合（終日イベント）
                elif 'date' in start:
                    d_start = datetime.strptime(start['date'], '%Y-%m-%d').date()
                    dt_start = datetime.combine(d_start, time.min).replace(tzinfo=JST)
                    parsed_events.append({'id': event_id, 'summary': summary, 'start': dt_start, 'end': None, 'all_day': True})
                    
            return parsed_events
        except HttpError as e:
            logging.error(f"LifeLogCog: Google API HttpError: {e}")
            self.calendar_error_detail = f"API Error: {e.reason}"
            return []
        except Exception as e:
            logging.error(f"LifeLogCog: Fetch Events Error: {e}")
            self.calendar_error_detail = f"Fetch Error: {e}"
            return []

    async def _get_events_from_journal_cog(self):
        """互換性維持のためのラッパー（現在は内部メソッド使用）"""
        return await self._fetch_todays_events()

    # --- 定時通知機能 ---
    @tasks.loop(time=DEFAULT_PLANNING_TIME)
    async def daily_planning_prompt(self):
        if not self.is_ready: return
        channel = self.bot.get_channel(self.lifelog_channel_id)
        if channel:
            embed = discord.Embed(
                title="☀️ Good Morning", 
                description="新しい1日が始まります。今日の計画を立てましょう。\n下の「📝 プランニング作成」ボタンを押してスタートしてください。", 
                color=discord.Color.orange()
            )
            await channel.send(embed=embed, view=LifeLogPlanningView(self))
        await self._refresh_schedule()

    @app_commands.command(name="set_plan_time")
    async def set_planning_time_command(self, ctx, time_str: str):
        if not re.match(r'^\d{1,2}:\d{2}$', time_str):
            await ctx.reply("⚠️ `HH:MM` 形式で入力してください (例: `08:00`)")
            return
        try:
            h, m = map(int, time_str.split(":"))
            new_time = time(hour=h, minute=m, tzinfo=JST)
            state = await self._get_planning_state()
            state["planning_time"] = time_str
            await self._save_planning_state(state)
            self.current_planning_time = new_time
            self.daily_planning_prompt.change_interval(time=new_time)
            if self.daily_planning_prompt.is_running(): self.daily_planning_prompt.restart()
            else: self.daily_planning_prompt.start()
            await ctx.reply(f"✅ プランニング通知時刻を **{time_str}** に変更しました。")
        except Exception as e:
            await ctx.reply(f"⚠️ エラー: {e}")

    # --- Debug Command ---
    @app_commands.command(name="test_calendar", description="Googleカレンダーとの接続テストを行います。")
    async def test_calendar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        status = self.calendar_status
        detail = self.calendar_error_detail or "なし"
        cal_id = self.calendar_id
        
        if not self.calendar_service:
            msg = f"❌ カレンダーサービスは初期化されていません。\nStatus: {status}\nDetail: {detail}"
            # 解決策の提示
            if "expired" in str(detail) or "revoked" in str(detail):
                msg += "\n\n💡 **解決策**: `token.json` の有効期限が切れています。ファイルを削除し、`generate_token.py` を再実行して再認証してください。"
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            events = await self._fetch_todays_events()
            count = len(events)
            
            warning = ""
            if "gserviceaccount.com" in cal_id:
                warning = "\n⚠️ **警告**: `GOOGLE_CALENDAR_ID` がサービスアカウントのアドレスになっています。通常、これは空のカレンダーです。個人のGmailアドレス等を指定してください。"

            await interaction.followup.send(
                f"✅ **接続成功**\n"
                f"Status: {status}\n"
                f"Calendar ID: `{cal_id}`\n"
                f"今日の予定取得数: {count}件\n"
                f"{warning}",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ **接続失敗 (API Error)**\n"
                f"Status: {status}\n"
                f"Calendar ID: `{cal_id}`\n"
                f"Error: {e}",
                ephemeral=True
            )

    # --- 状態管理 ---
    async def _get_planning_state(self) -> dict:
        if not self.dbx: return {}
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, PLANNING_STATE_PATH)
            return json.loads(res.content.decode('utf-8'))
        except (ApiError, Exception):
            return {}

    async def _save_planning_state(self, data: dict):
        if not self.dbx: return
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
            await asyncio.to_thread(self.dbx.files_upload, content, PLANNING_STATE_PATH, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"LifeLogCog: プランニング状態保存エラー: {e}")

    # --- プランニング機能 (Modal & Calendar) ---
    async def open_planning_modal(self, interaction: discord.Interaction):
        events = await self._fetch_todays_events()
        
        # 1. 取得したイベントを整理 (マップ化)
        schedule_map = {} # "HH:MM" -> list[summary]
        for ev in events:
            if ev.get('all_day'): continue # 終日はスキップ
            t_str = ev['start'].strftime('%H:%M')
            if t_str not in schedule_map: schedule_map[t_str] = []
            schedule_map[t_str].append(ev['summary'])
        
        # 2. 6:00 - 23:30 の枠を作成し、イベントがあれば埋める
        default_schedule_lines = []
        
        now = datetime.now(JST)
        current = now.replace(hour=6, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=30, second=0, microsecond=0)
        
        # 30分刻みの枠と、それ以外の時刻にあるイベントをマージしてソート
        all_times = set()
        
        # 定形枠を追加
        temp_curr = current
        while temp_curr <= end:
            all_times.add(temp_curr.strftime('%H:%M'))
            temp_curr += timedelta(minutes=30)
            
        # イベントがある時刻も追加 (例: 10:15)
        for t_str in schedule_map.keys():
            all_times.add(t_str)
            
        # ソート
        sorted_times = sorted(list(all_times))
        
        # 行生成
        for t_str in sorted_times:
            if t_str in schedule_map:
                for summary in schedule_map[t_str]:
                    default_schedule_lines.append(f"{t_str} {summary}")
            else:
                default_schedule_lines.append(f"{t_str} ")

        default_schedule = "\n".join(default_schedule_lines)

        if len(default_schedule) > 2800:
            default_schedule = default_schedule[:2800] + "\n..."

        await interaction.response.send_modal(LifeLogPlanningModal(self, default_schedule=default_schedule))

    async def submit_planning(self, interaction, highlight, schedule_text):
        today_date = datetime.now(JST).date()
        cal_error = None
        
        # 1. ハイライト（終日イベント）の登録
        if self.calendar_service and highlight:
            try:
                # 終日イベント: endは翌日
                next_day = today_date + timedelta(days=1)
                self._add_calendar_event(
                    summary=f"★{highlight}",
                    is_all_day=True,
                    date_obj=today_date,
                    end_date_obj=next_day,
                    color_id="11" 
                )
            except Exception as e:
                cal_error = str(e)

        plan_content = ""
        if highlight:
            plan_content += f"### Highlight\n- {highlight}\n\n"
        
        plan_content += "### Schedule\n"
        
        existing_entries = set()
        if self.calendar_service:
            try:
                existing_events = await self._fetch_todays_events()
                for e in existing_events:
                    if e.get('start') and not e.get('all_day'):
                        t_str = e['start'].strftime('%H:%M')
                        s_val = e['summary']
                        existing_entries.add((t_str, s_val))
            except: pass

        line_regex = re.compile(r'^\s*(\d{1,2})[:：](\d{2})\s*(.*)$')

        for line in schedule_text.split('\n'):
            line = line.strip()
            if not line: continue
            
            match = line_regex.match(line)
            if match:
                hour = match.group(1)
                minute = match.group(2)
                content = match.group(3).strip() if match.group(3) else ""
                time_str = f"{int(hour):02d}:{int(minute):02d}"
                
                if content:
                    plan_content += f"- {time_str} {content}\n"
                    
                    if self.calendar_service:
                        if (time_str, content) not in existing_entries:
                            try:
                                start_dt = datetime.strptime(time_str, '%H:%M').replace(
                                    year=today_date.year, month=today_date.month, day=today_date.day, tzinfo=JST
                                )
                                end_dt = start_dt + timedelta(minutes=30)
                                self._add_calendar_event(content, start_dt=start_dt, end_dt=end_dt)
                                existing_entries.add((time_str, content))
                            except Exception as e:
                                cal_error = str(e)
                else:
                    plan_content += f"- {time_str}\n"
            else:
                plan_content += f"- {line}\n"

        await self._save_to_obsidian_planning(plan_content)

        state = await self._get_planning_state()
        last_result_msg_id = state.get("last_plan_result_msg_id")
        if last_result_msg_id:
            try:
                old_res_msg = await interaction.channel.fetch_message(last_result_msg_id)
                await old_res_msg.delete()
            except: pass

        description = "Obsidianに計画を保存し、カレンダーを更新しました。"
        if cal_error:
            description = f"Obsidianには保存しましたが、カレンダー更新中にエラーが発生しました。\nError: {cal_error}"
        elif not self.calendar_service:
            description = "Obsidianに計画を保存しました。(カレンダー連携は無効です)"

        embed = discord.Embed(title="📅 プランニング完了", description=description, color=discord.Color.blue())
        if highlight: embed.add_field(name="★Highlight", value=highlight, inline=False)
        
        msg = await interaction.followup.send(embed=embed)
        state["last_plan_result_msg_id"] = msg.id
        await self._save_planning_state(state)
        
        await self._refresh_schedule()
        
        await asyncio.sleep(5)
        try: await msg.delete()
        except: pass

    async def _save_to_obsidian_planning(self, plan_content):
        if not self.dbx: return
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError:
                current_content = f"# Daily Note {date_str}\n"

            new_content = self._update_section_content(current_content, plan_content, PLANNING_HEADER)
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"Obsidian Planning Save Error: {e}")

    async def prompt_plan_selection(self, interaction: discord.Interaction):
        events = await self._fetch_todays_events()
        
        options = []
        if events:
            now = datetime.now(JST)
            upcoming = [ev for ev in events if ev['end'] is None or ev['end'] > now]
            for ev in upcoming[:25]:
                time_str = ev['start'].strftime('%H:%M')
                label = f"{time_str} {ev['summary']}"
                options.append(discord.SelectOption(label=label[:100], value=ev['summary'][:100]))
        
        view = LifeLogPlanSelectView(self, options, interaction.user)
        msg = await interaction.followup.send("開始するカレンダーの予定を選択してください:", view=view, ephemeral=True)
        view.message = msg

    # --- カレンダー書き込みヘルパー ---
    def _add_calendar_event(self, summary, start_dt=None, end_dt=None, is_all_day=False, date_obj=None, end_date_obj=None, color_id=None):
        if not self.calendar_service: return
        event_body = {'summary': summary, 'description': 'Created via Discord LifeLog'}
        if color_id: event_body['colorId'] = color_id
        if is_all_day and date_obj:
            date_str = date_obj.strftime('%Y-%m-%d')
            end_str = (end_date_obj or (date_obj + timedelta(days=1))).strftime('%Y-%m-%d')
            event_body['start'] = {'date': date_str}
            event_body['end'] = {'date': end_str}
        elif start_dt and end_dt:
            event_body['start'] = {'dateTime': start_dt.isoformat()}
            event_body['end'] = {'dateTime': end_dt.isoformat()}
        else: return
        try: self.calendar_service.events().insert(calendarId=self.calendar_id, body=event_body).execute()
        except Exception as e:
            logging.error(f"Calendar Insert Error: {e}")
            raise e

    # --- チャット監視＆切り替え ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.lifelog_channel_id: return
        content = message.content.strip()
        if not content: return

        if content.lower().startswith("m ") or content.startswith("ｍ "):
            memo_text = content[2:].strip()
            await self._add_memo_from_message(message, memo_text)
            return
        
        task_name, duration = self._parse_task_and_duration(content)
        if task_name.startswith("読書") or task_name == "読書":
            await self.prompt_book_selection(message, duration)
            return
        
        view = LifeLogConfirmTaskView(self, task_name, duration, message)
        bot_reply = await message.reply(f"タスク「**{task_name}**」として計測を開始しますか？（予定: {duration}分 / 60秒後に自動開始）", view=view)
        view.bot_response_message = bot_reply

    async def prompt_book_selection(self, message: discord.Message, duration: int):
        book_cog = self.bot.get_cog("BookCog")
        if not book_cog:
            await message.reply("⚠️ BookCogが見つからないため、書籍リストを取得できません。「読書」タスクとして開始します。")
            view = LifeLogConfirmTaskView(self, "読書", duration, message)
            bot_reply = await message.reply(f"タスク「**読書**」として計測を開始しますか？（予定: {duration}分 / 60秒後に自動開始）", view=view)
            view.bot_response_message = bot_reply
            return
        book_files, error = await book_cog.get_book_list()
        if error or not book_files:
            await message.reply(f"⚠️ 書籍リストの取得に失敗したか、書籍がありません ({error})。「読書」タスクとして開始します。")
            view = LifeLogConfirmTaskView(self, "読書", duration, message)
            bot_reply = await message.reply(f"タスク「**読書**」として計測を開始しますか？（予定: {duration}分 / 60秒後に自動開始）", view=view)
            view.bot_response_message = bot_reply
            return
        options = []
        for entry in book_files[:25]:
            file_name = os.path.basename(entry.path_display)
            label = os.path.splitext(file_name)[0][:100]
            options.append(discord.SelectOption(label=label, value=file_name))
        view = LifeLogBookSelectView(self, options, message.author, duration)
        msg = await message.reply(f"読む書籍を選択してください（予定: {duration}分）:", view=view)
        view.message = msg

    async def switch_task_from_interaction(self, interaction: discord.Interaction, new_task_name: str, duration: int):
        user = interaction.user
        prev_task_log = await self.finish_current_task(user, interaction, next_task_name=new_task_name)
        await self.start_new_task_context(interaction.channel, user, new_task_name, duration, prev_task_log)

    async def switch_task(self, message: discord.Message, new_task_name: str, duration: int):
        user = message.author
        prev_task_log = await self.finish_current_task(user, message, next_task_name=new_task_name)
        await self.start_new_task_context(message.channel, user, new_task_name, duration, prev_task_log)

    async def start_new_task_context(self, channel, user: discord.User, task_name: str, duration: int, prev_task_log: str = None):
        user_id = str(user.id)
        now = datetime.now(JST)
        start_time_str = now.strftime('%H:%M')
        end_time_plan = now + timedelta(minutes=duration)
        end_time_str = end_time_plan.strftime('%H:%M')
        embed = discord.Embed(color=discord.Color.green())
        if prev_task_log:
            try:
                prev_log_text = prev_task_log.split("(", 1)[0].strip()
                duration_text = prev_task_log.split("(", 1)[1].split(")", 1)[0]
                task_text = prev_task_log.split(")", 1)[1].strip()
                prev_task_display = f"{prev_log_text} ({duration_text}) {task_text}"
            except: prev_task_display = prev_task_log
            embed.description = f"✅ **前回の記録:** `{prev_task_display}`\n⬇️\n⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ {end_time_str} 予定: {duration}分)"
        else:
            embed.description = f"⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ {end_time_str} 予定: {duration}分)"
        embed.set_footer(text="メモ入力ボタンで詳細を記録できます。")
        
        reply_msg = await channel.send(f"{user.mention}", embed=embed, view=LifeLogTaskView(self))
        
        active_logs = await self._get_active_logs()
        active_logs[user_id] = {
            "task": task_name,
            "start_time": now.isoformat(),
            "planned_duration": duration,
            "message_id": reply_msg.id,
            "channel_id": reply_msg.channel.id,
            "memos": []
        }
        await self._save_active_logs(active_logs)
        self._start_monitor_task(user_id, task_name, reply_msg.channel.id, end_time_plan)

    async def finish_current_task(self, user: discord.User | discord.Object, context, next_task_name: str = None, end_time: datetime = None) -> str:
        user_id = str(user.id)
        if user_id in self.monitor_tasks:
            task = self.monitor_tasks[user_id]
            current_task = asyncio.current_task()
            if task != current_task: task.cancel()
            del self.monitor_tasks[user_id]
        active_logs = await self._get_active_logs()
        if user_id not in active_logs:
            if isinstance(context, discord.Interaction):
                if not context.response.is_done(): await context.response.send_message("⚠️ 進行中のタスクはありません。", ephemeral=True)
                else: await context.followup.send("⚠️ 進行中のタスクはありません。", ephemeral=True)
            return None
        log_data = active_logs.pop(user_id)
        await self._save_active_logs(active_logs)
        start_time = datetime.fromisoformat(log_data['start_time'])
        if end_time is None: end_time = datetime.now(JST)
        duration = end_time - start_time
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        duration_str = (f"{hours}h" if hours > 0 else "") + f"{minutes}m"
        if total_seconds < 60: duration_str = "0m"
        date_str = start_time.strftime('%Y-%m-%d')
        start_hm = start_time.strftime('%H:%M')
        end_hm = end_time.strftime('%H:%M')
        task_name = log_data['task']
        memos = log_data.get('memos', [])
        obsidian_line = f"- {start_hm} - {end_hm} ({duration_str}) **{task_name}**"
        formatted_memos = []
        if memos:
            for m in memos:
                lines = m.strip().split('\n')
                if lines: formatted_memos.append(f"\t- {lines[0]}")
                for line in lines[1:]:
                    if line.strip(): formatted_memos.append(f"\t- {line.strip()}")
            if formatted_memos: obsidian_line += "\n" + "\n".join(formatted_memos)
        await self._save_to_obsidian(date_str, obsidian_line)
        if self.dbx:
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", task_name)
            book_path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}/{safe_title}.md"
            try:
                self.dbx.files_get_metadata(book_path)
                _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
                book_content = res.content.decode('utf-8')
                book_log_line = f"- {date_str} {start_hm} - {end_hm} ({duration_str}) 読書ログ"
                if formatted_memos: book_log_line += "\n" + "\n".join(formatted_memos)
                new_book_content = self._update_section_content(book_content, book_log_line, "## Notes")
                await asyncio.to_thread(self.dbx.files_upload, new_book_content.encode('utf-8'), book_path, mode=WriteMode('overwrite'))
                if isinstance(context, discord.Interaction) and not next_task_name:
                    if not context.response.is_done(): await context.response.send_message(f"📖 読書ノート `{task_name}` にも記録しました。", ephemeral=True)
                    else: await context.followup.send(f"📖 読書ノート `{task_name}` にも記録しました。", ephemeral=True)
            except ApiError: pass 
            except Exception as e: logging.error(f"LifeLogCog: 読書ノート連携中にエラー: {e}", exc_info=True)
        try:
            channel = self.bot.get_channel(log_data['channel_id'])
            if channel:
                old_msg = await channel.fetch_message(log_data['message_id'])
                embed = old_msg.embeds[0]
                embed.color = discord.Color.dark_grey() 
                embed.description = f"✅ **完了:** {task_name} ({start_hm} - {end_hm}, {duration_str})"
                await old_msg.edit(embed=embed, view=None)
        except Exception: pass
        if isinstance(context, discord.Interaction) and not next_task_name:
            embed = discord.Embed(title="✅ タスク完了", color=discord.Color.light_grey())
            embed.add_field(name="Task", value=task_name, inline=True)
            embed.add_field(name="Duration", value=duration_str, inline=True)
            embed.set_footer(text=f"{start_hm} - {end_hm}")
            if not context.response.is_done(): await context.response.send_message(embed=embed, ephemeral=True)
            else: await context.followup.send(embed=embed, ephemeral=True)
        return obsidian_line

    async def _save_to_obsidian(self, date_str: str, line_to_add: str) -> bool:
        if not self.dbx: return False
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            current_content = ""
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): current_content = ""
                else: raise
            
            new_content = self._update_section_content(current_content, line_to_add, DAILY_NOTE_HEADER)
            
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            return True
        except Exception as e:
            logging.error(f"LifeLogCog: Obsidian保存エラー: {e}", exc_info=True)
            return False

    def _update_section_content(self, content: str, text: str, header: str) -> str:
        pattern = re.escape(header)
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            post_header = content[match.end():]
            next_header_match = re.search(r'\n##\s+', post_header)
            if next_header_match:
                insertion_point = match.end() + next_header_match.start()
                return content[:insertion_point] + f"\n{text}" + content[insertion_point:]
            else:
                return content.rstrip() + f"\n{text}\n"
        else:
            return content.rstrip() + f"\n\n{header}\n{text}\n"

    async def extend_task(self, interaction: discord.Interaction, minutes: int = 30):
        user_id = str(interaction.user.id)
        active_logs = await self._get_active_logs()
        if user_id in active_logs:
            active_logs[user_id]['planned_duration'] += minutes
            await self._save_active_logs(active_logs)
            task_name = active_logs[user_id]['task']
            channel_id = active_logs[user_id]['channel_id']
            start_time = datetime.fromisoformat(active_logs[user_id]['start_time'])
            new_duration = active_logs[user_id]['planned_duration']
            new_end_time = start_time + timedelta(minutes=new_duration)
            self._start_monitor_task(user_id, task_name, channel_id, new_end_time)
        else: await interaction.followup.send("延長する進行中のタスクが見つかりませんでした。", ephemeral=True)

    async def _resume_active_task_monitoring(self):
        active_logs = await self._get_active_logs()
        for user_id, log in active_logs.items():
            try:
                start_time = datetime.fromisoformat(log['start_time'])
                duration_minutes = log.get('planned_duration', 30)
                end_time = start_time + timedelta(minutes=duration_minutes)
                self._start_monitor_task(user_id, log['task'], log['channel_id'], end_time)
            except Exception as e: logging.error(f"LifeLogCog: 監視再開エラー User:{user_id}: {e}")

    def _start_monitor_task(self, user_id, task_name, channel_id, end_time):
        if user_id in self.monitor_tasks: self.monitor_tasks[user_id].cancel()
        self.monitor_tasks[user_id] = self.bot.loop.create_task(self._monitor_logic(user_id, task_name, channel_id, end_time))

    async def _monitor_logic(self, user_id, task_name, channel_id, end_time):
        try:
            now = datetime.now(JST)
            wait_seconds = (end_time - now).total_seconds()
            if wait_seconds > 0: await asyncio.sleep(wait_seconds)
            active_logs = await self._get_active_logs()
            if user_id not in active_logs or active_logs[user_id]['task'] != task_name: return
            alert_msg = None
            channel = self.bot.get_channel(channel_id)
            if channel:
                user = self.bot.get_user(int(user_id))
                if not user:
                    try: user = await self.bot.fetch_user(int(user_id))
                    except: pass
                mention = user.mention if user else f"User {user_id}"
                view = LifeLogTimeUpView(self, user_id, task_name)
                alert_msg = await channel.send(f"{mention} ⏰ タスク「**{task_name}**」の予定時間が経過しました。\n延長しますか？それとも終了しますか？（反応がない場合、5分後に自動終了します）", view=view)
                view.alert_message = alert_msg 
            await asyncio.sleep(300) 
            if alert_msg:
                try: await alert_msg.delete()
                except: pass
            active_logs = await self._get_active_logs()
            if user_id in active_logs and active_logs[user_id]['task'] == task_name:
                user_obj = discord.Object(id=int(user_id))
                await self.finish_current_task(user_obj, context=None, end_time=datetime.now(JST))
                if channel: await channel.send(f"🛑 応答がなかったため、タスク「{task_name}」を自動終了しました。")
        except asyncio.CancelledError: pass
        except Exception as e: logging.error(f"LifeLogCog: Monitor logic error for {user_id}: {e}", exc_info=True)
        finally:
            current = asyncio.current_task()
            if user_id in self.monitor_tasks and self.monitor_tasks[user_id] == current: del self.monitor_tasks[user_id]

    async def _refresh_schedule(self):
        for task in self.scheduled_start_tasks.values(): task.cancel()
        self.scheduled_start_tasks = {}
        events = await self._fetch_todays_events()
        now = datetime.now(JST)
        for event in events:
            start_dt = event.get('start')
            if not start_dt: continue
            if start_dt <= now: continue
            event_id = event.get('id', str(start_dt))
            wait_seconds = (start_dt - now).total_seconds()
            task = self.bot.loop.create_task(self._wait_and_trigger_schedule_start(event, wait_seconds))
            self.scheduled_start_tasks[event_id] = task
        logging.info(f"LifeLogCog: {len(self.scheduled_start_tasks)} 件の予定通知を予約しました。")

    async def _wait_and_trigger_schedule_start(self, event, wait_seconds):
        try:
            await asyncio.sleep(wait_seconds)
            event_id = event.get('id')
            summary = event.get('summary', '予定')
            start_dt = event.get('start')
            end_dt = event.get('end')
            duration = 30
            if start_dt and end_dt: duration = int((end_dt - start_dt).total_seconds() / 60)
            channel = self.bot.get_channel(self.lifelog_channel_id)
            if not channel: return
            active_logs = await self._get_active_logs()
            target_user_id = self.owner_id
            if active_logs: target_user_id = int(list(active_logs.keys())[0])
            is_active = str(target_user_id) in active_logs
            target_user = self.bot.get_user(target_user_id)
            if not target_user and target_user_id:
                try: target_user = await self.bot.fetch_user(target_user_id)
                except: pass
            if not is_active and target_user:
                await self.start_new_task_context(channel, target_user, summary, duration)
                await channel.send(f"🤖 **自動開始**: 予定「{summary}」の時間になったため、タスクを開始しました。")
            else:
                view = LifeLogScheduleStartView(self, summary, duration)
                msg = await channel.send(f"⏰ **予定の時間です**: {summary}\nこのタスクに切り替えますか？（予定: {duration}分）", view=view)
                view.message = msg
        except asyncio.CancelledError: pass
        except Exception as e: logging.error(f"LifeLogCog: Scheduled start error: {e}")
        finally:
            event_id = event.get('id')
            if event_id in self.scheduled_start_tasks: del self.scheduled_start_tasks[event_id]

    def _parse_task_and_duration(self, content: str) -> tuple[str, int]:
        match = DURATION_REGEX.search(content)
        if match:
            duration_str = match.group(1)
            unit = match.group(2)
            try:
                value = float(duration_str)
                if unit and unit.lower() in ['h', 'hr', 'hour', '時間']: minutes = int(value * 60)
                else: minutes = int(value)
                task_name = content[:match.start()].strip()
                return task_name, minutes
            except ValueError: return content, 30
        return content, 30

    @tasks.loop(time=DAILY_SUMMARY_TIME)
    async def daily_lifelog_summary(self):
        # 簡易実装: 必要であれば中身を記述
        pass

async def setup(bot: commands.Bot):
    if int(os.getenv("LIFELOG_CHANNEL_ID", 0)) == 0:
        logging.error("LifeLogCog: LIFELOG_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    await bot.add_cog(LifeLogCog(bot))