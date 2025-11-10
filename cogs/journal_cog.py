import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from datetime import datetime, time, timedelta, date
import zoneinfo
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import aiohttp
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
# ★ 修正(1): PathLookupError のインポートを削除
from dropbox.exceptions import ApiError
import re
import asyncio
import jpholiday
import json
from typing import Optional

# --- 共通関数をインポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。ダミー関数を使用します。")
    # (ダミー関数の定義)
    def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
        if section_header in current_content:
            lines = current_content.split('\n')
            try:
                header_index = -1
                for i, line in enumerate(lines):
                    if line.strip().lstrip('#').strip().lower() == section_header.lstrip('#').strip().lower():
                        header_index = i
                        break
                if header_index == -1: raise ValueError("Header not found")
                insert_index = header_index + 1
                while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                    insert_index += 1
                if insert_index > header_index + 1 and lines[insert_index - 1].strip() != "":
                    lines.insert(insert_index, "")
                    insert_index += 1
                lines.insert(insert_index, text_to_add)
                return "\n".join(lines)
            except ValueError:
                 return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"
        else:
            return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HIGHLIGHT_EMOJI = "✨"
BASE_PATH = os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')
PLANNING_SCHEDULE_PATH = f"{BASE_PATH}/.bot/planning_schedule.json"
JOURNAL_SCHEDULE_PATH = f"{BASE_PATH}/.bot/journal_schedule.json"
# ★ 修正: 時刻の正規表現を拡張 (HH:MM | H:MM | H | HH | Hmm | HHmm)
TIME_SCHEDULE_REGEX = re.compile(r'^(\d{1,2}:\d{2}|\d{1,4})(?:[~-](\d{1,2}:\d{2}|\d{1,4}))?\s+(.+)$')


# --- UIコンポーネント ---

