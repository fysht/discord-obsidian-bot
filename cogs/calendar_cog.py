import os
import json
import logging
import asyncio
from datetime import datetime, time, timedelta, timezone
import re

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
CONFIRM_EMOJI = '👍'
CANCEL_EMOJI = '👎'

# Google Calendar APIのスコープ
SCOPES = ['https://www.googleapis.com/auth/calendar'] 

# --- 作業時間帯のデフォルト設定 ---
WORK_START_HOUR = 9
WORK_END_HOUR = 18
MIN_TASK_DURATION_MINUTES = 15 # 最低確保するタスク時間

class CalendarCog(commands.Cog):
    """
    Googleカレンダーと連携し、タスク管理を自動化するCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.uncompleted_tasks = []
        # タスク提案を一時的に保存する辞書
        self.pending_schedules = {}

        if not self._are_credentials_valid():
            logging.error("CalendarCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return
        try:
            self.creds = self._get_google_credentials()
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    self.creds.refresh(Request())
                    self._save_google_credentials(self.creds) # 更新された認証情報を保存
                    logging.info("Google APIのアクセストークンをリフレッシュしました。")
                else:
                    raise Exception("Google Calendarの認証情報が無効です。")

            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") 
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
        if not os.path.exists(self.google_token_path):
            logging.error(f"Googleの認証ファイルが見つかりません。パス: {self.google_token_path}")
            return None
        try:
            return Credentials.from_authorized_user_file(self.google_token_path, SCOPES)
        except Exception as e:
            logging.error(f"認証ファイルからの認証情報読み込みに失敗しました: {e}")
            return None
    
    def _save_google_credentials(self, creds):
        """更新された認証情報をファイルに保存する"""
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
        self.notify_today_events.cancel()
        self.send_daily_review.cancel()


    # --- ここからがAIスケジューリング機能のコアロジック ---

    async def _handle_memo_reaction(self, payload: discord.RawReactionActionEvent):
        """メモへのリアクションをトリガーにAIスケジューリングを開始する"""
        if str(payload.emoji) != MEMO_TO_CALENDAR_EMOJI: return
        
        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        
        try:
            message = await channel.fetch_message(payload.message_id)
            if not message.content: return
            
            await message.add_reaction("⏳")

            # 1. AIによるタスク分析
            task_analysis = await self._analyze_task_with_ai(message.content)

            if not task_analysis:
                await message.reply("❌ AIによるタスク分析に失敗しました。メモの内容を確認してください。")
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
                return

            target_date = datetime.now(JST).date()

            # 2. カレンダーの空き時間を見つける
            free_slots = await self._find_free_slots(target_date)

            # 3. AIの分析結果に基づいてスケジュールを決定
            if task_analysis.get("decomposable", "No") == "Yes":
                # タスク分割が必要な場合
                await self._propose_decomposed_schedule(message, task_analysis, free_slots)
            else:
                # シンプルなタスクの場合
                await self._schedule_simple_task(message, task_analysis, free_slots)

        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"[CalendarCog] AIスケジューリング処理中にエラー: {e}", exc_info=True)
            if 'message' in locals():
                await message.reply(f"❌ スケジュール処理中にエラーが発生しました: {e}")
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")

    async def _analyze_task_with_ai(self, task_content: str) -> dict | None:
        """AIを使ってタスクを分析し、分割要否と所要時間を推定する"""
        prompt = f"""
        あなたは優秀なプロジェクトマネージャーです。以下のユーザーからのタスクメモを分析してください。

        # 指示
        1.  このタスクが複数の具体的な実行ステップに分割すべき複雑なものか、それとも単一のタスクかを判断してください。
        2.  判断結果に基づき、以下のJSON形式で出力してください。
        3.  JSON以外の説明や前置きは一切含めないでください。
        4.  各タスクの所要時間は現実的な分単位で、最低でも{MIN_TASK_DURATION_MINUTES}分としてください。

        # 出力フォーマット
        ## タスクが複雑で、分割すべき場合:
        {{
          "decomposable": "Yes",
          "subtasks": [
            {{ "summary": "（サブタスク1の要約）", "duration_minutes": （所要時間） }},
            {{ "summary": "（サブタスク2の要約）", "duration_minutes": （所要時間） }}
          ]
        }}

        ## タスクがシンプルで、分割不要な場合:
        {{
          "decomposable": "No",
          "summary": "（タスク全体の要約）",
          "duration_minutes": （所要時間）
        }}
        
        ---
        # タスクメモ
        {task_content}
        ---
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            # AIの出力からJSON部分だけを抽出する
            json_match = re.search(r'```json\n(\{.*?\})\n```', response.text, re.DOTALL)
            if json_match:
                json_text = json_match.group(1)
            else: # バッククォートがない場合も考慮
                json_text = response.text
            
            return json.loads(json_text)
        except Exception as e:
            logging.error(f"AIタスク分析のJSON解析に失敗: {e}\nAI Response: {response.text}")
            return None

    async def _find_free_slots(self, target_date: datetime.date) -> list:
        """指定された日の作業時間帯における空き時間（スロット）を見つける"""
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            
            start_of_day = datetime.combine(target_date, time(0, 0), tzinfo=JST)
            end_of_day = start_of_day + timedelta(days=1)

            events_result = service.events().list(
                calendarId='primary', 
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True, 
                orderBy='startTime'
            ).execute()
            
            busy_slots = []
            for event in events_result.get('items', []):
                start_str = event['start'].get('dateTime', event['start'].get('date'))
                end_str = event['end'].get('dateTime', event['end'].get('date'))
                
                # 終日予定は無視
                if 'T' not in start_str or 'T' not in end_str:
                    continue
                
                busy_slots.append((
                    datetime.fromisoformat(start_str),
                    datetime.fromisoformat(end_str)
                ))
            
            work_start_time = start_of_day.replace(hour=WORK_START_HOUR)
            work_end_time = start_of_day.replace(hour=WORK_END_HOUR)
            
            free_slots = []
            current_time = work_start_time

            while current_time < work_end_time:
                is_in_busy_slot = False
                for start, end in busy_slots:
                    if start <= current_time < end:
                        current_time = end # 忙しい時間帯なら、その終了時刻までスキップ
                        is_in_busy_slot = True
                        break
                
                if not is_in_busy_slot:
                    slot_start = current_time
                    slot_end = work_end_time # デフォルトの終了時刻

                    # 次の予定の開始時刻を探す
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
            
    async def _propose_decomposed_schedule(self, message: discord.Message, analysis: dict, free_slots: list):
        """分割されたタスクのスケジュール案をユーザーに提案する"""
        subtasks = analysis["subtasks"]
        total_duration = sum(task['duration_minutes'] for task in subtasks)

        best_slot_start = None
        for start, end in free_slots:
            if (end - start) >= timedelta(minutes=total_duration):
                best_slot_start = start
                break
        
        if not best_slot_start:
            await message.reply(f"❌ タスクを完了するための十分な時間（合計{total_duration}分）が今日の作業時間内に見つかりませんでした。")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("❌")
            return
            
        proposal_text = "AIがタスクを以下のように分割しました。この内容でスケジュールしますか？\n\n"
        current_time = best_slot_start
        scheduled_tasks = []
        for task in subtasks:
            end_time = current_time + timedelta(minutes=task['duration_minutes'])
            proposal_text += f"- **{current_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}** {task['summary']}\n"
            scheduled_tasks.append({
                'summary': task['summary'],
                'start': current_time.isoformat(),
                'end': end_time.isoformat()
            })
            current_time = end_time

        proposal_msg = await message.reply(proposal_text)
        await proposal_msg.add_reaction(CONFIRM_EMOJI)
        await proposal_msg.add_reaction(CANCEL_EMOJI)

        # 提案内容を一時保存
        self.pending_schedules[proposal_msg.id] = scheduled_tasks
        await message.remove_reaction("⏳", self.bot.user)


    async def _schedule_simple_task(self, message: discord.Message, analysis: dict, free_slots: list):
        """分割不要のシンプルなタスクをスケジュールする"""
        duration = analysis['duration_minutes']
        summary = analysis['summary']

        best_slot_start = None
        for start, end in free_slots:
            if (end - start) >= timedelta(minutes=duration):
                best_slot_start = start
                break

        if not best_slot_start:
            await message.reply(f"❌ タスクを完了するための十分な時間（{duration}分）が今日の作業時間内に見つかりませんでした。")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("❌")
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
            await message.reply(f"✅ **{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}** に「{summary}」を登録しました。")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
        except HttpError as e:
            await message.reply(f"❌ Googleカレンダーへの登録中にエラーが発生しました: {e}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("❌")


    async def _handle_proposal_reaction(self, payload: discord.RawReactionActionEvent):
        """スケジュール提案へのリアクションを処理する"""
        if payload.message_id not in self.pending_schedules:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        
        try:
            proposal_msg = await channel.fetch_message(payload.message_id)

            if str(payload.emoji) == CONFIRM_EMOJI:
                tasks_to_schedule = self.pending_schedules.pop(payload.message_id)
                
                service = build('calendar', 'v3', credentials=self.creds)
                for task in tasks_to_schedule:
                    event = {
                        'summary': task['summary'],
                        'start': {'dateTime': task['start'], 'timeZone': 'Asia/Tokyo'},
                        'end': {'dateTime': task['end'], 'timeZone': 'Asia/Tokyo'},
                    }
                    service.events().insert(calendarId='primary', body=event).execute()
                    await asyncio.sleep(0.5) # APIレート制限対策

                await proposal_msg.edit(content="✅ スケジュールをGoogleカレンダーに登録しました。", embed=None)
                # 元のメッセージにも完了マーク
                original_message = await channel.fetch_message(proposal_msg.reference.message_id)
                await original_message.add_reaction("✅")

            elif str(payload.emoji) == CANCEL_EMOJI:
                del self.pending_schedules[payload.message_id]
                await proposal_msg.edit(content="❌ スケジュール登録をキャンセルしました。", embed=None)

            await proposal_msg.clear_reactions()

        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"スケジュール提案リアクションの処理中にエラー: {e}", exc_info=True)


    # --- 既存のCogリスナーの修正 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        # チャンネルごとに処理を振り分け
        if payload.channel_id == self.memo_channel_id:
            # 提案メッセージへのリアクションか、新規メモへのリアクションかを判断
            if payload.message_id in self.pending_schedules:
                await self._handle_proposal_reaction(payload)
            else:
                await self._handle_memo_reaction(payload)
        
        elif payload.channel_id == self.calendar_channel_id:
            await self._handle_calendar_reaction(payload)

    # ... (以降、既存の _handle_calendar_reaction, notify_today_events, send_daily_review, _carry_over_uncompleted_tasks などはそのまま or 必要に応じて微修正) ...
    # 変更点：_create_google_calendar_eventは終日タスク作成用として残し、時間指定タスクは新ロジックで作成
    
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
            
            await channel.send(feedback_msg_content, delete_after=10)
            await message.delete(delay=10)

        except (discord.NotFound, discord.Forbidden): pass
        except Exception as e:
            logging.error(f"[CalendarCog] カレンダーリアクション処理中にエラー: {e}", exc_info=True)

    async def _create_google_calendar_event(self, summary: str, date: datetime.date):
        """【繰り越し用】指定された日に通知なしの終日予定を作成する"""
        end_date = date + timedelta(days=1)
        event = {
            'summary': summary,
            'start': {'date': date.isoformat()},
            'end': {'date': end_date.isoformat()},
        }
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            logging.info(f"[CalendarCog] Googleカレンダーに繰り越しタスクを追加しました: '{summary}' on {date}")
        except HttpError as e:
            logging.error(f"Googleカレンダーへのイベント作成中にエラー: {e}")

    # (notify_today_events, send_daily_review, _carry_over_uncompleted_tasks, _update_obsidian_task_log などは変更なし)
    # ... (省略) ...
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
    await bot.add_cog(CalendarCog(bot))