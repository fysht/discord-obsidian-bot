import os
import json
import logging
import asyncio
from datetime import datetime, time, timedelta, timezone
import re
from typing import Optional
import jpholiday

import discord
from discord.ext import commands, tasks
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode, DownloadError

from utils.obsidian_utils import update_section

JST = timezone(timedelta(hours=+9), 'JST')
# --- 時間定義の更新 ---
DAILY_PLANNING_TIME = time(hour=6, minute=0, tzinfo=JST) # 朝の計画タスク
TODAY_SCHEDULE_TIME = time(hour=7, minute=0, tzinfo=JST)
DAILY_REVIEW_TIME = time(hour=21, minute=30, tzinfo=JST)
MEMO_TO_CALENDAR_EMOJI = '📅'

SCOPES = ['https://www.googleapis.com/auth/calendar']
WORK_START_HOUR = 6
WORK_END_HOUR = 23
MIN_TASK_DURATION_MINUTES = 10
HIGHLIGHT_COLOR_ID = '4' # Google Calendar APIの色ID (Flamingo)

# --- 新しいUIコンポーネント ---

class ScheduleEditModal(discord.ui.Modal, title="スケジュールを手動で修正"):
    def __init__(self, cog, tasks: list):
        super().__init__(timeout=None)
        self.cog = cog
        self.tasks = tasks
        for i, task in enumerate(tasks[:5]): # UIのコンポーネント上限は5つのため
            self.add_item(discord.ui.TextInput(
                label=f"タスク {i+1}: {task['summary']}",
                default=task['start_time'],
                custom_id=f"task_{i}"
            ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        updated_tasks = []
        for i, task in enumerate(self.tasks[:5]):
            new_time = self.children[i].value
            # 時間形式のバリデーション (簡易)
            if re.match(r'^\d{2}:\d{2}$', new_time):
                updated_tasks.append({"summary": task['summary'], "start_time": new_time})
            else:
                await interaction.followup.send(f"⚠️ タスク「{task['summary']}」の時刻形式が不正です (`HH:MM`)。このタスクは除外されました。", ephemeral=True)
        
        if updated_tasks:
            await self.cog.confirm_and_register_schedule(interaction, updated_tasks)

class ScheduleConfirmationView(discord.ui.View):
    def __init__(self, cog, tasks: list):
        super().__init__(timeout=1800) # 30分でタイムアウト
        self.cog = cog
        self.tasks = tasks

    @discord.ui.button(label="この内容でカレンダーに登録", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.confirm_and_register_schedule(interaction, self.tasks)
        self.stop()

    @discord.ui.button(label="時間を手動で修正", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScheduleEditModal(self.cog, self.tasks))
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="スケジュールの登録をキャンセルしました。", view=None, embed=None)
        self.stop()

class HighlightChoiceView(discord.ui.View):
    def __init__(self, cog, scheduled_tasks: list):
        super().__init__(timeout=1800)
        self.cog = cog
        
        options = [discord.SelectOption(label=task[:100], value=task) for task in scheduled_tasks[:24]]
        options.append(discord.SelectOption(label="✨ 別のハイライトを自分で設定する", value="custom_highlight"))
        
        select = discord.ui.Select(placeholder="今日一日のハイライトを選択してください...", options=options, custom_id="highlight_select")
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        selected = interaction.data["values"][0]
        if selected == "custom_highlight":
            await interaction.response.send_modal(HighlightCustomModal(self.cog))
        else:
            await interaction.response.defer()
            await self.cog.create_highlight_event(interaction, selected)
        
        await interaction.message.edit(content=f"ハイライトが設定されました: **{selected}**", view=None)
        self.stop()

class HighlightCustomModal(discord.ui.Modal, title="ハイライトを自由入力"):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
    
    custom_highlight = discord.ui.TextInput(label="今日のハイライト", placeholder="今日最も集中したいこと、達成したいことは何ですか？")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.create_highlight_event(interaction, self.custom_highlight.value)


class TaskReviewView(discord.ui.View):
    def __init__(self, cog, task_summary: str, task_date: datetime.date):
        super().__init__(timeout=86400) 
        self.cog = cog
        self.task_summary = task_summary
        self.task_date = task_date
        self.is_processed = False

    async def handle_interaction(self, interaction: discord.Interaction, status: str, log_marker: str, feedback: str):
        if self.is_processed:
            await interaction.response.send_message("このタスクは既に対応済みです。", ephemeral=True, delete_after=10)
            return
        
        await interaction.response.defer()
        self.is_processed = True

        if status == "uncompleted":
            self.cog.uncompleted_tasks[self.task_summary] = self.task_date

        task_log_md = f"- [{log_marker}] {self.task_summary}\n"
        await self.cog._update_obsidian_task_log(self.task_date, task_log_md)
        
        await interaction.message.delete()
        
        feedback_msg = await interaction.channel.send(f"{interaction.user.mention}さん、「{self.task_summary}」を**{feedback}**として記録しました。")
        await asyncio.sleep(10)
        await feedback_msg.delete()

        self.stop()

    @discord.ui.button(label="完了", style=discord.ButtonStyle.success, emoji="✅")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_interaction(interaction, "completed", "x", "完了")

    @discord.ui.button(label="未完了 (繰越)", style=discord.ButtonStyle.danger, emoji="❌")
    async def uncompleted(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_interaction(interaction, "uncompleted", " ", "未完了（翌日に繰越）")

    @discord.ui.button(label="破棄", style=discord.ButtonStyle.secondary, emoji="🗑️")
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_interaction(interaction, "discarded", "-", "破棄")

class CalendarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.uncompleted_tasks = {}
        self.pending_schedules = {}
        self.pending_date_prompts = {}
        self.last_schedule_message_id = None
        self.daily_planning_message_id = None # 朝の計画用メッセージID

        if not self._are_credentials_valid():
            logging.error("CalendarCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return
        try:
            self.creds = self._get_google_credentials()
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    try:
                        self.creds.refresh(Request())
                        self._save_google_credentials(self.creds)
                        logging.info("Google APIのアクセストークンをリフレッシュしました。")
                    except RefreshError as e:
                        logging.error(f"❌ Google APIトークンのリフレッシュに失敗: {e}")
                        return
                else:
                    logging.error("❌ Google Calendarの有効な認証情報(token.json)が見つかりません。")
                    return

            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.dbx = self._initialize_dropbox_client()
            self.is_ready = True
            logging.info("✅ CalendarCogが正常に初期化され、準備が完了しました。")
        except Exception as e:
            logging.error(f"❌ CalendarCogの初期化中に予期せぬエラーが発生しました: {e}", exc_info=True)

    def _load_environment_variables(self):
        self.calendar_channel_id = int(os.getenv("CALENDAR_CHANNEL_ID", 0))
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH")
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
        token_path = self.google_token_path
        if os.getenv("RENDER"):
             token_path = f"/etc/secrets/{os.path.basename(token_path)}"
        if os.path.exists(token_path):
            try:
                return Credentials.from_authorized_user_file(token_path, SCOPES)
            except Exception as e:
                logging.error(f"認証ファイル({token_path})からの認証情報読み込みに失敗しました: {e}")
        return None
    
    def _save_google_credentials(self, creds):
        if not os.getenv("RENDER"):
            try:
                with open(self.google_token_path, 'w') as token:
                    token.write(creds.to_json())
                logging.info(f"更新されたGoogle認証情報を {self.google_token_path} に保存しました。")
            except Exception as e:
                logging.error(f"Google認証情報の保存に失敗しました: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.daily_planning_task.is_running(): self.daily_planning_task.start()
            if not self.notify_today_events.is_running(): self.notify_today_events.start()
            if not self.send_daily_review.is_running(): self.send_daily_review.start()
            if not self.check_weekend_gaps.is_running(): self.check_weekend_gaps.start()
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.calendar_channel_id:
            return
        
        # 朝の計画タスクへの返信かチェック
        if message.reference and message.reference.message_id == self.daily_planning_message_id:
            self.daily_planning_message_id = None # 一度処理したらIDをクリア
            await message.add_reaction("🤔")
            await self.handle_daily_plan_submission(message)


    def cog_unload(self):
        if self.is_ready:
            self.daily_planning_task.cancel()
            self.notify_today_events.cancel()
            self.send_daily_review.cancel()
            self.check_weekend_gaps.cancel()

    # --- 新機能: 朝の計画立案 ---
    @tasks.loop(time=DAILY_PLANNING_TIME)
    async def daily_planning_task(self):
        channel = self.bot.get_channel(self.calendar_channel_id)
        if not channel: return
        
        embed = discord.Embed(
            title="🌞 おはようございます！一日の計画を立てましょう",
            description="今日やるべきこと、やりたいことをリストアップして、このメッセージに返信してください。\nAIがスケジュール案を作成します。",
            color=discord.Color.gold()
        )
        msg = await channel.send(embed=embed)
        self.daily_planning_message_id = msg.id

    async def handle_daily_plan_submission(self, message: discord.Message):
        """ユーザーから提出された計画を処理する"""
        user_tasks_text = message.content
        today = datetime.now(JST).date()
        
        try:
            free_slots = await self._find_free_slots(today)
            
            # AIにスケジュール案を作成させる
            scheduled_tasks = await self._generate_ai_schedule(user_tasks_text, free_slots, today)

            if not scheduled_tasks:
                await message.reply("タスクをうまく解析できませんでした。もう一度試してみてください。")
                await message.remove_reaction("🤔", self.bot.user)
                return

            # 確認UIを提示
            embed = discord.Embed(
                title="🗓️ スケジュール案",
                description="AIが作成した本日のスケジュール案です。内容を確認してください。",
                color=discord.Color.purple()
            )
            for task in scheduled_tasks:
                embed.add_field(name=task['summary'], value=f"開始時刻: {task['start_time']}", inline=False)

            view = ScheduleConfirmationView(self, scheduled_tasks)
            await message.reply(embed=embed, view=view)
            await message.remove_reaction("🤔", self.bot.user)

        except Exception as e:
            logging.error(f"計画の処理中にエラー: {e}", exc_info=True)
            await message.reply(f"エラーが発生しました: {e}")
            await message.remove_reaction("🤔", self.bot.user)


    async def _generate_ai_schedule(self, tasks_text: str, free_slots: list, target_date: datetime.date) -> list:
        """AIを使ってタスクリストからスケジュールを生成する"""
        prompt = f"""
        あなたは優秀なアシスタントです。以下の「ユーザーのタスクリスト」と「空き時間」を元に、最適な一日のスケジュールを作成してください。

        # 指示
        1. ユーザーのタスクリストを個別のタスクに分解してください。
        2. 各タスクの所要時間（分単位）を常識的な範囲で予測してください。
        3. 既存の予定（空き時間以外の時間）を考慮し、各タスクを空き時間に割り当ててください。タスクは午前中や理性が働く早い時間帯に重いものを配置するのが望ましいです。
        4. 出力は以下のJSON形式のリストのみとし、説明や前置きは一切含めないでください。

        # 空き時間 (ISO 8601形式)
        {json.dumps([{"start": s.isoformat(), "end": e.isoformat()} for s, e in free_slots])}

        # ユーザーのタスクリスト
        {tasks_text}

        # 出力形式
        [
          {{"summary": "タスク1の名称", "start_time": "HH:MM"}},
          {{"summary": "タスク2の名称", "start_time": "HH:MM"}}
        ]
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            json_match = re.search(r'```json\n(\{.*?\})\n```', response.text, re.DOTALL)
            json_text = json_match.group(1) if json_match else response.text
            tasks = json.loads(json_text)
            # 時間順にソート
            return sorted(tasks, key=lambda x: x.get('start_time', '99:99'))
        except (json.JSONDecodeError, KeyError) as e:
            logging.error(f"AIスケジュール生成のJSON解析に失敗: {e}\nAI Response: {response.text}")
            return []

    async def confirm_and_register_schedule(self, interaction: discord.Interaction, tasks: list):
        """確認されたスケジュールをGoogleカレンダーに登録する"""
        if not interaction.response.is_done():
            await interaction.response.defer()
            
        today = datetime.now(JST).date()
        registered_tasks = []
        
        for task in tasks:
            try:
                start_time_dt = datetime.strptime(task['start_time'], '%H:%M').time()
                start_datetime = datetime.combine(today, start_time_dt, tzinfo=JST)
                
                await self._create_google_calendar_event(
                    summary=task['summary'],
                    date=today,
                    start_time=start_datetime,
                    duration_minutes=15 # 所要時間は15分に固定
                )
                registered_tasks.append(task['summary'])
                await asyncio.sleep(0.5) # APIレート制限対策
            except Exception as e:
                logging.error(f"カレンダー登録エラー ({task['summary']}): {e}")
                await interaction.followup.send(f"⚠️「{task['summary']}」の登録中にエラーが発生しました。", ephemeral=True)
        
        await interaction.message.edit(content="✅ スケジュールをカレンダーに登録しました。", view=None, embed=None)

        # ハイライト選択のフローを開始
        if registered_tasks:
            view = HighlightChoiceView(self, registered_tasks)
            await interaction.followup.send("次に、今日一日のハイライトを選択してください。", view=view, ephemeral=False)

    async def create_highlight_event(self, interaction: discord.Interaction, highlight_text: str):
        """ハイライトを終日予定としてカレンダーに登録する"""
        today = datetime.now(JST).date()
        summary = f"✨ ハイライト: {highlight_text}"
        
        try:
            await self._create_google_calendar_event(
                summary=summary,
                date=today,
                color_id=HIGHLIGHT_COLOR_ID
            )
            await self._update_obsidian_highlight(today, highlight_text)
            await interaction.followup.send(f"✅ 今日のハイライト「**{highlight_text}**」を設定しました！", ephemeral=True)
        except Exception as e:
            logging.error(f"ハイライト登録エラー: {e}")
            await interaction.followup.send(f"❌ ハイライトの登録中にエラーが発生しました: {e}", ephemeral=True)

    async def _update_obsidian_highlight(self, date: datetime.date, highlight_text: str):
        date_str = date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        content_to_add = f"- {highlight_text}"
        
        try:
            try:
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# {date_str}\n"
                else: raise

            new_content = update_section(current_content, content_to_add, "## Highlight")
            self.dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"Obsidianにハイライトを記録しました: {daily_note_path}")
        except Exception as e:
            logging.error(f"Obsidianへのハイライト記録中にエラー: {e}")


    # --- 新機能: 休日の空き時間チェック ---
    @tasks.loop(hours=2)
    async def check_weekend_gaps(self):
        now = datetime.now(JST)
        # 実行時間を8時から22時の間に限定
        if not (8 <= now.hour <= 22):
            return

        is_holiday = now.weekday() >= 5 or jpholiday.is_holiday(now.date())
        if not is_holiday:
            return

        channel = self.bot.get_channel(self.calendar_channel_id)
        if not channel: return
            
        logging.info("休日の空き時間チェックを実行します...")
        free_slots = await self._find_free_slots(now.date())
        
        for start, end in free_slots:
            if (end - start).total_seconds() >= 7200: # 2時間以上の空き
                # これから始まる空き時間のみを通知
                if start > now:
                    embed = discord.Embed(
                        title="🕒 空き時間のお知らせ",
                        description=f"**{start.strftime('%H:%M')}** から **{end.strftime('%H:%M')}** まで、2時間以上の空き時間があります。\n何か新しいことに挑戦したり、休憩する良い機会かもしれませんね。",
                        color=discord.Color.orange()
                    )
                    await channel.send(embed=embed)
                    logging.info(f"2時間以上の空き時間を検知・通知しました: {start} - {end}")
                    return # 最初の空き時間を見つけたら通知して終了
    
    @check_weekend_gaps.before_loop
    async def before_check_gaps(self):
        await self.bot.wait_until_ready()
        # ループが2時間ごとなので、起動時にちょうど実行されるように調整
        now = datetime.now(JST)
        await asyncio.sleep((120 - (now.minute % 120)) * 60 - now.second)


    async def schedule_task_from_memo(self, task_content: str, target_date: Optional[datetime.date] = None):
        channel = self.bot.get_channel(self.calendar_channel_id)
        if not channel:
            logging.error("カレンダーチャンネルが見つかりません。")
            return

        task_analysis = await self._analyze_task_with_ai(task_content, target_date)
        
        if not task_analysis or not task_analysis.get("summary"):
            await channel.send(f"⚠️「{task_content}」のタスク分析に失敗しました。処理を中断します。", delete_after=15)
            return

        date_to_schedule = datetime.strptime(task_analysis["target_date"], '%Y-%m-%d').date()

        if task_analysis.get("all_day"):
            await self._create_google_calendar_event(task_analysis["summary"], date_to_schedule)
            await channel.send(f"✅ **{date_to_schedule.strftime('%Y-%m-%d')}** の終日予定として「{task_analysis['summary']}」を登録しました。", delete_after=15)
        else:
            free_slots = await self._find_free_slots(date_to_schedule)
            await self._schedule_simple_task(None, task_analysis, free_slots, date_to_schedule)
    
    async def _analyze_task_with_ai(self, task_content: str, specified_date: Optional[datetime.date] = None) -> dict | None:
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        prompt = f"""
        あなたは優秀なプロジェクトマネージャーです。以下のタスクメモを分析し、カレンダー登録用の情報を抽出してください。
        # 指示
        1. **日付の判断**: ユーザーが日付を指定している場合(`specified_date`)はそれを優先し、ない場合はメモ内容から日付を読み取り `YYYY-MM-DD` 形式に変換してください。どちらにもなければ今日の日付 (`{today_str}`) を使用してください。
        2. **時間と所要時間の判断**: メモ内容から具体的な開始時刻や終了時刻を分析してください。時間指定がなく作業時間が予測できる場合は所要時間を分単位で予測してください。時間を必要としないタスクは「終日予定」と判断してください。
        3. **要約**: カレンダーに登録するのにふさわしい簡潔なタスク名（summary）を作成してください。
        4. **出力**: 以下のJSON形式で出力してください。JSON以外の説明や前置きは一切含めないでください。
        # 出力フォーマット
        {{
          "target_date": "YYYY-MM-DD",
          "summary": "（タスクの要約）",
          "start_time": "HH:MM" or null,
          "duration_minutes": （所要時間） or null,
          "all_day": true or false
        }}
        ---
        # タスクメモ: {task_content}
        # ユーザー指定の日付 (あれば): {specified_date.isoformat() if specified_date else "なし"}
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            json_match = re.search(r'```json\n(\{.*?\})\n```', response.text, re.DOTALL)
            json_text = json_match.group(1) if json_match else response.text
            return json.loads(json_text)
        except Exception as e:
            logging.error(f"AIタスク分析のJSON解析に失敗: {e}\nAI Response: {getattr(locals(), 'response', 'N/A')}")
            return None

    async def _find_free_slots(self, target_date: datetime.date) -> list:
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            start_of_day = datetime.combine(target_date, time(0, 0), tzinfo=JST)
            end_of_day = start_of_day + timedelta(days=1)
            events_result = service.events().list(
                calendarId='primary', timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(), singleEvents=True, orderBy='startTime'
            ).execute()
            
            busy_slots = []
            for event in events_result.get('items', []):
                start_str = event['start'].get('dateTime')
                end_str = event['end'].get('dateTime')
                if start_str and end_str:
                    busy_slots.append((datetime.fromisoformat(start_str), datetime.fromisoformat(end_str)))
                elif event['start'].get('date'): # 終日予定
                    event_date = datetime.fromisoformat(event['start']['date']).date()
                    busy_slots.append((
                        datetime.combine(event_date, time.min, tzinfo=JST),
                        datetime.combine(event_date, time.max, tzinfo=JST)
                    ))

            
            work_start_time = start_of_day.replace(hour=WORK_START_HOUR)
            work_end_time = start_of_day.replace(hour=WORK_END_HOUR)
            
            free_slots = []
            current_time = work_start_time
            
            # 忙しい時間帯をソート
            busy_slots.sort()

            for busy_start, busy_end in busy_slots:
                if current_time < busy_start:
                    free_slots.append((current_time, busy_start))
                current_time = max(current_time, busy_end)

            if current_time < work_end_time:
                free_slots.append((current_time, work_end_time))

            return free_slots

        except HttpError as e:
            logging.error(f"Googleカレンダーからの予定取得中にエラー: {e}")
            return []

    async def _schedule_simple_task(self, message: Optional[discord.Message], analysis: dict, free_slots: list, target_date: datetime.date):
        duration = analysis.get('duration_minutes') or 60
        summary = analysis['summary']
        start_time_str = analysis.get('start_time')
        channel = self.bot.get_channel(self.calendar_channel_id)
        if not channel: return
        
        start_time = None
        if start_time_str:
            try:
                parsed_time = datetime.strptime(start_time_str, '%H:%M').time()
                start_time = datetime.combine(target_date, parsed_time, tzinfo=JST)
            except ValueError:
                await channel.send(f"⚠️ AIが提案した開始時刻 `{start_time_str}` の形式が不正なため、空き時間を探します。", delete_after=15)

        if not start_time:
            best_slot_start = next((start for start, end in free_slots if (end - start) >= timedelta(minutes=duration)), None)
            if not best_slot_start:
                await self._create_google_calendar_event(summary, target_date)
                await channel.send(f"💬 **{target_date.strftime('%Y-%m-%d')}** の作業時間内に最適な空き時間が見つからなかったため、終日予定として登録しました。", delete_after=15)
                return
            start_time = best_slot_start

        end_time = start_time + timedelta(minutes=duration)
        await self._create_google_calendar_event(summary, target_date, start_time, duration)
        await channel.send(f"✅ **{target_date.strftime('%m/%d')} {start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}** に「{summary}」を登録しました。", delete_after=15)

    async def _create_google_calendar_event(self, summary: str, date: datetime.date, start_time: Optional[datetime] = None, duration_minutes: int = 60, color_id: str = None):
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            event = {}
            if start_time:
                end_time = start_time + timedelta(minutes=duration_minutes)
                event = {'summary': summary, 'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'}, 'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'}}
            else:
                end_date = date + timedelta(days=1)
                event = {'summary': summary, 'start': {'date': date.isoformat()}, 'end': {'date': end_date.isoformat()}}
            
            if color_id:
                event['colorId'] = color_id
            
            service.events().insert(calendarId='primary', body=event).execute()
            logging.info(f"[CalendarCog] Googleカレンダーに予定を追加しました: '{summary}' on {date}")
        except HttpError as e:
            logging.error(f"Googleカレンダーへのイベント作成中にエラー: {e}")
            raise

    @tasks.loop(time=TODAY_SCHEDULE_TIME)
    async def notify_today_events(self):
        if not self.is_ready: return
        channel = self.bot.get_channel(self.calendar_channel_id)
        if not channel: return

        if self.last_schedule_message_id:
            try:
                old_message = await channel.fetch_message(self.last_schedule_message_id)
                await old_message.delete()
            except discord.NotFound:
                pass
            self.last_schedule_message_id = None

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
            if not events: return
            advice = await self._generate_overall_advice(events)
            embed = self._create_today_embed(today, events, advice)
            new_message = await channel.send(embed=embed)
            self.last_schedule_message_id = new_message.id
            for event in events: await self._add_to_daily_log(event)
        except Exception as e:
            logging.error(f"[CalendarCog] 今日の予定通知中にエラー: {e}", exc_info=True)

    @tasks.loop(time=DAILY_REVIEW_TIME)
    async def send_daily_review(self):
        if not self.is_ready: return
        try:
            today = datetime.now(JST).date()
            today_str = today.strftime('%Y-%m-%d')
            log_path = f"{self.dropbox_vault_path}/.bot/calendar_log/{today_str}.json"
            
            try:
                _, res = self.dbx.files_download(log_path)
                daily_events = json.loads(res.content.decode('utf-8'))
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_events = []
                else: raise
            
            await self._carry_over_uncompleted_tasks()

            if not daily_events:
                logging.info(f"{today_str}のレビュー対象タスクはありません。")
                return

            channel = self.bot.get_channel(self.calendar_channel_id)
            if channel:
                header_msg = await channel.send(f"--- **🗓️ {today_str} のタスクレビュー** ---\nお疲れ様でした！今日のタスクの達成度をボタンで教えてください。")
                
                for event in daily_events:
                    embed = discord.Embed(title=f"タスク: {event['summary']}", color=discord.Color.gold())
                    view = TaskReviewView(self, event['summary'], today)
                    await channel.send(embed=embed, view=view)
                
                footer_msg = await channel.send("--------------------")
                await asyncio.sleep(3600)
                try:
                    await header_msg.delete()
                    await footer_msg.delete()
                except discord.NotFound:
                    pass

        except Exception as e:
            logging.error(f"[CalendarCog] 振り返り通知中にエラー: {e}", exc_info=True)

    async def _carry_over_uncompleted_tasks(self):
        if not self.uncompleted_tasks: return
        
        tasks_to_carry_over = self.uncompleted_tasks.copy()
        self.uncompleted_tasks.clear()

        carry_over_date = datetime.now(JST).date() + timedelta(days=1)
        for task, original_date in tasks_to_carry_over.items():
            await self._create_google_calendar_event(f"【繰越】{task}", carry_over_date)
            logging.info(f"未完了タスク「{task}」(元期日: {original_date})を{carry_over_date}の予定として登録しました。")

        channel = self.bot.get_channel(self.calendar_channel_id)
        if channel and tasks_to_carry_over:
             await channel.send(f"✅ {len(tasks_to_carry_over)}件の未完了タスクを、{carry_over_date.strftime('%Y-%m-%d')}の終日予定としてカレンダーに登録しました。", delete_after=300)
        
        logging.info("[CalendarCog] 未完了タスクの繰り越しが完了しました。")

    async def _generate_overall_advice(self, events: list) -> str:
        event_list_str = "\n".join([f"- {self._format_datetime(e.get('start'))}: {e.get('summary', '名称未設定')}" for e in events])
        prompt = f"以下の今日の予定リスト全体を見て、一日を最も生産的に過ごすための総合的なアドバイスを提案してください。\n# 指示\n- 挨拶や前置きは不要です。\n- 箇条書きで、簡潔に3点ほどアドバイスを生成してください。\n# 今日の予定リスト\n{event_list_str}"
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text
        except Exception as e:
            logging.error(f"総合アドバイスの生成に失敗: {e}")
            return "アドバイスの生成中にエラーが発生しました。"

    def _create_today_embed(self, date: datetime.date, events: list, advice: str) -> discord.Embed:
        embed = discord.Embed(title=f"🗓️ {date.strftime('%Y-%m-%d')} の予定", description=f"**🤖 AIによる一日の過ごし方アドバイス**\n{advice}", color=discord.Color.green())
        event_list = "\n".join([f"**{self._format_datetime(e.get('start'))}** {e.get('summary', '名称未設定')}" for e in events])
        embed.add_field(name="タイムライン", value=event_list, inline=False)
        return embed

    def _format_datetime(self, dt_obj: dict) -> str:
        if 'dateTime' in dt_obj:
            return datetime.fromisoformat(dt_obj['dateTime']).astimezone(JST).strftime('%H:%M')
        return "終日" if 'date' in dt_obj else ""

    async def _add_to_daily_log(self, event: dict):
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        log_path = f"{self.dropbox_vault_path}/.bot/calendar_log/{today_str}.json"
        try:
            try:
                _, res = self.dbx.files_download(log_path)
                daily_events = json.loads(res.content.decode('utf-8'))
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_events = []
                else:
                    raise
            if not any(e['id'] == event['id'] for e in daily_events):
                daily_events.append({'id': event['id'], 'summary': event.get('summary', '名称未設定')})
                try:
                    self.dbx.files_upload(json.dumps(daily_events, indent=2, ensure_ascii=False).encode('utf-8'), log_path, mode=WriteMode('overwrite'))
                except Exception as e:
                    logging.error(f"デイリーログの保存に失敗: {e}")
        except Exception as e:
            logging.error(f"デイリーログの読み込みまたは処理中にエラー: {e}", exc_info=True)
            
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
                self.dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
                logging.info(f"Obsidianのタスクログを更新しました: {daily_note_path}")
                return
            except Exception as e:
                logging.error(f"Obsidianタスクログの更新に失敗 (試行 {attempt + 1}/3): {e}")
                if attempt < 2: await asyncio.sleep(5 * (attempt + 1))
                else: logging.error("リトライの上限に達しました。アップロードを断念します。")

async def setup(bot: commands.Bot):
    await bot.add_cog(CalendarCog(bot))