# --- 朝の計画用モーダル (日本語UI) ---
class MorningPlanningModal(discord.ui.Modal, title="今日の計画"):
    highlight = discord.ui.TextInput(
        label="今日のハイライト (最重要タスク)",
        style=discord.TextStyle.short,
        placeholder="例: プロジェクトAの設計書を完成させる",
        required=True
    )
    
    schedule = discord.ui.TextInput(
        label="今日の予定 (編集/追加)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500
    )

    def __init__(self, cog, existing_schedule_text: str):
        super().__init__(timeout=1800)
        self.cog = cog
        self.schedule.default = existing_schedule_text

    async def on_submit(self, interaction: discord.Interaction):
        logging.info(f"MorningPlanningModal on_submit called by {interaction.user}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog._save_planning_entry(
                interaction,
                self.highlight.value,
                self.schedule.value
            )
        except Exception as e:
             logging.error(f"MorningPlanningModal on_submit error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ 計画の保存中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in MorningPlanningModal: {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
             try:
                 await interaction.response.send_message(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
             except discord.InteractionResponded:
                  await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)

# --- 朝の計画用View (日本語UI) ---
class MorningPlanningView(discord.ui.View):
    def __init__(self, cog, existing_schedule_text: str):
        super().__init__(timeout=7200)
        self.cog = cog
        self.existing_schedule_text = existing_schedule_text
        self.message = None

    @discord.ui.button(label="今日の計画を立てる", style=discord.ButtonStyle.success, emoji="☀️")
    async def plan_day(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"MorningPlanningView button clicked by {interaction.user}")
        try:
            await interaction.response.send_modal(
                MorningPlanningModal(self.cog, self.existing_schedule_text)
            )
            if self.message:
                await self.message.edit(view=None)
            self.stop()
        except Exception as e_modal:
             logging.error(f"Error sending MorningPlanningModal: {e_modal}", exc_info=True)
             if not interaction.response.is_done():
                 try:
                     await interaction.response.send_message(f"❌ 計画入力モーダルの表示に失敗しました: {e_modal}", ephemeral=True)
                 except discord.InteractionResponded:
                      await interaction.followup.send(f"❌ 計画入力モーダルの表示に失敗しました: {e_modal}", ephemeral=True)
             else:
                 await interaction.followup.send(f"❌ 計画入力モーダルの表示に失敗しました: {e_modal}", ephemeral=True)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


# --- 夜の振り返り用モーダル (日本語UI) ---
class NightlyReviewModal(discord.ui.Modal, title="今日一日の振り返り"):
    wins = discord.ui.TextInput(
        label="今日上手くいったこと (Wins)",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n- 集中してタスクを終えられた\n- 散歩が気持ちよかった",
        required=True
    )
    learnings = discord.ui.TextInput(
        label="学んだこと (Learnings)",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n- 新しいショートカットキーを覚えた\n- あの人にはこういう伝え方が良いと分かった",
        required=True
    )
    todays_events = discord.ui.TextInput(
        label="今日の出来事 (食事、場所、ハイライトの結果など)",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n- 昼食はラーメンを食べた\n- ハイライトは7割達成\n- 夜はジムに行った",
        required=False
    )
    tomorrows_schedule = discord.ui.TextInput(
        label="翌日の予定 (Googleカレンダーに追加)",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n10:00 チームミーティング\n14:00 歯医者\n18:00 友人との夕食",
        required=False,
        max_length=1000
    )
    
    def __init__(self, cog):
        super().__init__(timeout=1800)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        logging.info(f"NightlyReviewModal on_submit called by {interaction.user}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog._save_journal_entry(
                interaction, 
                self.wins.value, 
                self.learnings.value, 
                self.todays_events.value,
                self.tomorrows_schedule.value
            )
        except Exception as e:
             logging.error(f"NightlyReviewModal on_submit error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ ジャーナル保存中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in NightlyReviewModal: {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
             try:
                 await interaction.response.send_message(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
             except discord.InteractionResponded:
                  await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)


# --- 夜の振り返り用View (日本語UI) ---
class NightlyJournalView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=7200)
        self.cog = cog
        self.message = None

    @discord.ui.button(label="今日を振り返る", style=discord.ButtonStyle.primary, emoji="📝")
    async def write_journal(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"NightlyJournalView write_journal called by {interaction.user}")
        try:
            await interaction.response.send_modal(NightlyReviewModal(self.cog))
            if self.message:
                await self.message.edit(view=None)
            self.stop()
        except Exception as e:
            logging.error(f"NightlyJournalView button click error sending modal: {e}", exc_info=True)
            if not interaction.response.is_done():
                 try:
                     await interaction.response.send_message(f"❌ モーダル表示中にエラーが発生しました: {e}", ephemeral=True)
                 except discord.InteractionResponded:
                      pass
            else:
                  await interaction.followup.send(f"❌ モーダル表示中にエラーが発生しました: {e}", ephemeral=True)

    async def on_timeout(self):
        logging.info("NightlyJournalView timed out.")
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


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

            self.planning_schedule_path = PLANNING_SCHEDULE_PATH
            self.journal_schedule_path = JOURNAL_SCHEDULE_PATH

            self.google_creds = self._get_google_creds()
            if not self.google_creds:
                 logging.error("Google APIの認証に失敗しました。カレンダー機能は利用できません。")
                 self.calendar_service = None
            else:
                 self.calendar_service = build('calendar', 'v3', credentials=self.google_creds)
                 logging.info("Google Calendar APIの認証に成功しました。")

            self.today_events_text_cache = ""
            self.is_ready = True
            logging.info("✅ JournalCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ JournalCogの初期化中にエラー: {e}", exc_info=True)
            self.is_ready = False

    def _load_env_vars(self):
        self.channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.google_calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        required = {
            "JOURNAL_CHANNEL_ID": self.channel_id != 0,
            "GOOGLE_CALENDAR_ID": bool(self.google_calendar_id),
            "GEMINI_API_KEY": bool(self.gemini_api_key),
            "DROPBOX_APP_KEY": bool(self.dropbox_app_key),
            "DROPBOX_APP_SECRET": bool(self.dropbox_app_secret),
            "DROPBOX_REFRESH_TOKEN": bool(self.dropbox_refresh_token),
            "DROPBOX_VAULT_PATH": bool(self.dropbox_vault_path)
        }
        missing = [name for name, present in required.items() if not present]
        if missing:
            logging.error(f"JournalCog: 不足している環境変数があります: {', '.join(missing)}")
            return False
        if not os.path.exists('token.json'):
             logging.warning("JournalCog: Google API認証ファイル 'token.json' が見つかりません。")
        logging.info("JournalCog: 必要な環境変数はすべて設定されています。")
        return True

    def _get_google_creds(self):
        creds = None
        if not os.path.exists('token.json'):
             logging.error("token.json が見つかりません。generate_token.py を実行して作成してください。")
             return None
        try:
            creds = Credentials.from_authorized_user_file('token.json', ['https://www.googleapis.com/auth/calendar'])
            logging.info("token.json を読み込みました。")
        except Exception as e:
            logging.error(f"token.json の読み込みに失敗しました: {e}")
            return None
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logging.info("Google APIトークンが期限切れです。リフレッシュを試みます...")
                try:
                    creds.refresh(Request())
                    logging.info("Google APIトークンのリフレッシュに成功しました。")
                    with open('token.json', 'w') as token:
                        token.write(creds.to_json())
                    logging.info("更新された token.json を保存しました。")
                except Exception as e:
                    logging.error(f"Google APIトークンのリフレッシュに失敗しました: {e}")
                    try:
                        os.remove('token.json')
                        logging.info("無効な可能性のある token.json を削除しました。")
                    except OSError as e_rm:
                         logging.error(f"token.json の削除に失敗しました: {e_rm}")
                    return None
            else:
                logging.error("Google APIの認証情報が無効です。リフレッシュトークンがないか、他の問題が発生しています。")
                logging.error("generate_token.py を再実行して token.json を再生成してください。")
                return None
        return creds


    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            logging.info("JournalCog is ready. Starting tasks...")
            await self.bot.wait_until_ready()
            
            planning_schedule = await self._load_schedule_from_db(self.planning_schedule_path)
            if planning_schedule:
                plan_time = time(hour=planning_schedule['hour'], minute=planning_schedule['minute'], tzinfo=JST)
                self.daily_planning_task.change_interval(time=plan_time)
                if not self.daily_planning_task.is_running():
                    self.daily_planning_task.start()
                logging.info(f"Daily planning task scheduled for {plan_time}.")
            else:
                logging.info("朝の計画タスクのスケジュールが設定されていません。タスクは開始しません。")

            journal_schedule = await self._load_schedule_from_db(self.journal_schedule_path)
            if journal_schedule:
                journal_time = time(hour=journal_schedule['hour'], minute=journal_schedule['minute'], tzinfo=JST)
                self.prompt_daily_journal.change_interval(time=journal_time)
                if not self.prompt_daily_journal.is_running():
                    self.prompt_daily_journal.start()
                logging.info(f"Daily journal prompt task scheduled for {journal_time}.")
            else:
                logging.info("夜の振り返りタスクのスケジュールが設定されていません。タスクは開始しません。")
        else:
            logging.error("JournalCog is not ready. Tasks will not start.")


    async def cog_unload(self):
        logging.info("Unloading JournalCog...")
        if hasattr(self, 'session') and self.session and not self.session.closed:
            await self.session.close()
        if hasattr(self, 'daily_planning_task'):
            self.daily_planning_task.cancel()
        if hasattr(self, 'prompt_daily_journal'):
            self.prompt_daily_journal.cancel()
        logging.info("JournalCog unloaded.")

    async def _get_todays_events(self, target_date: date = None) -> list:
        if not self.calendar_service:
             logging.warning("Calendar service is not available.")
             return []
        try:
            if target_date is None:
                target_date = datetime.now(JST).date()
            
            # ★ 修正: .localize() を tzinfo=JST に変更
            dt_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=JST)
            dt_end = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=JST)
            
            time_min = dt_start.isoformat()
            time_max = dt_end.isoformat()
            
            logging.info(f"Fetching Google Calendar events from {time_min} to {time_max} for calendar ID: {self.google_calendar_id}")
            events_result = await asyncio.to_thread(
                 self.calendar_service.events().list(
                    calendarId=self.google_calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute
            )
            items = events_result.get('items', [])
            logging.info(f"Found {len(items)} events for {target_date}.")
            return items
        except HttpError as e:
            logging.error(f"Google Calendarからの予定取得中にHttpErrorが発生: Status {e.resp.status}, Reason: {e.reason}, Content: {e.content}")
            return []
        except Exception as e:
            logging.error(f"Google Calendarからの予定取得中に予期せぬエラーが発生: {e}", exc_info=True)
            return []


    async def set_highlight_on_calendar(self, highlight_text: str, interaction: discord.Interaction) -> bool:
        if not self.calendar_service:
             logging.warning("Cannot set highlight: Calendar service is not available.")
             if interaction and interaction.response.is_done():
                 await interaction.followup.send("❌ カレンダー機能が利用できません (API認証エラー)。", ephemeral=True)
             return False
        
        try:
            events = await self._get_todays_events()
            target_event = None
            for event in events:
                if event.get('summary') == highlight_text:
                    if not event.get('summary', '').startswith(HIGHLIGHT_EMOJI):
                        target_event = event
                    else:
                         logging.info(f"Event '{highlight_text}' is already highlighted.")
                         return True
                    break

            today_str = date.today().isoformat()
            operation_type = "更新" if target_event else "新規作成"
            logging.info(f"Attempting to {operation_type} highlight: '{highlight_text}'")

            if target_event:
                updated_body = {
                    'summary': f"{HIGHLIGHT_EMOJI} {target_event['summary']}",
                    'colorId': '5'
                }
                await asyncio.to_thread(
                    self.calendar_service.events().patch(
                        calendarId=self.google_calendar_id,
                        eventId=target_event['id'],
                        body=updated_body
                    ).execute
                )
                logging.info(f"Successfully patched event ID {target_event['id']} as highlight.")
            else:
                event_body = {
                    'summary': f"{HIGHLIGHT_EMOJI} {highlight_text}",
                    'start': {'date': today_str},
                    'end': {'date': today_str},
                    'colorId': '5'
                }
                await asyncio.to_thread(
                    self.calendar_service.events().insert(
                        calendarId=self.google_calendar_id,
                        body=event_body
                    ).execute
                )
                logging.info(f"Successfully inserted new all-day event as highlight: '{highlight_text}'")

            return True

        except HttpError as e:
            logging.error(f"カレンダーのハイライト設定中にHttpErrorが発生: Status {e.resp.status}, Reason: {e.reason}, Content: {e.content}")
            error_message = f"カレンダーのハイライト設定に失敗しました (HTTP {e.resp.status})。"
            if e.resp.status == 403:
                error_message += " カレンダーへの書き込み権限がない可能性があります。"
            await interaction.followup.send(f"❌ {error_message}", ephemeral=True)
            return False
        except Exception as e:
            logging.error(f"カレンダーのハイライト設定中に予期せぬエラーが発生: {e}", exc_info=True)
            await interaction.followup.send(f"❌ ハイライト設定中に予期せぬエラーが発生しました: {e}", ephemeral=True)
            return False

    # --- 朝の計画タスク (日本語UI) ---
    @tasks.loop()
    async def daily_planning_task(self):
        logging.info("Executing daily_planning_task...")
        
        if not self.daily_planning_task.time:
             logging.warning("daily_planning_task: タスクが実行されましたが、有効な実行時刻が設定されていません。")
             return
             
        if not self.is_ready:
             logging.warning("JournalCog is not ready, skipping daily_planning_task.")
             return
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
             logging.error(f"Planning prompt channel (ID: {self.channel_id}) not found.")
             return

        try:
            events = await self._get_todays_events()
            event_summaries = []
            if events:
                for event in events:
                    summary = event.get('summary', '予定あり')
                    if summary.startswith(HIGHLIGHT_EMOJI):
                        continue
                        
                    start = event.get('start', {}).get('dateTime')
                    if start:
                        start_time = datetime.fromisoformat(start).astimezone(JST).strftime('%H:%M')
                        end = event.get('end', {}).get('dateTime')
                        end_time = "N/A"
                        if end:
                            end_time = datetime.fromisoformat(end).astimezone(JST).strftime('%H:%M')
                        event_summaries.append(f"{start_time}-{end_time} {summary}")
                    else:
                        start_date = event.get('start', {}).get('date')
                        if start_date:
                            event_summaries.append(f"終日: {summary}")
            
            self.today_events_text_cache = "\n".join(event_summaries) if event_summaries else "（カレンダーに予定はありません）"
            
            view = MorningPlanningView(self, self.today_events_text_cache)

            embed = discord.Embed(
                title="おはようございます！☀️ 今日の計画を立てましょう",
                description="Googleカレンダーから以下の予定を取得しました。\n内容を確認・編集し、今日のハイライトを決めてください。",
                color=discord.Color.green()
            )
            embed.add_field(
                name="今日の予定 (Googleカレンダー)",
                value=f"```\n{self.today_events_text_cache}\n```",
                inline=False
            )
            sent_message = await channel.send(embed=embed, view=view)
            view.message = sent_message

            logging.info("Planning prompt (with GCal events) sent successfully.")
        except Exception as e:
            logging.error(f"Error in daily_planning_task loop: {e}", exc_info=True)


    # --- 夜の振り返りタスク (日本語UI) ---
    @tasks.loop()
    async def prompt_daily_journal(self):
        logging.info("Executing prompt_daily_journal task...")
        
        if not self.prompt_daily_journal.time:
             logging.warning("prompt_daily_journal: タスクが実行されましたが、有効な実行時刻が設定されていません。")
             return
             
        if not self.is_ready:
             logging.warning("JournalCog is not ready, skipping prompt_daily_journal.")
             return
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
             logging.error(f"Journal prompt channel (ID: {self.channel_id}) not found.")
             return

        try:
            embed = discord.Embed(
                title="📝 今日の振り返り",
                description="一日お疲れ様でした。今日一日を振り返り、ジャーナルと翌日の予定を記録しませんか？",
                color=discord.Color.purple()
            )
            view = NightlyJournalView(self)
            sent_message = await channel.send(embed=embed, view=view)
            view.message = sent_message
            
            logging.info("Journal prompt sent successfully.")
        except Exception as e:
            logging.error(f"Error in prompt_daily_journal loop: {e}", exc_info=True)


    # --- ★ 修正: 朝の計画保存 (Googleカレンダーへの「新規」登録機能を追加) ---
    async def _save_planning_entry(self, interaction: discord.Interaction, highlight: str, schedule: str):
        logging.info("Saving planning entry to Obsidian (Eng) and Calendar (Highlight + New)...")
        if not self.is_ready:
             await interaction.followup.send("❌ 保存機能が利用できません。", ephemeral=True)
             return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"

        # 1. ハイライトをカレンダーに登録 (先に行う)
        highlight_success = False
        if highlight:
            highlight_success = await self.set_highlight_on_calendar(highlight, interaction)
        
        # 2. (新規) モーダルに入力されたスケジュールをパース
        schedule_list_for_calendar = self._parse_schedule_text(schedule)
        
        # 3. (新規) 元のカレンダーテキストと比較し、新規追加分のみを抽出
        original_calendar_text = self.today_events_text_cache
        new_events_to_register = []
        if schedule_list_for_calendar:
            for item in schedule_list_for_calendar:
                # 簡易的なチェック: 予定の「概要(summary)」が元のカレンダーテキストに含まれていなければ、新規とみなす
                # (時刻変更は検知せず、あくまで「新規」のテキストのみ)
                if item['summary'] not in original_calendar_text:
                    new_events_to_register.append(item)
        logging.info(f"朝の計画から {len(new_events_to_register)} 件の新規予定をカレンダーに登録します。")

        # 4. (新規) 今日の日付を取得
        today_date = now.date()

        # 5. (新規) カレンダー保存タスクを定義
        async def save_new_events_to_calendar():
            if not new_events_to_register or not self.calendar_service:
                return None # 登録対象なし、またはカレンダーサービスなし
            try:
                # _register_schedule_to_calendar を「本日」の日付で実行
                success = await self._register_schedule_to_calendar(interaction, new_events_to_register, today_date)
                return success
            except Exception as e:
                logging.error(f"朝の計画のカレンダー登録中に予期せぬエラー: {e}", exc_info=True)
                return False # 失敗

        # 6. (既存) Obsidian保存タスクを定義
        async def save_planning_to_obsidian():
            try:
                # Obsidianの項目は英語
                planning_content = f"""
- **Highlight:** {highlight}
### Schedule
{schedule.strip()}
"""
                section_header = "## Planning"

                current_content = ""
                try:
                    logging.debug(f"Downloading daily note: {daily_note_path}")
                    _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                    current_content = res.content.decode('utf-8')
                    logging.debug("Daily note downloaded successfully.")
                except ApiError as e:
                    if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        logging.info(f"Daily note {daily_note_path} not found. Creating new file content.")
                        current_content = f"# {date_str}\n"
                    else:
                        raise

                new_content = update_section(current_content, planning_content, section_header)

                await asyncio.to_thread(
                    self.dbx.files_upload,
                    new_content.encode('utf-8'), 
                    daily_note_path, 
                    mode=WriteMode('overwrite')
                )
                logging.info(f"Planning entry saved successfully to Obsidian: {daily_note_path}")
                return True
            except Exception as e:
                logging.error(f"Obsidianへの計画保存中に予期せぬエラーが発生: {e}", exc_info=True)
                return False
        
        # 7. (新規) Obsidian保存とカレンダー保存を並列実行
        try:
            obsidian_success, calendar_success = await asyncio.gather(
                save_planning_to_obsidian(),
                save_new_events_to_calendar()
            )

            # 8. (新規) 実行結果をまとめてフィードバック (日本語)
            response_messages = []
            if obsidian_success:
                response_messages.append("✅ 今日の計画をObsidianに記録しました。")
            else:
                response_messages.append("❌ 計画のObsidian記録に失敗しました。")

            if highlight:
                response_messages.append(f"✅ ハイライト「**{highlight}**」をカレンダーに登録しました。" if highlight_success else f"❌ ハイライト「**{highlight}**」のカレンダー登録に失敗しました。")
            
            # カレンダーの新規登録結果
            if calendar_success is True:
                response_messages.append(f"✅ スケジュールから **{len(new_events_to_register)}** 件の新規予定をカレンダーに登録しました。")
            elif calendar_success is False:
                response_messages.append(f"❌ スケジュールからの新規予定のカレンダー登録に失敗しました。")
            # (calendar_success is None の場合は（対象なし）、何も表示しない)
            
            await interaction.followup.send("\n".join(response_messages), ephemeral=True)

        except Exception as e_gather:
             logging.error(f"計画保存の並列処理中にエラー: {e_gather}", exc_info=True)
             await interaction.followup.send(f"❌ 保存処理中に予期せぬエラーが発生しました: {e_gather}", ephemeral=True)


    # --- 夜の振り返り保存 (英語項目) ---
    async def _save_journal_entry(self, interaction: discord.Interaction, wins: str, learnings: str, todays_events: Optional[str], tomorrows_schedule: Optional[str]):
        logging.info("Saving journal entry to Obsidian (Eng) and GCal (tomorrow)...")
        if not self.is_ready:
             await interaction.followup.send("❌ ジャーナル保存機能が利用できません。", ephemeral=True)
             return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"

        # 1. Obsidian用コンテンツ作成 (英語項目)
        journal_content = f"- {now.strftime('%H:%M')}\n"
        
        def format_as_list(text):
            if not text or not text.strip(): return ""
            lines = [f"\t\t- {line.strip()}" if not line.strip().startswith(('-', '*', '+')) else f"\t\t{line.strip()}" for line in text.strip().split('\n') if line.strip()]
            return "\n".join(lines)

        journal_content += f"\t- **Wins:**\n" + format_as_list(wins)
        journal_content += f"\n\t- **Learnings:**\n" + format_as_list(learnings)
        
        if todays_events and todays_events.strip():
            journal_content += f"\n\t- **Today's Events:**\n" + format_as_list(todays_events)

        section_header = "## Journal"

        # 2. 翌日の予定をパース
        tomorrow_date = (now + timedelta(days=1)).date()
        schedule_list = []
        if tomorrows_schedule and tomorrows_schedule.strip():
            schedule_list = self._parse_schedule_text(tomorrows_schedule)
            logging.info(f"Parsed {len(schedule_list)} events for tomorrow ({tomorrow_date})")

        # 3. Obsidianに保存 (非同期)
        async def save_to_obsidian():
            try:
                current_content = ""
                try:
                    logging.debug(f"Downloading daily note: {daily_note_path}")
                    _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                    current_content = res.content.decode('utf-8')
                    logging.debug("Daily note downloaded successfully.")
                except ApiError as e:
                    if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        logging.info(f"Daily note {daily_note_path} not found. Creating new file content.")
                        current_content = f"# {date_str}\n"
                    else:
                        raise
                
                new_content = update_section(current_content, journal_content, section_header)
                
                await asyncio.to_thread(
                    self.dbx.files_upload,
                    new_content.encode('utf-8'), 
                    daily_note_path, 
                    mode=WriteMode('overwrite')
                )
                logging.info(f"Journal entry saved successfully to Obsidian: {daily_note_path}")
                return True
            except Exception as e:
                logging.error(f"Obsidianへのジャーナル保存中に予期せぬエラーが発生: {e}", exc_info=True)
                return False

        # 4. Googleカレンダーに登録 (非同期)
        async def save_to_calendar():
            if not schedule_list or not self.calendar_service:
                return None
            try:
                success = await self._register_schedule_to_calendar(interaction, schedule_list, tomorrow_date)
                return success
            except Exception as e:
                logging.error(f"翌日のカレンダー登録中に予期せぬエラー: {e}", exc_info=True)
                return False

        # 5. 並列実行と結果通知 (日本語)
        try:
            obsidian_success, calendar_success = await asyncio.gather(
                save_to_obsidian(),
                save_to_calendar()
            )

            response_messages = []
            if obsidian_success:
                response_messages.append("✅ 今日の振り返りをObsidianに記録しました。")
            else:
                response_messages.append("❌ 振り返りのObsidian記録に失敗しました。")

            if calendar_success is True:
                response_messages.append(f"✅ 翌日の予定 {len(schedule_list)} 件をカレンダーに登録しました。")
            elif calendar_success is False:
                response_messages.append("❌ 翌日のカレンダー登録に失敗しました。")

            await interaction.followup.send("\n".join(response_messages), ephemeral=True)

        except Exception as e_gather:
             logging.error(f"ジャーナル保存の並列処理中にエラー: {e_gather}", exc_info=True)
             await interaction.followup.send(f"❌ 保存処理中に予期せぬエラーが発生しました: {e_gather}", ephemeral=True)

    # ★ 修正: 簡略化された時刻入力をパースするヘルパー
    def _normalize_time_str(self, time_str: str) -> Optional[str]:
        """
        "9", "930", "1015", "9:30" などを "HH:MM" 形式に正規化する。
        失敗した場合は None を返す。
        """
        if not time_str:
            return None
        
        time_str = time_str.strip()
        
        # 1. "HH:MM" または "H:MM" 形式
        if ':' in time_str:
            try:
                parts = time_str.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return f"{hour:02d}:{minute:02d}"
            except (ValueError, IndexError):
                pass # 他の形式でパース試行
                
        # 2. "H" または "HH" 形式 (例: "9" -> 09:00, "10" -> 10:00)
        if len(time_str) <= 2:
            try:
                hour = int(time_str)
                if 0 <= hour <= 23:
                    return f"{hour:02d}:00"
            except ValueError:
                pass # 他の形式でパース試行

        # 3. "Hmm" または "HHmm" 形式 (例: "930" -> 09:30, "1015" -> 10:15)
        if len(time_str) == 3 or len(time_str) == 4:
            try:
                if len(time_str) == 3: # "930"
                    hour = int(time_str[0])
                    minute = int(time_str[1:])
                else: # "1015"
                    hour = int(time_str[:2])
                    minute = int(time_str[2:])
                    
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return f"{hour:02d}:{minute:02d}"
            except ValueError:
                pass # パース失敗

        logging.warning(f"サポートされていない時刻形式です: '{time_str}'")
        return None # どの形式にも一致しない

    # ★ 修正: _normalize_time_str を使うように _parse_schedule_text を変更
    def _parse_schedule_text(self, tasks_text: str) -> list[dict]:
        schedule_list = []
        for line in tasks_text.strip().split('\n'):
            # 修正後のREGEX: (HH:MM|Hmm|HHmm|H|HH)(-(HH:MM|...))?( )+(Summary)
            match = TIME_SCHEDULE_REGEX.match(line.strip())
            if match:
                start_time_raw = match.group(1)
                end_time_raw = match.group(2) # オプショナルな終了時刻
                summary = match.group(3).strip()
                
                # ★ 正規化処理
                start_time_str = self._normalize_time_str(start_time_raw)
                end_time_str = self._normalize_time_str(end_time_raw) if end_time_raw else None

                if not start_time_str:
                    logging.warning(f"スケジュール行の開始時刻パースに失敗: '{line}' (入力: {start_time_raw})")
                    continue # 開始時刻がパースできなければスキップ

                try:
                    start_time_obj = datetime.strptime(start_time_str, '%H:%M').time()

                    # 終了時刻の決定
                    if end_time_str:
                        # 終了時刻が指定されている (かつ パース成功) 場合
                        end_time_obj = datetime.strptime(end_time_str, '%H:%M').time()
                    else:
                        # 終了時刻が指定されていない (または パース失敗) 場合 (デフォルト1時間)
                        end_time_obj = (datetime.combine(date.today(), start_time_obj) + timedelta(hours=1)).time()
                    
                    schedule_list.append({
                        "summary": summary,
                        "start_time": start_time_obj.strftime('%H:%M'),
                        "end_time": end_time_obj.strftime('%H:%M')
                    })
                except ValueError as e_time:
                    # strptime が失敗することは _normalize_time_str が正しければないはずだが、念のため
                    logging.warning(f"スケジュール行の時刻パースに失敗 (strptime): '{line}'. エラー: {e_time}")
            elif line.strip():
                 logging.warning(f"スケジュール行の形式が不正 (HH:MM なし): '{line}'")
        return schedule_list


    async def _register_schedule_to_calendar(self, interaction: discord.Interaction, schedule: list, target_date: date) -> bool:
        logging.info(f"Registering {len(schedule)} events to Google Calendar for {target_date}...")
        if not self.calendar_service:
             logging.warning("Cannot register schedule: Calendar service is not available.")
             # (★ 修正: 朝の実行時に interaction が None でないことを確認)
             if interaction and interaction.response.is_done():
                 await interaction.followup.send("❌ カレンダー機能が利用できません (API認証エラー)。", ephemeral=True)
             return False

        successful_registrations = 0
        failed_summaries = []
        try:
            for event in schedule:
                try:
                    start_time = datetime.strptime(event['start_time'], '%H:%M').time()
                    end_time = datetime.strptime(event['end_time'], '%H:%M').time()
                    
                    # ★ 修正: .localize() を tzinfo=JST に変更
                    start_dt = datetime.combine(target_date, start_time, tzinfo=JST)
                    end_dt = datetime.combine(target_date, end_time, tzinfo=JST)
                    if end_dt <= start_dt:
                         logging.warning(f"Event '{event['summary']}' has end time <= start time. Assuming 1 hour duration.")
                         end_dt = start_dt + timedelta(hours=1)

                    event_body = {
                        'summary': event['summary'],
                        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
                        'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
                    }
                    await asyncio.to_thread(
                        self.calendar_service.events().insert(
                            calendarId=self.google_calendar_id,
                            body=event_body
                        ).execute
                    )
                    successful_registrations += 1
                except (ValueError, HttpError, Exception) as e_event:
                     logging.error(f"イベント '{event['summary']}' ({target_date}) のカレンダー登録中にエラー: {e_event}")
                     failed_summaries.append(event['summary'])
            
            if failed_summaries:
                logging.error(f"{len(failed_summaries)}件のカレンダー登録に失敗しました: {', '.join(failed_summaries)}")
                return False
            
            logging.info(f"Finished registering schedule for {target_date}. {successful_registrations}/{len(schedule)} succeeded.")
            return True

        except Exception as e:
            logging.error(f"カレンダーへの一括スケジュール登録中に予期せぬエラー: {e}", exc_info=True)
            return False


    # --- スケジュール管理ヘルパー (日本語UI) ---

    async def _load_schedule_from_db(self, path: str) -> Optional[dict]:
        if not self.dbx: return None
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, path)
            data = json.loads(res.content.decode('utf-8'))
            hour = int(data.get('hour'))
            minute = int(data.get('minute'))
            logging.info(f"Dropboxからスケジュールを読み込みました ({path}): {hour:02d}:{minute:02d}")
            return {"hour": hour, "minute": minute}
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.info(f"スケジュールファイル ({path}) が見つかりません。")
            else:
                logging.error(f"スケジュールファイルの読み込みに失敗 ({path}): {e}")
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, Exception) as e:
            logging.error(f"スケジュールファイルの解析に失敗 ({path}): {e}。")
        return None

    async def _save_schedule_to_db(self, path: str, hour: int, minute: int):
        if not self.dbx: raise Exception("Dropbox client not initialized")
        try:
            data = {"hour": hour, "minute": minute}
            content = json.dumps(data, indent=2).encode('utf-8')
            await asyncio.to_thread(self.dbx.files_upload, content, path, mode=WriteMode('overwrite'))
            logging.info(f"Dropboxにスケジュールを保存しました ({path}): {hour:02d}:{minute:02d}")
        except Exception as e:
            logging.error(f"スケジュールファイルの保存に失敗 ({path}): {e}")
            raise

    async def _delete_schedule_from_db(self, path: str):
        if not self.dbx: raise Exception("Dropbox client not initialized")
        try:
            await asyncio.to_thread(self.dbx.files_delete_v2, path)
            logging.info(f"Dropboxからスケジュールファイル ({path}) を削除しました。")
        except ApiError as e:
            # ★ 修正(2): dropbox.exceptions.PathLookupError -> e.error.is_path_lookup()
            # dropbox.files.PathLookupError を使うために dropbox.files をインポート
            if e.error.is_path_lookup() and e.error.get_path_lookup().is_not_found():
                logging.info(f"スケジュールファイル ({path}) は既に削除されています。")
                pass
            else:
                logging.error(f"スケジュールファイルの削除に失敗: {e}")
                raise
        except Exception as e:
            logging.error(f"スケジュールファイルの削除中に予期せぬエラー ({path}): {e}")
            raise

    # --- スケジュール設定コマンド群 (日本語UI) ---
    
    journal_group = app_commands.Group(name="journal", description="ジャーナル機能のスケジュールを管理します。")

    @journal_group.command(name="set_planning_schedule", description="朝の計画タスクの実行時刻 (JST) を設定します。")
    @app_commands.describe(schedule_time="実行時刻 (HH:MM形式, 24時間表記, JST)。例: 07:30")
    async def set_planning_schedule(self, interaction: discord.Interaction, schedule_time: str):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        match = re.match(r'^([0-2]?[0-9]):([0-5]?[0-9])$', schedule_time.strip())
        if not match:
            await interaction.followup.send("❌ 時刻の形式が正しくありません。`HH:MM` (例: `07:30`) で入力してください。", ephemeral=True)
            return

        try:
            hour = int(match.group(1))
            minute = int(match.group(2))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                 raise ValueError("時刻の範囲が不正です")

            await self._save_schedule_to_db(self.planning_schedule_path, hour, minute)
            new_time_obj = time(hour=hour, minute=minute, tzinfo=JST)
            self.daily_planning_task.change_interval(time=new_time_obj)
            
            if not self.daily_planning_task.is_running():
                self.daily_planning_task.start()
                await interaction.followup.send(f"✅ 朝の計画タスクの時刻を毎日 **{hour:02d}:{minute:02d} (JST)** に設定し、タスクを開始しました。", ephemeral=True)
            else:
                await interaction.followup.send(f"✅ 朝の計画タスクの時刻を毎日 **{hour:02d}:{minute:02d} (JST)** に変更しました。", ephemeral=True)

        except ValueError:
             await interaction.followup.send("❌ 時刻の値が不正です (例: `25:00`)。", ephemeral=True)
        except Exception as e:
            logging.error(f"計画スケジュール設定中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ スケジュールの設定中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    @journal_group.command(name="cancel_planning_schedule", description="朝の計画タスクの定時実行を停止し、スケジュールを削除します。")
    async def cancel_planning_schedule(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        try:
            if self.daily_planning_task.is_running():
                self.daily_planning_task.cancel()
            await self._delete_schedule_from_db(self.planning_schedule_path)
            await interaction.followup.send("✅ 朝の計画タスクの定時実行を停止し、スケジュールを削除しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"計画スケジュール削除中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ スケジュールの削除中に予期せぬエラーが発生しました: {e}", ephemeral=True)


    @journal_group.command(name="set_journal_schedule", description="夜の振り返りタスクの実行時刻 (JST) を設定します。")
    @app_commands.describe(schedule_time="実行時刻 (HH:MM形式, 24時間表記, JST)。例: 21:30")
    async def set_journal_schedule(self, interaction: discord.Interaction, schedule_time: str):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        match = re.match(r'^([0-2]?[0-9]):([0-5]?[0-9])$', schedule_time.strip())
        if not match:
            await interaction.followup.send("❌ 時刻の形式が正しくありません。`HH:MM` (例: `21:30`) で入力してください。", ephemeral=True)
            return

        try:
            hour = int(match.group(1))
            minute = int(match.group(2))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                 raise ValueError("時刻の範囲が不正です")

            await self._save_schedule_to_db(self.journal_schedule_path, hour, minute)
            new_time_obj = time(hour=hour, minute=minute, tzinfo=JST)
            self.prompt_daily_journal.change_interval(time=new_time_obj)
            
            if not self.prompt_daily_journal.is_running():
                self.prompt_daily_journal.start()
                await interaction.followup.send(f"✅ 夜の振り返りタスクの時刻を毎日 **{hour:02d}:{minute:02d} (JST)** に設定し、タスクを開始しました。", ephemeral=True)
            else:
                await interaction.followup.send(f"✅ 夜の振り返りタスクの時刻を毎日 **{hour:02d}:{minute:02d} (JST)** に変更しました。", ephemeral=True)

        except ValueError:
             await interaction.followup.send("❌ 時刻の値が不正です (例: `25:00`)。", ephemeral=True)
        except Exception as e:
            logging.error(f"振り返りスケジュール設定中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ スケジュールの設定中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    @journal_group.command(name="cancel_journal_schedule", description="夜の振り返りタスクの定時実行を停止し、スケジュールを削除します。")
    async def cancel_journal_schedule(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        try:
            if self.prompt_daily_journal.is_running():
                self.prompt_daily_journal.cancel()
            await self._delete_schedule_from_db(self.journal_schedule_path)
            await interaction.followup.send("✅ 夜の振り返りタスクの定時実行を停止し、スケジュールを削除しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"振り返りスケジュール削除中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ スケジュールの削除中に予期せぬエラーが発生しました: {e}", ephemeral=True)
    
    @journal_group.command(name="run_planning_now", description="朝の計画タスクを手動で実行します。")
    async def run_planning_now(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.send_message("✅ 朝の計画タスクを手動で実行します...", ephemeral=True)
        await self.daily_planning_task()

    @journal_group.command(name="run_journal_now", description="夜の振り返りタスクを手動で実行します。")
    async def run_journal_now(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.send_message("✅ 夜の振り返りタスクを手動で実行します...", ephemeral=True)
        await self.prompt_daily_journal()


    # --- before_loop tasks ---
    @daily_planning_task.before_loop
    @prompt_daily_journal.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()
        logging.info("Bot is ready, tasks can now run.")


async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))