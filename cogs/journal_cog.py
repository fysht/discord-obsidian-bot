import os
import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, time, timedelta, date
import zoneinfo
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account # サービスアカウントを使う場合
import aiohttp
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import asyncio
import jpholiday
import json

# --- 共通関数をインポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。ダミー関数を使用します。")
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
        if section_header in current_content:
            return current_content.replace(section_header, f"{section_header}\n{link_to_add}")
        else:
            return f"{current_content}\n\n{section_header}\n{link_to_add}\n"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
PLANNING_PROMPT_TIME = time(hour=7, minute=30, tzinfo=JST)
JOURNAL_PROMPT_TIME = time(hour=21, minute=30, tzinfo=JST)
IDLE_CHECK_INTERVAL_HOURS = 1
HIGHLIGHT_EMOJI = "✨"

# --- UIコンポーネント ---

class HighlightInputModal(discord.ui.Modal, title="ハイライトの手動入力"):
    highlight_text = discord.ui.TextInput(
        label="今日のハイライトを入力してください",
        style=discord.TextStyle.short,
        required=True
    )
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        success = await self.cog.set_highlight_on_calendar(self.highlight_text.value, interaction)
        if success:
            await interaction.followup.send(f"✅ ハイライト「**{self.highlight_text.value}**」を設定しました。", ephemeral=True)
        # エラーメッセージは set_highlight_on_calendar 内で送信される

