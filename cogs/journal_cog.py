# cogs/journal_cog.py
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
# from google.oauth2 import service_account # サービスアカウントは現在使用していない
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
            # 簡易的な追記処理（元の関数の完全な再現ではない）
            lines = current_content.split('\n')
            try:
                header_index = lines.index(section_header)
                insert_index = header_index + 1
                while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                    insert_index += 1
                lines.insert(insert_index, link_to_add)
                return "\n".join(lines)
            except ValueError:
                 return f"{current_content}\n\n{section_header}\n{link_to_add}\n"
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
        super().__init__(timeout=300) # タイムアウトを設定
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        logging.info(f"HighlightInputModal on_submit called by {interaction.user}")
        # deferをthinking=Trueで行う
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            success = await self.cog.set_highlight_on_calendar(self.highlight_text.value, interaction)
            if success:
                await interaction.followup.send(f"✅ ハイライト「**{self.highlight_text.value}**」を設定しました。", ephemeral=True)
            # エラーメッセージは set_highlight_on_calendar 内で送信される
        except Exception as e:
             logging.error(f"HighlightInputModal on_submit error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ ハイライト設定中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in HighlightInputModal: {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
            try:
                # responseが完了していない場合のみ呼び出す
                await interaction.response.send_message(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
            except discord.InteractionResponded:
                 await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)


class HighlightOptionsView(discord.ui.View):
    def __init__(self, cog, event_options: list):
        super().__init__(timeout=3600) # 1時間
        self.cog = cog

        select_options = event_options if event_options else [discord.SelectOption(label="予定なし", value="no_event", description="今日カレンダーに登録された予定はありません")]
        disabled_select = not event_options # 予定がない場合は選択肢を無効化

        select = discord.ui.Select(
            placeholder="今日の予定からハイライトを選択..." if event_options else "カレンダーに予定がありません",
            options=select_options,
            custom_id="select_highlight_from_calendar",
            disabled=disabled_select
        )
        select.callback = self.select_callback # コールバックを登録
        self.add_item(select)

        button = discord.ui.Button(label="その他のハイライトを入力", style=discord.ButtonStyle.primary, custom_id="input_other_highlight")
        button.callback = self.button_callback # コールバックを登録
        self.add_item(button)

    async def select_callback(self, interaction: discord.Interaction):
        logging.info(f"HighlightOptionsView select_callback called by {interaction.user}")
        selected_highlight = interaction.data["values"][0]
        if selected_highlight == "no_event":
             await interaction.response.defer() # 何もせず応答だけ返す
             return

        # deferをthinking=Trueで行う
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            success = await self.cog.set_highlight_on_calendar(selected_highlight, interaction)
            if success:
                await interaction.followup.send(f"✅ ハイライト「**{selected_highlight}**」を設定しました。", ephemeral=True)
            # エラーメッセージは set_highlight_on_calendar 内で送信される
        except Exception as e:
             logging.error(f"HighlightOptionsView select_callback error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ ハイライト設定中に予期せぬエラーが発生しました: {e}", ephemeral=True)
        finally:
             self.stop()
             # interaction.messageが存在するか確認
             if interaction.message:
                 try:
                     await interaction.message.edit(view=None)
                 except discord.NotFound:
                     logging.warning("HighlightOptionsView: 元のメッセージが見つからず編集できませんでした。")
                 except Exception as e_edit:
                     logging.error(f"HighlightOptionsView message edit error: {e_edit}")

    async def button_callback(self, interaction: discord.Interaction):
        logging.info(f"HighlightOptionsView button_callback called by {interaction.user}")
        try:
            modal = HighlightInputModal(self.cog)
            await interaction.response.send_modal(modal)
        except Exception as e:
             logging.error(f"HighlightOptionsView button_callback error sending modal: {e}", exc_info=True)
             # モーダル送信前のエラーのため、followupでエラーを通知
             if not interaction.response.is_done():
                 try:
                     await interaction.response.send_message(f"❌ モーダル表示中にエラーが発生しました: {e}", ephemeral=True)
                 except discord.InteractionResponded:
                      pass # response.send_modalで応答済みの場合があるため無視
             else:
                 # is_done() == True の場合は defer されている可能性は低いが念のため
                  await interaction.followup.send(f"❌ モーダル表示中にエラーが発生しました: {e}", ephemeral=True)
        finally:
            self.stop()
            # interaction.messageが存在するか確認
            if interaction.message:
                try:
                    await interaction.message.edit(view=None)
                except discord.NotFound:
                     logging.warning("HighlightOptionsView: 元のメッセージが見つからず編集できませんでした。")
                except Exception as e_edit:
                     logging.error(f"HighlightOptionsView message edit error: {e_edit}")

    async def on_timeout(self):
        logging.info("HighlightOptionsView timed out.")
        # タイムアウトした場合、元のメッセージからViewを削除するなどの処理
        # (interactionオブジェクトがないため、元のメッセージを取得して編集する必要がある)


class ScheduleInputModal(discord.ui.Modal, title="今日の予定を入力"):
    tasks_input = discord.ui.TextInput(
        label="今日の予定を改行区切りで入力",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n- 読書\n- 1時間の散歩\n- 昼寝 30分\n- 買い物",
        required=True
    )
    def __init__(self, cog):
        super().__init__(timeout=600) # タイムアウトを設定
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        logging.info(f"ScheduleInputModal on_submit called by {interaction.user}")
        # deferをthinking=Trueで行う
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog.process_schedule(interaction, self.tasks_input.value)
        except Exception as e:
             logging.error(f"ScheduleInputModal on_submit error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ スケジュール処理中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in ScheduleInputModal: {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
            try:
                await interaction.response.send_message(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
            except discord.InteractionResponded:
                 await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)


class ScheduleConfirmView(discord.ui.View):
    def __init__(self, cog, proposed_schedule: list):
        super().__init__(timeout=1800) # 30分
        self.cog = cog
        self.schedule = proposed_schedule

    @discord.ui.button(label="この内容でカレンダーに登録", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"ScheduleConfirmView confirm called by {interaction.user}")
        # deferをthinking=Trueで行う
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            success = await self.cog.register_schedule_to_calendar(interaction, self.schedule)
            if success:
                # 正常終了時はregister_schedule_to_calendar内でfollowupされる
                # メッセージ編集とハイライト質問に移る
                if interaction.message:
                     await interaction.message.edit(content="✅ 予定をGoogleカレンダーに登録しました。次に今日一日を象徴する**ハイライト**を決めましょう。", view=None, embed=None)
                await self.cog._ask_for_highlight(interaction.channel)
            # エラー時はregister_schedule_to_calendar内でfollowupされる
        except Exception as e:
             logging.error(f"ScheduleConfirmView confirm error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ カレンダー登録またはハイライト質問中に予期せぬエラーが発生しました: {e}", ephemeral=True)
        finally:
            self.stop() # 正常・異常問わずViewを停止


    @discord.ui.button(label="修正する", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"ScheduleConfirmView edit called by {interaction.user}")
        await interaction.response.send_message("お手数ですが、再度 `/plan` コマンドを実行して予定を再入力してください。", ephemeral=True, delete_after=15)
        self.stop()
        if interaction.message:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                logging.warning("ScheduleConfirmView: メッセージの削除に失敗しました。")

    async def on_timeout(self):
        logging.info("ScheduleConfirmView timed out.")
        # タイムアウト時の処理 (例: メッセージ編集)


class SimpleJournalModal(discord.ui.Modal, title="今日一日の振り返り"):
    journal_entry = discord.ui.TextInput(
        label="今日の出来事や感じたことを自由に記録しましょう。",
        style=discord.TextStyle.paragraph,
        placeholder="楽しかったこと、学んだこと、感謝したことなど...",
        required=True
    )
    def __init__(self, cog):
        super().__init__(timeout=1800) # タイムアウトを設定 (30分)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        logging.info(f"SimpleJournalModal on_submit called by {interaction.user}")
        # deferをthinking=Trueで行う
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog._save_journal_entry(interaction, self.journal_entry.value)
            # 成功メッセージは _save_journal_entry 内で行う
        except Exception as e:
             logging.error(f"SimpleJournalModal on_submit error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ ジャーナル保存中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in SimpleJournalModal: {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
             try:
                 await interaction.response.send_message(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
             except discord.InteractionResponded:
                  await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)


class SimpleJournalView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=7200) # 2時間有効
        self.cog = cog

    @discord.ui.button(label="今日を振り返る", style=discord.ButtonStyle.primary, emoji="📝")
    async def write_journal(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"SimpleJournalView write_journal called by {interaction.user}")
        try:
            await interaction.response.send_modal(SimpleJournalModal(self.cog))
        except Exception as e:
            logging.error(f"SimpleJournalView button click error sending modal: {e}", exc_info=True)
            if not interaction.response.is_done():
                 try:
                     await interaction.response.send_message(f"❌ モーダル表示中にエラーが発生しました: {e}", ephemeral=True)
                 except discord.InteractionResponded:
                      pass
            else:
                  await interaction.followup.send(f"❌ モーダル表示中にエラーが発生しました: {e}", ephemeral=True)

        # モーダル送信後、ボタンを含む元のメッセージは編集しない (モーダルが閉じるのを待つ)
        # self.stop() はモーダル送信が成功したら不要かもしれないが、一旦残す
        self.stop()
        # await interaction.message.edit(view=None) # モーダル表示後にViewを消さない

    async def on_timeout(self):
        logging.info("SimpleJournalView timed out.")
        # タイムアウト時の処理 (例: メッセージ編集)


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

            # Google認証情報の取得試行
            self.google_creds = self._get_google_creds()
            if not self.google_creds:
                 logging.error("Google APIの認証に失敗しました。カレンダー機能は利用できません。")
                 self.calendar_service = None # カレンダーサービスをNoneに
            else:
                 self.calendar_service = build('calendar', 'v3', credentials=self.google_creds)
                 logging.info("Google Calendar APIの認証に成功しました。")

            self.idle_reminders_sent = set()
            self.is_ready = True
            logging.info("✅ JournalCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ JournalCogの初期化中にエラー: {e}", exc_info=True)
            self.is_ready = False # 初期化失敗時はis_readyをFalseに

    def _load_env_vars(self):
        self.channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0)) # デフォルトを0に
        self.google_calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault") # デフォルトを設定

    def _validate_env_vars(self) -> bool:
        """必須環境変数の存在チェックとログ出力"""
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
        # token.jsonの存在確認も追加
        if not os.path.exists('token.json'):
             logging.warning("JournalCog: Google API認証ファイル 'token.json' が見つかりません。")
             # return False # 警告にとどめ、カレンダー機能以外は動くようにする
        logging.info("JournalCog: 必要な環境変数はすべて設定されています。")
        return True

    def _get_google_creds(self):
        """Google API認証情報の取得と更新、ログ強化"""
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
                    # 更新された認証情報を保存
                    with open('token.json', 'w') as token:
                        token.write(creds.to_json())
                    logging.info("更新された token.json を保存しました。")
                except Exception as e:
                    logging.error(f"Google APIトークンのリフレッシュに失敗しました: {e}")
                    # リフレッシュ失敗時は認証情報を破棄してNoneを返す
                    try:
                        os.remove('token.json')
                        logging.info("無効な可能性のある token.json を削除しました。")
                    except OSError as e_rm:
                         logging.error(f"token.json の削除に失敗しました: {e_rm}")
                    return None
            else:
                # リフレッシュトークンがない、またはその他の理由で無効な場合
                logging.error("Google APIの認証情報が無効です。リフレッシュトークンがないか、他の問題が発生しています。")
                logging.error("generate_token.py を再実行して token.json を再生成してください。")
                return None
        return creds


    @commands.Cog.listener()
    async def on_ready(self):
        """Cogの準備完了時の処理、タスクの開始"""
        if self.is_ready:
            logging.info("JournalCog is ready. Starting tasks...")
            if not self.daily_planning_task.is_running():
                self.daily_planning_task.start()
                logging.info(f"Daily planning task scheduled for {PLANNING_PROMPT_TIME}.")
            if not self.prompt_daily_journal.is_running():
                self.prompt_daily_journal.start()
                logging.info(f"Daily journal prompt task scheduled for {JOURNAL_PROMPT_TIME}.")
            if not self.check_idle_time_loop.is_running():
                self.check_idle_time_loop.start()
                logging.info(f"Idle time check loop started (Interval: {IDLE_CHECK_INTERVAL_HOURS} hours).")
        else:
            logging.error("JournalCog is not ready. Tasks will not start.")


    async def cog_unload(self):
        """Cogアンロード時の処理"""
        logging.info("Unloading JournalCog...")
        if hasattr(self, 'session') and self.session: # sessionが存在するか確認
            await self.session.close()
        if hasattr(self, 'daily_planning_task'): # タスクが存在するか確認
            self.daily_planning_task.cancel()
        if hasattr(self, 'prompt_daily_journal'):
            self.prompt_daily_journal.cancel()
        if hasattr(self, 'check_idle_time_loop'):
            self.check_idle_time_loop.cancel()
        logging.info("JournalCog unloaded.")

    async def _get_todays_events(self) -> list:
        """今日のGoogle Calendarイベントを取得 (エラーハンドリング強化)"""
        if not self.calendar_service:
             logging.warning("Calendar service is not available.")
             return []
        try:
            now = datetime.now(JST)
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            time_max = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
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
            logging.info(f"Found {len(items)} events today.")
            return items
        except HttpError as e:
            logging.error(f"Google Calendarからの予定取得中にHttpErrorが発生: Status {e.resp.status}, Reason: {e.reason}, Content: {e.content}")
            # 権限エラーなどの場合、具体的なメッセージを出す
            if e.resp.status == 403:
                 logging.error("アクセス権限がない可能性があります。Google Calendar APIの権限やカレンダーIDを確認してください。")
            elif e.resp.status == 404:
                 logging.error(f"カレンダーID '{self.google_calendar_id}' が見つかりません。")
            return []
        except Exception as e:
            # HttpError以外の予期せぬエラー
            logging.error(f"Google Calendarからの予定取得中に予期せぬエラーが発生: {e}", exc_info=True)
            return []


    async def set_highlight_on_calendar(self, highlight_text: str, interaction: discord.Interaction) -> bool:
        """指定されたテキストに一致する予定をハイライトする (エラーハンドリング強化)"""
        if not self.calendar_service:
             logging.warning("Cannot set highlight: Calendar service is not available.")
             await interaction.followup.send("❌ カレンダー機能が利用できません (API認証エラー)。", ephemeral=True)
             return False
        try:
            events = await self._get_todays_events() # 今日のイベントを再取得
            target_event = None
            for event in events:
                # 完全一致で検索
                if event.get('summary') == highlight_text:
                    # 既にハイライト済みでないか確認
                    if not event.get('summary', '').startswith(HIGHLIGHT_EMOJI):
                        target_event = event
                    else:
                         logging.info(f"Event '{highlight_text}' is already highlighted.")
                         # 既にハイライト済みでも成功として扱う（ユーザーには通知済み）
                         await interaction.followup.send(f"✅ ハイライト「**{highlight_text}**」は既に設定されています。", ephemeral=True)
                         return True # ここでTrueを返して終了
                    break

            today_str = date.today().isoformat()
            operation_type = "更新" if target_event else "新規作成"
            logging.info(f"Attempting to {operation_type} highlight: '{highlight_text}'")

            if target_event:
                # 既存の予定を更新
                updated_body = {
                    'summary': f"{HIGHLIGHT_EMOJI} {target_event['summary']}",
                    'colorId': '5' # 黄色 (Google Calendarのデフォルト色ID)
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
                # 新しい終日予定としてハイライトを作成
                event_body = {
                    'summary': f"{HIGHLIGHT_EMOJI} {highlight_text}",
                    'start': {'date': today_str},
                    'end': {'date': today_str},
                    'colorId': '5' # 黄色
                }
                await asyncio.to_thread(
                    self.calendar_service.events().insert(
                        calendarId=self.google_calendar_id,
                        body=event_body
                    ).execute
                )
                logging.info(f"Successfully inserted new all-day event as highlight: '{highlight_text}'")

            return True # 正常終了

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

    @tasks.loop(time=PLANNING_PROMPT_TIME)
    async def daily_planning_task(self):
        """朝の計画を促すタスク (エラーハンドリング追加)"""
        logging.info("Executing daily_planning_task...")
        if not self.is_ready:
             logging.warning("JournalCog is not ready, skipping daily_planning_task.")
             return
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
             logging.error(f"Planning prompt channel (ID: {self.channel_id}) not found.")
             return

        try:
            self.idle_reminders_sent.clear() # アイドルリマインダー履歴をクリア
            view = discord.ui.View(timeout=7200) # 2時間
            button = discord.ui.Button(label="1日の計画を立てる", style=discord.ButtonStyle.success, custom_id="plan_day")

            async def planning_callback(interaction: discord.Interaction):
                logging.info(f"Planning button clicked by {interaction.user}")
                try:
                    await interaction.response.send_modal(ScheduleInputModal(self))
                    # モーダル表示成功後、元のメッセージを編集
                    if interaction.message:
                         # ボタンを無効化するなどしても良い
                         await interaction.message.edit(content="計画を入力中です...", view=None)
                except Exception as e_modal:
                     logging.error(f"Error sending ScheduleInputModal: {e_modal}", exc_info=True)
                     if not interaction.response.is_done():
                         try:
                             await interaction.response.send_message(f"❌ 計画入力モーダルの表示に失敗しました: {e_modal}", ephemeral=True)
                         except discord.InteractionResponded:
                              await interaction.followup.send(f"❌ 計画入力モーダルの表示に失敗しました: {e_modal}", ephemeral=True)
                     else: # 既に defer されている場合など
                         await interaction.followup.send(f"❌ 計画入力モーダルの表示に失敗しました: {e_modal}", ephemeral=True)

            button.callback = planning_callback
            view.add_item(button)
            await channel.send("おはようございます！☀️ 有意義な一日を過ごすために、まず1日の計画を立てませんか？", view=view)
            logging.info("Planning prompt sent successfully.")
        except Exception as e:
            logging.error(f"Error in daily_planning_task loop: {e}", exc_info=True)


    async def _ask_for_highlight(self, channel: discord.TextChannel):
        """ハイライトを尋ねる処理 (エラーハンドリング追加)"""
        logging.info("Asking for highlight...")
        if not self.is_ready:
             logging.warning("JournalCog is not ready, skipping _ask_for_highlight.")
             return
        await asyncio.sleep(2) # 登録処理の完了を待つ意図
        try:
            events = await self._get_todays_events()
            # 既にハイライトされた予定と終日予定を除外
            event_summaries = [
                e.get('summary', '名称未設定') for e in events
                if 'dateTime' in e.get('start', {}) and not e.get('summary', '').startswith(HIGHLIGHT_EMOJI)
            ]

            description = "今日のハイライトを決めて、一日に集中する軸を作りましょう。\n\n"
            if event_summaries:
                description += "今日の予定リストからハイライトを選択するか、新しいハイライトを入力してください。"
            else:
                description += "ハイライトとして取り組みたいことを入力してください。"

            embed = discord.Embed(title=f"{HIGHLIGHT_EMOJI} 今日のハイライト決め", description=description, color=discord.Color.blue())

            # 選択肢が多すぎる場合を考慮
            event_options = [discord.SelectOption(label=s[:100], value=s[:100]) for s in event_summaries][:25] # discordの制限は25個

            view = HighlightOptionsView(self, event_options)
            await channel.send(embed=embed, view=view)
            logging.info("Highlight prompt sent successfully.")
        except Exception as e:
            logging.error(f"Error in _ask_for_highlight: {e}", exc_info=True)
            await channel.send(f"❌ ハイライト選択肢の表示中にエラーが発生しました: {e}")

    async def process_schedule(self, interaction: discord.Interaction, tasks_text: str):
        """AIにスケジュール案を作成させる処理 (エラーハンドリング強化)"""
        logging.info("Processing schedule proposal...")
        if not self.is_ready:
             logging.warning("JournalCog is not ready, skipping process_schedule.")
             await interaction.followup.send("❌ スケジュール処理機能が利用できません。", ephemeral=True)
             return

        try:
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

            response = await self.gemini_model.generate_content_async(prompt)

            # --- JSON抽出処理の改善 ---
            json_text = ""
            if response and hasattr(response, 'text'):
                 # ```json ... ``` または ``` ... ``` ブロックを探す
                 code_block_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', response.text, re.DOTALL)
                 if code_block_match:
                     json_text = code_block_match.group(1)
                 else:
                     # コードブロックがない場合、単純に最初に見つかったリストを探す
                     list_match = re.search(r'(\[.*?\])', response.text, re.DOTALL)
                     if list_match:
                         json_text = list_match.group(1)
            # --- ここまで ---

            if not json_text:
                logging.error(f"AIからのスケジュール提案JSONの抽出に失敗。Response: {getattr(response, 'text', 'N/A')}")
                await interaction.followup.send("❌ AIによるスケジュール提案の生成に失敗しました (JSON形式エラー)。AIの応答を確認してください。", ephemeral=True)
                return

            try:
                proposed_schedule = json.loads(json_text)
                # 簡単なバリデーション (リストであり、要素が辞書であるか)
                if not isinstance(proposed_schedule, list) or not all(isinstance(item, dict) for item in proposed_schedule):
                    raise ValueError("提案形式がリストまたは辞書のリストではありません。")
                # 必須キーの存在チェック (例)
                for item in proposed_schedule:
                    if not all(key in item for key in ["summary", "start_time", "end_time"]):
                        raise ValueError("提案に必要なキー (summary, start_time, end_time) が不足しています。")

            except json.JSONDecodeError as e:
                 logging.error(f"AIからのスケジュール提案JSONのパースに失敗: {e}. JSON Text: {json_text}")
                 await interaction.followup.send(f"❌ AIスケジュール提案のJSON解析に失敗しました: {e}", ephemeral=True)
                 return
            except ValueError as e:
                 logging.error(f"AIからのスケジュール提案JSONの形式が不正: {e}. JSON Text: {json_text}")
                 await interaction.followup.send(f"❌ AIスケジュール提案の形式が不正です: {e}", ephemeral=True)
                 return


            embed = discord.Embed(title="AIによるスケジュール提案", description="AIが作成した本日のスケジュール案です。これでよろしいですか？", color=discord.Color.green())
            schedule_text = ""
            for event in proposed_schedule:
                # summaryが長すぎる場合に切り詰める
                summary_display = (event['summary'][:50] + '...') if len(event['summary']) > 53 else event['summary']
                schedule_text += f"**{summary_display}**: {event['start_time']} - {event['end_time']}\n"
            if not schedule_text:
                 schedule_text = "提案された予定はありません。"
            # Embedのフィールドではなくdescriptionに入れる (フィールド数制限回避)
            embed.description += f"\n\n{schedule_text}"

            view = ScheduleConfirmView(self, proposed_schedule)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            logging.info("Schedule proposal sent to user for confirmation.")

        except Exception as e:
            logging.error(f"Error in process_schedule: {e}", exc_info=True)
            await interaction.followup.send(f"❌ スケジュール提案の処理中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    async def register_schedule_to_calendar(self, interaction: discord.Interaction, schedule: list) -> bool:
        """提案されたスケジュールをGoogleカレンダーに一括登録する (エラーハンドリング強化)"""
        logging.info(f"Registering {len(schedule)} events to Google Calendar...")
        if not self.calendar_service:
             logging.warning("Cannot register schedule: Calendar service is not available.")
             await interaction.followup.send("❌ カレンダー機能が利用できません (API認証エラー)。", ephemeral=True)
             return False

        try:
            today = date.today()
            successful_registrations = 0
            for event in schedule:
                try:
                    start_time = datetime.strptime(event['start_time'], '%H:%M').time()
                    end_time = datetime.strptime(event['end_time'], '%H:%M').time()
                    # 終了時刻が開始時刻より早い場合は翌日扱い（例: 23:00-01:00）を防ぐため日付を確認
                    start_dt = JST.localize(datetime.combine(today, start_time))
                    end_dt = JST.localize(datetime.combine(today, end_time))
                    if end_dt <= start_dt:
                         # 終了時刻が同じか前なら、日付を跨がない限りエラーとするか、最小時間を加算
                         # ここでは単純化のため、一旦同じ日として扱う (必要なら調整)
                         logging.warning(f"Event '{event['summary']}' has end time <= start time. Treating as same day.")
                         # end_dt = start_dt + timedelta(minutes=15) # 例: 最低15分確保

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
                except ValueError as e_time:
                     logging.error(f"イベント '{event['summary']}' の時刻形式エラー: {e_time}. Start: {event.get('start_time')}, End: {event.get('end_time')}")
                     await interaction.followup.send(f"⚠️ イベント「{event['summary']}」の時刻形式 ({event.get('start_time', '')}-{event.get('end_time', '')}) が不正なため登録をスキップしました。", ephemeral=True)
                except HttpError as e_http:
                     logging.error(f"イベント '{event['summary']}' のカレンダー登録中にHttpError: {e_http}")
                     await interaction.followup.send(f"⚠️ イベント「{event['summary']}」のカレンダー登録中にAPIエラーが発生しました (HTTP {e_http.resp.status})。", ephemeral=True)
                except Exception as e_event:
                     logging.error(f"イベント '{event['summary']}' の登録中に予期せぬエラー: {e_event}")
                     await interaction.followup.send(f"⚠️ イベント「{event['summary']}」の登録中に予期せぬエラーが発生しました。", ephemeral=True)

            # 正常終了のメッセージ（一部失敗した場合も含む）
            final_message = f"✅ {successful_registrations} / {len(schedule)} 件の予定をカレンダーに登録しました。"
            if successful_registrations < len(schedule):
                 final_message += " 一部の予定の登録に失敗しました。詳細はログを確認してください。"
            await interaction.followup.send(final_message, ephemeral=True)
            logging.info(f"Finished registering schedule. {successful_registrations}/{len(schedule)} succeeded.")
            return True # 処理自体は完了した

        except Exception as e: # ループ外の予期せぬエラー
            logging.error(f"カレンダーへの一括スケジュール登録中に予期せぬエラーが発生: {e}", exc_info=True)
            await interaction.followup.send(f"❌ カレンダーへの一括登録中に予期せぬエラーが発生しました: {e}", ephemeral=True)
            return False


    # --- 夜の振り返り機能 ---
    @tasks.loop(time=JOURNAL_PROMPT_TIME)
    async def prompt_daily_journal(self):
        """夜の振り返りを促すタスク (エラーハンドリング追加)"""
        logging.info("Executing prompt_daily_journal task...")
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
                description="一日お疲れ様でした。今日一日を振り返り、ジャーナルを記録しませんか？",
                color=discord.Color.purple()
            )
            await channel.send(embed=embed, view=SimpleJournalView(self))
            logging.info("Journal prompt sent successfully.")
        except Exception as e:
            logging.error(f"Error in prompt_daily_journal loop: {e}", exc_info=True)

    async def _save_journal_entry(self, interaction: discord.Interaction, entry_text: str):
        """ジャーナルの内容をObsidianのデイリーノートに保存する (エラーハンドリング強化)"""
        logging.info("Saving journal entry to Obsidian...")
        if not self.is_ready:
             logging.warning("JournalCog is not ready, skipping _save_journal_entry.")
             await interaction.followup.send("❌ ジャーナル保存機能が利用できません。", ephemeral=True)
             return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"

        # フォーマットされたジャーナルエントリ
        journal_content = f"- {now.strftime('%H:%M')} {entry_text.strip()}"
        section_header = "## Journal" # utils/obsidian_utils.py の SECTION_ORDER と一致させる

        try:
            try:
                logging.debug(f"Downloading daily note: {daily_note_path}")
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
                logging.debug("Daily note downloaded successfully.")
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    logging.info(f"Daily note {daily_note_path} not found. Creating new file content.")
                    current_content = f"# {date_str}\n" # ファイルがなければ基本的な内容を作成
                else:
                    logging.error(f"Dropbox download error for {daily_note_path}: {e}")
                    raise # 再試行不可能なエラーとして上位に投げる

            logging.debug("Updating daily note content with journal entry.")
            new_content = update_section(current_content, journal_content, section_header)

            logging.debug(f"Uploading updated daily note: {daily_note_path}")
            self.dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"Journal entry saved successfully to Obsidian: {daily_note_path}")
            await interaction.followup.send("✅ 今日の振り返りを記録しました。", ephemeral=True)

        except ApiError as e:
             logging.error(f"Dropbox API error during journal save: {e}", exc_info=True)
             await interaction.followup.send(f"❌ Dropboxへのジャーナル保存中にAPIエラーが発生しました: {e}", ephemeral=True)
        except Exception as e:
            logging.error(f"Obsidianへのジャーナル保存中に予期せぬエラーが発生: {e}", exc_info=True)
            await interaction.followup.send(f"❌ ジャーナルの保存中に予期せぬエラーが発生しました: {e}", ephemeral=True)


    # --- 空き時間リマインダー (休日のみ) ---
    @tasks.loop(hours=IDLE_CHECK_INTERVAL_HOURS)
    async def check_idle_time_loop(self):
        """空き時間をチェックするループ (エラーハンドリング追加)"""
        logging.debug("Executing check_idle_time_loop...")
        if not self.is_ready:
             logging.warning("JournalCog is not ready, skipping check_idle_time_loop.")
             return
        if not self.calendar_service: # カレンダーサービスがなければスキップ
             logging.debug("Calendar service not available, skipping idle time check.")
             return

        try:
            now = datetime.now(JST)
            today = now.date()

            # --- 休日判定 ---
            is_weekend = today.weekday() >= 5 # 土日
            is_holiday = jpholiday.is_holiday(today)
            is_day_off = is_weekend or is_holiday
            # --- ここまで ---

            # 休日 かつ 9時から21時の間のみ実行
            if is_day_off and (9 <= now.hour < 21):
                logging.info(f"Checking for idle time on a day off ({today})...")
                events = await self._get_todays_events()
                if not events:
                     logging.info("No events found for today. Skipping idle check.")
                     return # イベントがなければチェック終了

                # dateTimeを持つイベントのみを対象とし、開始時間でソート
                sorted_events = sorted(
                    [e for e in events if 'dateTime' in e.get('start', {})],
                    key=lambda e: e['start']['dateTime']
                )

                if not sorted_events:
                     logging.info("No timed events found for today. Skipping idle check.")
                     return

                last_end_time = now # 現在時刻を開始点とする

                for event in sorted_events:
                    try:
                        # タイムゾーン情報を付与して比較できるようにする
                        start_time = datetime.fromisoformat(event['start']['dateTime']).astimezone(JST)
                        end_time = datetime.fromisoformat(event['end']['dateTime']).astimezone(JST)
                    except ValueError:
                         logging.warning(f"Failed to parse event time: {event.get('summary', 'No summary')}")
                         continue # パースできないイベントはスキップ

                    # イベントが既に終了しているか、現在進行中の場合
                    if start_time < now:
                        last_end_time = max(last_end_time, end_time)
                        continue

                    # 次のイベントまでの空き時間
                    idle_duration = start_time - last_end_time

                    # 2時間以上の空きがあるか
                    if idle_duration >= timedelta(hours=2):
                        reminder_key = f"{today.isoformat()}-{last_end_time.hour}" # 日付と開始時間でキー作成
                        if reminder_key not in self.idle_reminders_sent:
                            channel = self.bot.get_channel(self.channel_id)
                            if channel:
                                idle_hours = idle_duration.total_seconds() / 3600
                                await channel.send(
                                     f"💡 **空き時間のお知らせ**\n"
                                     f"現在、**{last_end_time.strftime('%H:%M')}** から次の予定 (**{start_time.strftime('%H:%M')}** - {event.get('summary', '')}) まで"
                                     f"**約{idle_hours:.1f}時間**の空きがあります。何か予定を入れませんか？"
                                 )
                                self.idle_reminders_sent.add(reminder_key)
                                logging.info(f"Idle time reminder sent for {reminder_key}.")
                            else:
                                 logging.error("Cannot send idle reminder: Channel not found.")
                        else:
                             logging.debug(f"Idle reminder already sent for {reminder_key}.")

                    # 次のループのために、このイベントの終了時刻を記録
                    last_end_time = max(last_end_time, end_time)

            else:
                 logging.debug(f"Not a day off or outside of active hours. Skipping idle time check. (Day: {today.weekday()}, Holiday: {is_holiday}, Hour: {now.hour})")

        except Exception as e:
            logging.error(f"Error in check_idle_time_loop: {e}", exc_info=True)

    # タスクループの開始前にBotの準備を待つデコレータを追加
    @daily_planning_task.before_loop
    @prompt_daily_journal.before_loop
    @check_idle_time_loop.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()
        logging.info("Bot is ready, tasks can now run.")


async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))