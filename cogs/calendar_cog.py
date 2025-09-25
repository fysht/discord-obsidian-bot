import os
import json
import logging
import asyncio
from datetime import datetime, time, timedelta, timezone
import re
from typing import Optional

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

# --- 定数定義 ---
JST = timezone(timedelta(hours=+9), 'JST')
TODAY_SCHEDULE_TIME = time(hour=7, minute=0, tzinfo=JST)
DAILY_REVIEW_TIME = time(hour=21, minute=30, tzinfo=JST)
MEMO_TO_CALENDAR_EMOJI = '📅'
CONFIRM_EMOJI = '👍'
CANCEL_EMOJI = '👎'

# Google Calendar APIのスコープ (読み書き可能な権限)
SCOPES = ['https://www.googleapis.com/auth/calendar']

# --- 作業時間帯のデフォルト設定 ---
WORK_START_HOUR = 8
WORK_END_HOUR = 22
MIN_TASK_DURATION_MINUTES = 10 # 最低確保するタスク時間

class CalendarCog(commands.Cog):
    """
    Googleカレンダーと連携し、タスク管理を自動化するCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.uncompleted_tasks = {} # { task_summary: original_date }
        self.pending_schedules = {}
        self.pending_date_prompts = {} # { original_message_id: {"task_analysis": ..., "prompt_msg_id": ...} }


        if not self._are_credentials_valid():
            logging.error("CalendarCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return
        try:
            # --- サーバー用の認証ロジック ---
            self.creds = self._get_google_credentials()
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    try:
                        self.creds.refresh(Request())
                        self._save_google_credentials(self.creds)
                        logging.info("Google APIのアクセストークンをリフレッシュしました。")
                    except RefreshError as e:
                        logging.error(f"❌ Google APIトークンのリフレッシュに失敗: {e}")
                        logging.error("-> token.jsonを再生成し、サーバーにアップロードする必要があります。")
                        return # is_readyをFalseのまま終了
                else:
                    logging.error("❌ Google Calendarの有効な認証情報(token.json)が見つかりません。")
                    return # is_readyをFalseのまま終了
            # --- ここまで ---

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
            if not self.notify_today_events.is_running(): self.notify_today_events.start()
            if not self.send_daily_review.is_running(): self.send_daily_review.start()

    def cog_unload(self):
        if self.is_ready:
            self.notify_today_events.cancel()
            self.send_daily_review.cancel()

    async def _handle_memo_reaction(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != MEMO_TO_CALENDAR_EMOJI: return
        
        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        
        try:
            message = await channel.fetch_message(payload.message_id)
            if not message.content: return
            
            await message.add_reaction("⏳")
            
            task_analysis = await self._analyze_task_with_ai(message.content)

            if not task_analysis:
                await message.reply("❌ AIによるタスク分析に失敗しました。フォールバックとして終日予定で登録します。", delete_after=60)
                await self._schedule_as_all_day_task(message, message.content, datetime.now(JST).date())
                return

            target_date_str = task_analysis.get("target_date")
            if target_date_str:
                try:
                    target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
                    await self._continue_scheduling(message, task_analysis, target_date)
                except (ValueError, TypeError):
                    await message.reply(f"❌ AIが日付 `{target_date_str}` を認識しましたが、形式が不正です。処理を中断します。", delete_after=60)
                    await message.remove_reaction("⏳", self.bot.user)
                    await message.add_reaction("❌")
            else:
                prompt_msg = await message.reply(f"{message.author.mention} いつタスクを登録しますか？ (例: 明日, 10/25, 来週の月曜日)")
                self.pending_date_prompts[message.id] = {
                    "task_analysis": task_analysis,
                    "prompt_msg_id": prompt_msg.id,
                    "author_id": message.author.id
                }
                await message.remove_reaction("⏳", self.bot.user)

        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"[CalendarCog] AIスケジューリング処理中にエラー: {e}", exc_info=True)
            if 'message' in locals():
                await message.reply(f"❌ スケジュール処理中にエラーが発生しました: {e}", delete_after=60)
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
    
    async def _continue_scheduling(self, message: discord.Message, task_analysis: dict, target_date: datetime.date):
        """日付が確定した後のスケジューリング処理を続行する"""
        try:
            free_slots = await self._find_free_slots(target_date)

            if task_analysis.get("decomposable", "No") == "Yes":
                await self._propose_decomposed_schedule(message, task_analysis, free_slots, target_date)
            else:
                await self._schedule_simple_task(message, task_analysis, free_slots, target_date)
        except Exception as e:
             logging.error(f"[CalendarCog] スケジュール継続処理中にエラー: {e}", exc_info=True)
             await message.reply(f"❌ スケジューリング処理中にエラーが発生しました: {e}", delete_after=60)

    async def _parse_date_from_text(self, text: str) -> Optional[datetime.date]:
        """AIを使ってテキストから日付を解析する"""
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        prompt = f"""
        ユーザーが入力した以下のテキストから日付を読み取り、`YYYY-MM-DD` 形式で出力してください。
        今日の日付は `{today_str}` です。
        日付が読み取れない場合は `null` とだけ出力してください。
        JSONやマークダウンは含めず、日付文字列またはnullのみを出力してください。
        ---
        テキスト: {text}
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            date_str = response.text.strip()
            if date_str and date_str.lower() != 'null':
                return datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError, Exception) as e:
            logging.error(f"AIによる日付解析に失敗: {e}")
        return None

    async def _analyze_task_with_ai(self, task_content: str) -> dict | None:
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        prompt = f"""
        あなたは優秀なプロジェクトマネージャーです。以下のユーザーからのタスクメモを分析し、指定された日付とタスク内容を抽出してください。

        # 指示
        1.  まず、メモから日付に関する記述を探してください。日付は「9/21」「9月21日」「明日」「あさって」など、あらゆる形式が考えられます。今日の日付は `{today_str}` です。
        2.  日付の記述が見つかった場合は、その日付を必ず `YYYY-MM-DD` 形式に変換してください。見つからない場合は `null` としてください。
        3.  次に、日付に関する記述を除いた残りのテキストをタスクリストとして解釈してください。
        4.  タスクリストを分析し、複数の具体的な実行ステップに分割すべきか、単一のタスクかを判断してください。
        5.  最終的に、以下のJSON形式で出力してください。JSON以外の説明や前置きは一切含めないでください。
        6.  各タスクの所要時間は現実的な分単位で、最低でも{MIN_TASK_DURATION_MINUTES}分としてください。

        # 出力フォーマット
        ## タスクが複雑で、分割すべき場合:
        ```json
        {{
          "target_date": "YYYY-MM-DD" or null,
          "decomposable": "Yes",
          "subtasks": [
            {{ "summary": "（サブタスク1の要約）", "duration_minutes": （所要時間） }},
            {{ "summary": "（サブタスク2の要約）", "duration_minutes": （所要時間） }}
          ]
        }}
        ```
        ## タスクがシンプルで、分割不要な場合:
        ```json
        {{
          "target_date": "YYYY-MM-DD" or null,
          "decomposable": "No",
          "summary": "（タスク全体の要約）",
          "duration_minutes": （所要時間）
        }}
        ```
        ---
        # タスクメモ
        {task_content}
        ---
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
            
            work_start_time = start_of_day.replace(hour=WORK_START_HOUR)
            work_end_time = start_of_day.replace(hour=WORK_END_HOUR)
            
            free_slots = []
            current_time = work_start_time
            while current_time < work_end_time:
                is_in_busy_slot = False
                for start, end in busy_slots:
                    if start <= current_time < end:
                        current_time = end
                        is_in_busy_slot = True
                        break
                
                if not is_in_busy_slot:
                    slot_start = current_time
                    slot_end = work_end_time
                    for start, _ in busy_slots:
                        if start > slot_start:
                            slot_end = min(slot_end, start)
                            break
                    if slot_start < slot_end:
                      free_slots.append((slot_start, slot_end))
                    current_time = slot_end
            return free_slots
        except HttpError as e:
            logging.error(f"Googleカレンダーからの予定取得中にエラー: {e}")
            return []
            
    async def _propose_decomposed_schedule(self, message: discord.Message, analysis: dict, free_slots: list, target_date: datetime.date):
        subtasks = analysis["subtasks"]
        total_duration = sum(task['duration_minutes'] for task in subtasks)

        best_slot_start = next((start for start, end in free_slots if (end - start) >= timedelta(minutes=total_duration)), None)
        
        if not best_slot_start:
            summary = "\n".join([task['summary'] for task in subtasks])
            await self._schedule_as_all_day_task(message, summary, target_date)
            return
            
        proposal_text = f"AIが **{target_date.strftime('%Y年%m月%d日')}** のタスクを以下のように分割しました。この内容でスケジュールしますか？\n\n"
        current_time = best_slot_start
        scheduled_tasks = []
        for task in subtasks:
            end_time = current_time + timedelta(minutes=task['duration_minutes'])
            proposal_text += f"- **{current_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}** {task['summary']}\n"
            scheduled_tasks.append({'summary': task['summary'], 'start': current_time.isoformat(), 'end': end_time.isoformat()})
            current_time = end_time
        
        proposal_msg = await message.reply(proposal_text)
        await proposal_msg.add_reaction(CONFIRM_EMOJI)
        await proposal_msg.add_reaction(CANCEL_EMOJI)
        self.pending_schedules[proposal_msg.id] = scheduled_tasks
        await message.remove_reaction("⏳", self.bot.user)

    async def _schedule_simple_task(self, message: discord.Message, analysis: dict, free_slots: list, target_date: datetime.date):
        duration = analysis['duration_minutes']
        summary = analysis['summary']

        best_slot_start = next((start for start, end in free_slots if (end - start) >= timedelta(minutes=duration)), None)

        if not best_slot_start:
            await self._schedule_as_all_day_task(message, summary, target_date)
            return

        start_time = best_slot_start
        end_time = start_time + timedelta(minutes=duration)
        event = {
            'summary': summary,
            'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        }
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            await message.reply(f"✅ **{target_date.strftime('%m/%d')} {start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}** に「{summary}」を登録しました。", delete_after=60)
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
        except HttpError as e:
            await message.reply(f"❌ Googleカレンダーへの登録中にエラーが発生しました: {e}", delete_after=60)
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("❌")

    async def _schedule_as_all_day_task(self, message: discord.Message, summary: str, target_date: datetime.date):
        """タスクを指定された日の終日予定として登録するフォールバック関数"""
        try:
            end_date = target_date + timedelta(days=1)
            event = {
                'summary': summary,
                'start': {'date': target_date.isoformat()},
                'end': {'date': end_date.isoformat()},
            }
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            
            if target_date == datetime.now(JST).date():
                reply_text = f"💬 今日の作業時間内に最適な空き時間が見つからなかったため、終日予定として「{summary}」を登録しました。"
            else:
                reply_text = f"💬 **{target_date.strftime('%Y年%m月%d日')}** の作業時間内に最適な空き時間が見つからなかったため、終日予定として「{summary}」を登録しました。"

            await message.reply(reply_text, delete_after=60)
            logging.info(f"[CalendarCog] タスクを終日予定として登録しました: '{summary}' on {target_date}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")

        except HttpError as e:
            await message.reply(f"❌ 終日予定としてカレンダー登録中にエラーが発生しました: {e}", delete_after=60)
            logging.error(f"Googleカレンダーへの終日イベント作成中にエラー: {e}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("❌")

    async def _handle_proposal_reaction(self, payload: discord.RawReactionActionEvent):
        if payload.message_id not in self.pending_schedules: return
        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        
        try:
            proposal_msg = await channel.fetch_message(payload.message_id)
            if str(payload.emoji) == CONFIRM_EMOJI:
                tasks_to_schedule = self.pending_schedules.pop(payload.message_id)
                service = build('calendar', 'v3', credentials=self.creds)
                for task in tasks_to_schedule:
                    event = {'summary': task['summary'], 'start': {'dateTime': task['start'], 'timeZone': 'Asia/Tokyo'}, 'end': {'dateTime': task['end'], 'timeZone': 'Asia/Tokyo'}}
                    service.events().insert(calendarId='primary', body=event).execute()
                    await asyncio.sleep(0.5)
                await proposal_msg.edit(content="✅ スケジュールをGoogleカレンダーに登録しました。", embed=None, view=None)
                await asyncio.sleep(10)
                await proposal_msg.delete()
                original_message = await channel.fetch_message(proposal_msg.reference.message_id)
                await original_message.add_reaction("✅")
            elif str(payload.emoji) == CANCEL_EMOJI:
                del self.pending_schedules[payload.message_id]
                await proposal_msg.edit(content="❌ スケジュール登録をキャンセルしました。", embed=None, view=None)
                await asyncio.sleep(10)
                await proposal_msg.delete()
            await proposal_msg.clear_reactions()
        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"スケジュール提案リアクションの処理中にエラー: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot: return
        
        if message.reference and message.reference.message_id:
            pending_item = next((item for item in self.pending_date_prompts.values() if item["prompt_msg_id"] == message.reference.message_id), None)
            if pending_item and message.author.id == pending_item["author_id"]:
                original_message_id = next(key for key, val in self.pending_date_prompts.items() if val == pending_item)
                
                try:
                    await message.add_reaction("⏳")
                    target_date = await self._parse_date_from_text(message.content)
                    
                    if target_date:
                        task_analysis = pending_item["task_analysis"]
                        original_message = await message.channel.fetch_message(original_message_id)
                        
                        prompt_msg_to_delete = await message.channel.fetch_message(pending_item["prompt_msg_id"])

                        del self.pending_date_prompts[original_message_id]
                        await message.channel.delete_messages([message, prompt_msg_to_delete])
                        
                        await self._continue_scheduling(original_message, task_analysis, target_date)
                    else:
                        await message.reply("日付を認識できませんでした。もう一度入力してください。（例：明日、10/25）", delete_after=30)
                        await message.remove_reaction("⏳", self.bot.user)

                except discord.NotFound:
                    logging.warning("日付指定の返信処理中にメッセージが削除されました。")
                except Exception as e:
                    logging.error(f"日付指定の返信処理中にエラー: {e}", exc_info=True)
                    try:
                        await message.remove_reaction("⏳", self.bot.user)
                    except discord.NotFound:
                        pass
                return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not self.is_ready or payload.user_id == self.bot.user.id: return
        if payload.channel_id == self.memo_channel_id:
            if payload.message_id in self.pending_schedules:
                await self._handle_proposal_reaction(payload)
            else:
                await self._handle_memo_reaction(payload)
        elif payload.channel_id == self.calendar_channel_id:
            await self._handle_calendar_reaction(payload)
    
    async def _handle_calendar_reaction(self, payload: discord.RawReactionActionEvent):
        try:
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if message.author.id != self.bot.user.id or not message.embeds: return
            
            embed = message.embeds[0]
            if not embed.title or not embed.title.startswith("タスク: "): return

            task_summary = embed.title.replace("タスク: ", "")
            date_str_match = re.search(r'(\d{4}-\d{2}-\d{2})', embed.footer.text or '')
            if not date_str_match:
                target_date = message.created_at.astimezone(JST).date()
            else:
                target_date = datetime.strptime(date_str_match.group(1), '%Y-%m-%d').date()

            if str(payload.emoji) == '❌':
                self.uncompleted_tasks[task_summary] = target_date
                logging.info(f"[CalendarCog] 未完了タスクを追加: {task_summary} (期日: {target_date})")

            task_list_md = f"- [{ 'x' if str(payload.emoji) == '✅' else ' ' }] {task_summary}\n"
            await self._update_obsidian_task_log(target_date, task_list_md)
            
            user = self.bot.get_user(payload.user_id)
            feedback_msg_content = f"「{task_summary}」のフィードバックありがとうございます！"
            if user:
                feedback_msg_content = f"{user.mention}さん、{feedback_msg_content}"
            
            await channel.send(feedback_msg_content, delete_after=10)
            await message.delete()
        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"[CalendarCog] カレンダーリアクション処理中にエラー: {e}", exc_info=True)

    async def _create_google_calendar_event(self, summary: str, date: datetime.date):
        end_date = date + timedelta(days=1)
        event = {'summary': summary, 'start': {'date': date.isoformat()}, 'end': {'date': end_date.isoformat()}}
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            logging.info(f"[CalendarCog] Googleカレンダーに終日予定を追加しました: '{summary}' on {date}")
        except HttpError as e:
            logging.error(f"Googleカレンダーへのイベント作成中にエラー: {e}")

    @tasks.loop(time=TODAY_SCHEDULE_TIME)
    async def notify_today_events(self):
        if not self.is_ready: return
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
            channel = self.bot.get_channel(self.calendar_channel_id)
            if channel: await channel.send(embed=embed)
            for event in events: await self._add_to_daily_log(event)
        except Exception as e:
            logging.error(f"[CalendarCog] 今日の予定通知中にエラー: {e}", exc_info=True)

    @tasks.loop(time=DAILY_REVIEW_TIME)
    async def send_daily_review(self):
        if not self.is_ready: return
        try:
            today_str = datetime.now(JST).strftime('%Y-%m-%d')
            log_path = f"{self.dropbox_vault_path}/.bot/calendar_log/{today_str}.json"
            try:
                _, res = self.dbx.files_download(log_path)
                daily_events = json.loads(res.content.decode('utf-8'))
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    await self._carry_over_uncompleted_tasks()
                    return
                raise
            if not daily_events: 
                await self._carry_over_uncompleted_tasks()
                return
            channel = self.bot.get_channel(self.calendar_channel_id)
            if channel:
                await channel.send(f"--- **🗓️ {today_str} のタスクレビュー** ---\nお疲れ様でした！今日のタスクの達成度をリアクションで教えてください。", delete_after=3600) # 1時間後に削除
                for event in daily_events:
                    embed = discord.Embed(title=f"タスク: {event['summary']}", color=discord.Color.gold())
                    embed.set_footer(text=f"Task for: {today_str}")
                    message = await channel.send(embed=embed)
                    await message.add_reaction("✅")
                    await message.add_reaction("❌")
                await channel.send("--------------------", delete_after=3600) # 1時間後に削除
            await self._carry_over_uncompleted_tasks()
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
            _, res = self.dbx.files_download(log_path)
            daily_events = json.loads(res.content.decode('utf-8'))
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                daily_events = []
            else:
                logging.error(f"デイリーログの読み込みに失敗: {e}")
                return
        if not any(e['id'] == event['id'] for e in daily_events):
            daily_events.append({'id': event['id'], 'summary': event.get('summary', '名称未設定')})
            try:
                self.dbx.files_upload(json.dumps(daily_events, indent=2, ensure_ascii=False).encode('utf-8'), log_path, mode=WriteMode('overwrite'))
            except Exception as e:
                logging.error(f"デイリーログの保存に失敗: {e}")
            
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