class HighlightOptionsView(discord.ui.View):
    def __init__(self, cog, event_options: list):
        super().__init__(timeout=3600)
        self.cog = cog
        
        if event_options:
            self.add_item(discord.ui.Select(
                placeholder="今日の予定からハイライトを選択...",
                options=event_options,
                custom_id="select_highlight_from_calendar"
            ))
        self.add_item(discord.ui.Button(label="その他のハイライトを入力", style=discord.ButtonStyle.primary, custom_id="input_other_highlight"))

    async def interaction_check(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id")
        
        if custom_id == "select_highlight_from_calendar":
            selected_highlight = interaction.data["values"][0]
            await interaction.response.defer(ephemeral=True)
            success = await self.cog.set_highlight_on_calendar(selected_highlight, interaction)
            if success:
                await interaction.followup.send(f"✅ ハイライト「**{selected_highlight}**」を設定しました。", ephemeral=True)
            self.stop()
            await interaction.message.edit(view=None)
        
        elif custom_id == "input_other_highlight":
            modal = HighlightInputModal(self.cog)
            await interaction.response.send_modal(modal)
            self.stop()
            await interaction.message.edit(view=None)
        return True

class ScheduleInputModal(discord.ui.Modal, title="今日の予定を入力"):
    tasks_input = discord.ui.TextInput(
        label="今日の予定を改行区切りで入力",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n- 読書\n- 1時間の散歩\n- 昼寝 30分\n- 買い物",
        required=True
    )
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.process_schedule(interaction, self.tasks_input.value)

class ScheduleConfirmView(discord.ui.View):
    def __init__(self, cog, proposed_schedule: list):
        super().__init__(timeout=1800)
        self.cog = cog
        self.schedule = proposed_schedule
    
    @discord.ui.button(label="この内容でカレンダーに登録", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.register_schedule_to_calendar(interaction, self.schedule)
        self.stop()
        await interaction.message.edit(content="✅ 予定をGoogleカレンダーに登録しました。次に今日一日を象徴する**ハイライト**を決めましょう。", view=None, embed=None)
        await self.cog._ask_for_highlight(interaction.channel)

    @discord.ui.button(label="修正する", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("お手数ですが、再度ボタンを押して予定を再入力してください。", ephemeral=True)
        self.stop()
        await interaction.message.delete()

class SimpleJournalModal(discord.ui.Modal, title="今日一日の振り返り"):
    journal_entry = discord.ui.TextInput(
        label="今日の出来事や感じたことを自由に記録しましょう。",
        style=discord.TextStyle.paragraph,
        placeholder="楽しかったこと、学んだこと、感謝したことなど...",
        required=True
    )
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog._save_journal_entry(interaction, self.journal_entry.value)

class SimpleJournalView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=7200) # 2時間有効
        self.cog = cog

    @discord.ui.button(label="今日を振り返る", style=discord.ButtonStyle.primary, emoji="📝")
    async def write_journal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SimpleJournalModal(self.cog))
        self.stop()
        await interaction.message.edit(view=None)

# --- Cog本体 ---
class JournalCog(commands.Cog):
    """朝の計画と夜の振り返りを支援するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()

        if not self._validate_env_vars():
            logging.error("JournalCog: 必須の環境変数が不足しています。Cogを無効化します。")
            return

        try:
            self.session = aiohttp.ClientSession()
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            self.google_creds = self._get_google_creds()
            if not self.google_creds:
                raise Exception("Google APIの認証に失敗しました。")
            self.calendar_service = build('calendar', 'v3', credentials=self.google_creds)
            self.idle_reminders_sent = set()
            self.is_ready = True
            logging.info("✅ JournalCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ JournalCogの初期化中にエラー: {e}", exc_info=True)

    def _load_env_vars(self):
        self.channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.google_calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        return all([self.channel_id, self.google_calendar_id, self.gemini_api_key, self.dropbox_refresh_token])

    def _get_google_creds(self):
        creds = None
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', ['https://www.googleapis.com/auth/calendar'])
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logging.error(f"Google APIトークンのリフレッシュに失敗: {e}")
                    os.remove('token.json') # 壊れたトークンファイルを削除
                    return None
            else:
                logging.error("Google APIの認証情報(token.json)が見つからないか、無効です。")
                return None
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        return creds

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.daily_planning_task.is_running(): self.daily_planning_task.start()
            if not self.prompt_daily_journal.is_running(): self.prompt_daily_journal.start()
            if not self.check_idle_time_loop.is_running(): self.check_idle_time_loop.start()

    async def cog_unload(self):
        await self.session.close()
        self.daily_planning_task.cancel()
        self.prompt_daily_journal.cancel()
        self.check_idle_time_loop.cancel()

    async def _get_todays_events(self) -> list:
        if not self.is_ready: return []
        try:
            now = datetime.now(JST)
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            time_max = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
            events_result = self.calendar_service.events().list(
                calendarId=self.google_calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            return events_result.get('items', [])
        except HttpError as e:
            logging.error(f"Google Calendarからの予定取得に失敗: {e}")
            return []

    async def set_highlight_on_calendar(self, highlight_text: str, interaction: discord.Interaction) -> bool:
        """指定されたテキストに一致する予定をハイライトする"""
        try:
            events = await self._get_todays_events()
            target_event = None
            for event in events:
                if event.get('summary') == highlight_text:
                    target_event = event
                    break
            
            # 予定が見つからない場合は、新しい終日予定としてハイライトを作成
            if not target_event:
                today_str = date.today().isoformat()
                event_body = {
                    'summary': f"{HIGHLIGHT_EMOJI} {highlight_text}",
                    'start': {'date': today_str},
                    'end': {'date': today_str},
                    'colorId': '5' # 黄色
                }
                self.calendar_service.events().insert(calendarId=self.google_calendar_id, body=event_body).execute()
                return True

            # 既存の予定を更新
            updated_body = {
                'summary': f"{HIGHLIGHT_EMOJI} {target_event['summary']}",
                'colorId': '5' # 黄色
            }
            self.calendar_service.events().patch(
                calendarId=self.google_calendar_id,
                eventId=target_event['id'],
                body=updated_body
            ).execute()
            return True
        except HttpError as e:
            logging.error(f"カレンダーのハイライト設定に失敗: {e}")
            await interaction.followup.send("カレンダーのハイライト設定に失敗しました。APIエラー。", ephemeral=True)
            return False

    @tasks.loop(time=PLANNING_PROMPT_TIME)
    async def daily_planning_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        self.idle_reminders_sent.clear()
        view = discord.ui.View(timeout=7200)
        button = discord.ui.Button(label="1日の計画を立てる", style=discord.ButtonStyle.success, custom_id="plan_day")
        async def planning_callback(interaction: discord.Interaction):
            await interaction.response.send_modal(ScheduleInputModal(self))
            view.stop()
            await interaction.message.edit(content="AIがスケジュール案を作成中です...", view=None)
        button.callback = planning_callback
        view.add_item(button)
        await channel.send("おはようございます！有意義な一日を過ごすために、まず1日の計画を立てませんか？", view=view)

    async def _ask_for_highlight(self, channel: discord.TextChannel):
        await asyncio.sleep(2)
        events = await self._get_todays_events()
        event_summaries = [e.get('summary', '名称未設定') for e in events if 'date' not in e.get('start', {}) and HIGHLIGHT_EMOJI not in e.get('summary', '')]
        description = "今日のハイライトを決めて、一日に集中する軸を作りましょう。\n\n"
        if event_summaries:
            description += "今日の予定リストからハイライトを選択するか、新しいハイライトを入力してください。"
        else:
            description += "ハイライトとして取り組みたいことを入力してください。"
        embed = discord.Embed(title=f"{HIGHLIGHT_EMOJI} 今日のハイライト決め", description=description, color=discord.Color.blue())
        event_options = [discord.SelectOption(label=s[:100], value=s[:100]) for s in event_summaries][:25] # 選択肢は25個まで
        view = HighlightOptionsView(self, event_options)
        await channel.send(embed=embed, view=view)

    async def process_schedule(self, interaction: discord.Interaction, tasks_text: str):
        existing_events = await self._get_todays_events()
        events_context = "\n".join([f"- {e['summary']} (開始: {e.get('start', {}).get('dateTime', e.get('start', {}).get('date'))})" for e in existing_events])
        prompt = f"""
        あなたは優秀なパーソナルアシスタントです。現在の時刻は{datetime.now(JST).strftime('%H:%M')}です。以下のユーザーの予定リストと既存の予定を元に、最適なタイムスケジュールを提案してください。
        # 指示
        - 各タスクの所要時間を常識の範囲で推測してください（ユーザーが指定している場合はそれを優先）。
        - 既存の予定と重ならないように、各タスクの開始時刻と終了時刻を決定してください。
        - 移動時間や休憩時間も考慮し、無理のないスケジュールを作成してください。
        - 結果は必ず以下のJSON形式のリストで出力してください。説明文は不要です。
        # 既存の予定
        {events_context if events_context else "なし"}
        # ユーザーが今日やりたいことのリスト
        {tasks_text}
        # 出力形式 (JSONのみ)
        [
            {{"summary": "タスク名1", "start_time": "HH:MM", "end_time": "HH:MM"}},
            {{"summary": "タスク名2", "start_time": "HH:MM", "end_time": "HH:MM"}}
        ]
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            json_match = re.search(r'\[.*\]', response.text, re.DOTALL)
            if not json_match:
                await interaction.followup.send("AIによるスケジュール提案の生成に失敗しました。形式が正しくありません。", ephemeral=True)
                return
            proposed_schedule = json.loads(json_match.group(0))
            embed = discord.Embed(title="AIによるスケジュール提案", description="AIが作成した本日のスケジュール案です。これでよろしいですか？", color=discord.Color.green())
            for event in proposed_schedule:
                embed.add_field(name=event['summary'], value=f"{event['start_time']} - {event['end_time']}", inline=False)
            view = ScheduleConfirmView(self, proposed_schedule)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"スケジュールの解析中にエラーが発生しました: {e}", ephemeral=True)
            logging.error(f"AIスケジュール提案の処理エラー: {e}")

    async def register_schedule_to_calendar(self, interaction: discord.Interaction, schedule: list):
        """提案されたスケジュールをGoogleカレンダーに一括登録する"""
        try:
            today = date.today()
            for event in schedule:
                start_time = datetime.strptime(event['start_time'], '%H:%M').time()
                end_time = datetime.strptime(event['end_time'], '%H:%M').time()
                start_dt = JST.localize(datetime.combine(today, start_time))
                end_dt = JST.localize(datetime.combine(today, end_time))
                event_body = {
                    'summary': event['summary'],
                    'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
                    'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
                }
                self.calendar_service.events().insert(calendarId=self.google_calendar_id, body=event_body).execute()
            await interaction.followup.send("カレンダーへの登録が完了しました。", ephemeral=True)
        except HttpError as e:
            logging.error(f"カレンダーへのスケジュール登録に失敗: {e}")
            await interaction.followup.send("カレンダーへのスケジュール登録中にエラーが発生しました。", ephemeral=True)

    # --- 夜の振り返り機能 ---
    @tasks.loop(time=JOURNAL_PROMPT_TIME)
    async def prompt_daily_journal(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        embed = discord.Embed(
            title="📝 今日の振り返り",
            description="一日お疲れ様でした。今日一日を振り返り、ジャーナルを記録しませんか？",
            color=discord.Color.purple()
        )
        await channel.send(embed=embed, view=SimpleJournalView(self))

    async def _save_journal_entry(self, interaction: discord.Interaction, entry_text: str):
        """ジャーナルの内容をObsidianのデイリーノートに保存する"""
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        # フォーマットされたジャーナルエントリ
        journal_content = f"- {now.strftime('%H:%M')} {entry_text.strip()}"
        section_header = "## Journal"

        try:
            try:
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# {date_str}\n" # ファイルがなければ作成
                else:
                    raise
            
            new_content = update_section(current_content, journal_content, section_header)
            self.dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            await interaction.followup.send("✅ 今日の振り返りを記録しました。", ephemeral=True)
            logging.info(f"ジャーナルをObsidianに保存しました: {daily_note_path}")

        except Exception as e:
            logging.error(f"Obsidianへのジャーナル保存に失敗: {e}")
            await interaction.followup.send("ジャーナルの保存中にエラーが発生しました。", ephemeral=True)


    # --- 空き時間リマインダー (休日のみ) ---
    @tasks.loop(hours=IDLE_CHECK_INTERVAL_HOURS)
    async def check_idle_time_loop(self):
        now = datetime.now(JST)
        today = now.date()
        if (today.weekday() >= 5 or jpholiday.is_holiday(today)) and (9 <= now.hour < 21):
            events = await self._get_todays_events()
            if not events: return
            
            sorted_events = sorted([e for e in events if 'dateTime' in e.get('start', {})], key=lambda e: e['start']['dateTime'])
            
            last_end_time = now
            for event in sorted_events:
                start_time = datetime.fromisoformat(event['start']['dateTime'])
                end_time = datetime.fromisoformat(event['end']['dateTime'])
                
                if start_time < now:
                    last_end_time = max(last_end_time, end_time)
                    continue

                idle_duration = start_time - last_end_time
                if idle_duration >= timedelta(hours=2):
                    reminder_key = f"{today.isoformat()}-{last_end_time.hour}"
                    if reminder_key not in self.idle_reminders_sent:
                        channel = self.bot.get_channel(self.channel_id)
                        if channel:
                            await channel.send(f"💡 **空き時間のお知らせ**\n現在、**{last_end_time.strftime('%H:%M')}** から **{start_time.strftime('%H:%M')}** まで**約{int(idle_duration.total_seconds()/3600)}時間**の空きがあります。何か予定を入れませんか？")
                            self.idle_reminders_sent.add(reminder_key)
                
                last_end_time = max(last_end_time, end_time)

async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))