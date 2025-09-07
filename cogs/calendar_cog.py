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
# 各タスクの実行時刻 (JST)
TOMORROW_SCHEDULE_TIME = time(hour=21, minute=0, tzinfo=JST)
DAILY_REVIEW_TIME = time(hour=22, minute=0, tzinfo=JST)

# Google Calendar APIのスコープ
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

class CalendarCog(commands.Cog):
    """
    Googleカレンダーと連携し、タスク管理を自動化するCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False

        # --- 環境変数の読み込み ---
        self._load_environment_variables()

        if not self._are_credentials_valid():
            logging.error("CalendarCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return

        # --- APIクライアントの初期化 ---
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
        """環境変数をインスタンス変数に読み込む"""
        self.calendar_channel_id = int(os.getenv("CALENDAR_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH")
        self.google_credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        self.google_token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")

    def _are_credentials_valid(self) -> bool:
        """必須の環境変数がすべて設定されているかを確認する"""
        return all([
            self.calendar_channel_id, self.gemini_api_key, self.dropbox_refresh_token,
            self.dropbox_vault_path, self.google_credentials_path, self.google_token_path
        ])

    def _initialize_dropbox_client(self) -> dropbox.Dropbox:
        """Dropboxクライアントを初期化する"""
        return dropbox.Dropbox(
            app_key=self.dropbox_app_key,
            app_secret=self.dropbox_app_secret,
            oauth2_refresh_token=self.dropbox_refresh_token
        )

    def _get_google_credentials(self):
        """Google APIの認証情報を取得・更新する"""
        creds = None
        if os.path.exists(self.google_token_path):
            creds = Credentials.from_authorized_user_file(self.google_token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logging.error(f"Googleトークンのリフレッシュに失敗しました: {e}")
                    # 古いtoken.jsonを削除して再認証を促す
                    os.remove(self.google_token_path)
                    return self._get_google_credentials() # 再帰呼び出し
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
        """Cogの準備が完了したときにタスクを開始する"""
        if self.is_ready:
            if not self.notify_upcoming_events.is_running():
                self.notify_upcoming_events.start()
            if not self.notify_tomorrow_events.is_running():
                self.notify_tomorrow_events.start()
            if not self.send_daily_review.is_running():
                self.send_daily_review.start()

    def cog_unload(self):
        """Cogがアンロードされるときにタスクをキャンセルする"""
        self.notify_upcoming_events.cancel()
        self.notify_tomorrow_events.cancel()
        self.send_daily_review.cancel()

    # --- 1. 直近の予定のリアルタイム通知 ---
    @tasks.loop(minutes=15)
    async def notify_upcoming_events(self):
        logging.info("[CalendarCog] 15分以内の直近の予定をチェックします...")
        try:
            now = datetime.now(timezone.utc)
            time_max = now + timedelta(minutes=15)
            
            service = build('calendar', 'v3', credentials=self.creds)
            events_result = service.events().list(
                calendarId='primary', timeMin=now.isoformat(), timeMax=time_max.isoformat(),
                singleEvents=True, orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])

            if not events:
                return

            processed_ids = await self._get_processed_event_ids()
            new_events = [e for e in events if e['id'] not in processed_ids]

            if not new_events:
                return

            channel = self.bot.get_channel(self.calendar_channel_id)
            if not channel:
                logging.error(f"[CalendarCog] チャンネルID {self.calendar_channel_id} が見つかりません。")
                return

            for event in new_events:
                advice = await self._generate_event_advice(event)
                embed = self._create_event_embed(event, advice)
                await channel.send(embed=embed)
                processed_ids.add(event['id'])
                await self._add_to_daily_log(event) # 今日の通知ログに追加

            await self._save_processed_event_ids(processed_ids)

        except Exception as e:
            logging.error(f"[CalendarCog] 直近の予定通知中にエラー: {e}", exc_info=True)

    # --- 2. 明日の予定の事前通知 ---
    @tasks.loop(time=TOMORROW_SCHEDULE_TIME)
    async def notify_tomorrow_events(self):
        logging.info("[CalendarCog] 明日の予定の事前通知タスクを開始します...")
        try:
            tomorrow = datetime.now(JST).date() + timedelta(days=1)
            time_min_dt = datetime.combine(tomorrow, time.min, tzinfo=JST)
            time_max_dt = datetime.combine(tomorrow, time.max, tzinfo=JST)

            service = build('calendar', 'v3', credentials=self.creds)
            events_result = service.events().list(
                calendarId='primary', timeMin=time_min_dt.isoformat(), timeMax=time_max_dt.isoformat(),
                singleEvents=True, orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])

            if not events:
                logging.info(f"[CalendarCog] {tomorrow} の予定はありませんでした。")
                return

            advice = await self._generate_overall_advice(events)
            embed = self._create_tomorrow_embed(tomorrow, events, advice)
            
            channel = self.bot.get_channel(self.calendar_channel_id)
            if channel:
                await channel.send(embed=embed)

            await self._update_obsidian_tomorrow_task_list(tomorrow, events)
            
        except Exception as e:
            logging.error(f"[CalendarCog] 明日の予定通知中にエラー: {e}", exc_info=True)

    # --- 3. 一日の振り返り機能 ---
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
                if isinstance(e.error, DownloadError) and e.error.is_path().is_not_found():
                    logging.info(f"[CalendarCog] {today_str} の通知ログは見つかりませんでした。")
                    return
                raise

            if not daily_events:
                return

            embed = discord.Embed(
                title=f"🗓️ {today_str} のタスク一覧",
                description="お疲れ様でした！今日のタスクの達成度をリアクションで教えてください。",
                color=discord.Color.gold()
            )
            event_list = ""
            for event in daily_events:
                event_list += f"- {event['summary']}\n"
            embed.add_field(name="通知されたタスク", value=event_list, inline=False)

            channel = self.bot.get_channel(self.calendar_channel_id)
            if channel:
                message = await channel.send(embed=embed)
                await message.add_reaction("✅")
                await message.add_reaction("❌")
        except Exception as e:
            logging.error(f"[CalendarCog] 振り返り通知中にエラー: {e}", exc_info=True)

    # --- 4. 進捗の記録 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if payload.channel_id != self.calendar_channel_id:
            return

        try:
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)

            if message.author.id != self.bot.user.id or not message.embeds:
                return
            
            embed = message.embeds[0]
            if not embed.title or "のタスク一覧" not in embed.title:
                return

            date_str_match = embed.title.split(" ")[1] # "🗓️ YYYY-MM-DD のタスク一覧"
            target_date = datetime.strptime(date_str_match, '%Y-%m-%d').date()

            log_path = f"{self.dropbox_vault_path}/.bot/calendar_log/{target_date.strftime('%Y-%m-%d')}.json"
            try:
                _, res = self.dbx.files_download(log_path)
                daily_events = json.loads(res.content.decode('utf-8'))
            except ApiError:
                return # ログファイルがなければ何もしない

            task_list_md = ""
            for event in daily_events:
                task_list_md += f"- [{ 'x' if str(payload.emoji) == '✅' else ' ' }] {event['summary']}\n"

            await self._update_obsidian_task_log(target_date, task_list_md)

            # ユーザーのリアクションを消してフィードバック
            user = self.bot.get_user(payload.user_id)
            if user:
                await message.remove_reaction(payload.emoji, user)
            
            await channel.send(f"{user.mention}さん、フィードバックありがとうございます！Obsidianに記録しました。", delete_after=10)

        except (discord.NotFound, discord.Forbidden):
            pass # メッセージが削除されている場合は何もしない
        except Exception as e:
            logging.error(f"[CalendarCog] リアクション処理中にエラー: {e}", exc_info=True)


    # --- ヘルパー関数 ---

    async def _generate_event_advice(self, event: dict) -> str:
        """個別のイベントに対するAIアドバイスを生成する"""
        try:
            start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date'))
            end = event.get('end', {}).get('dateTime', event.get('end', {}).get('date'))
            
            prompt = f"""
            あなたは優秀なアシスタントです。以下の予定について、生産性を高めるための具体的なアドバイスを2〜3個、箇条書きで提案してください。

            # 予定
            - タイトル: {event.get('summary', '名称未設定')}
            - 開始時刻: {start}
            - 終了時刻: {end}
            - 説明: {event.get('description', 'なし')}
            """
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text
        except Exception as e:
            logging.error(f"イベントアドバイスの生成に失敗: {e}")
            return "アドバイスの生成中にエラーが発生しました。"
    
    async def _generate_overall_advice(self, events: list) -> str:
        """1日の予定全体に対するAIアドバイスを生成する"""
        try:
            event_list_str = ""
            for event in events:
                start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date'))
                event_list_str += f"- {start}: {event.get('summary', '名称未設定')}\n"

            prompt = f"""
            あなたは優秀な戦略的アドバイザーです。以下の明日の予定リスト全体を見て、一日を最も生産的に過ごすための総合的なアドバイス（時間の使い方、心構え、注意点など）を300字程度の文章で提案してください。

            # 明日の予定リスト
            {event_list_str}
            """
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text
        except Exception as e:
            logging.error(f"総合アドバイスの生成に失敗: {e}")
            return "アドバイスの生成中にエラーが発生しました。"

    def _create_event_embed(self, event: dict, advice: str) -> discord.Embed:
        """直近の予定通知用のEmbedを作成する"""
        start_str = self._format_datetime(event.get('start'))
        end_str = self._format_datetime(event.get('end'))

        embed = discord.Embed(
            title=f" upcoming: {event.get('summary', '名称未設定')}",
            color=discord.Color.blue()
        )
        embed.add_field(name="時間", value=f"{start_str} - {end_str}", inline=False)
        if event.get('description'):
            embed.add_field(name="説明", value=event['description'], inline=False)
        embed.add_field(name="🤖 AIからのアドバイス", value=advice, inline=False)
        return embed

    def _create_tomorrow_embed(self, date: datetime.date, events: list, advice: str) -> discord.Embed:
        """明日の予定一覧用のEmbedを作成する"""
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
        """Google CalendarのdatetimeオブジェクトをJSTのHH:MM形式に変換する"""
        if 'dateTime' in dt_obj:
            dt = datetime.fromisoformat(dt_obj['dateTime']).astimezone(JST)
            return dt.strftime('%H:%M')
        elif 'date' in dt_obj:
            return "終日"
        return ""

    async def _get_processed_event_ids(self) -> set:
        """処理済みのイベントIDをDropboxから読み込む"""
        path = f"{self.dropbox_vault_path}/.bot/processed_calendar_events.json"
        try:
            _, res = self.dbx.files_download(path)
            data = json.loads(res.content.decode('utf-8'))
            return set(data.get('processed_ids', []))
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path().is_not_found():
                return set()
            logging.error(f"処理済みイベントIDファイルの読み込みに失敗: {e}")
            return set()

    async def _save_processed_event_ids(self, ids: set):
        """処理済みのイベントIDをDropboxに保存する"""
        path = f"{self.dropbox_vault_path}/.bot/processed_calendar_events.json"
        data = {'processed_ids': list(ids)}
        try:
            self.dbx.files_upload(
                json.dumps(data, indent=2).encode('utf-8'),
                path,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"処理済みイベントIDファイルの保存に失敗: {e}")

    async def _add_to_daily_log(self, event: dict):
        """その日に通知したイベントのログをDropboxに保存する"""
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        log_path = f"{self.dropbox_vault_path}/.bot/calendar_log/{today_str}.json"
        
        try:
            _, res = self.dbx.files_download(log_path)
            daily_events = json.loads(res.content.decode('utf-8'))
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path().is_not_found():
                daily_events = []
            else:
                logging.error(f"デイリーログの読み込みに失敗: {e}")
                return

        # 簡略化したイベント情報を保存
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
            
    async def _update_obsidian_tomorrow_task_list(self, date: datetime.date, events: list):
        """明日のObsidianデイリーノートにタスクリストを書き込む"""
        date_str = date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        task_list_md = ""
        for event in events:
            task_list_md += f"- [ ] {event.get('summary', '名称未設定')}\n"
        
        try:
            # 既存のノート内容を取得
            try:
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path().is_not_found():
                    current_content = "" # ファイルがなければ新規作成
                else: raise

            new_content = update_section(current_content, task_list_md.strip(), "## Task List")

            self.dbx.files_upload(
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"Obsidianの明日のデイリーノートを更新しました: {daily_note_path}")
        except Exception as e:
            logging.error(f"Obsidianの明日のデイリーノート更新に失敗: {e}")
            
    async def _update_obsidian_task_log(self, date: datetime.date, log_content: str):
        """ObsidianのデイリーノートのTask Logセクションを更新する"""
        date_str = date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"

        try:
            try:
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path().is_not_found():
                    current_content = ""
                else: raise

            new_content = update_section(current_content, log_content.strip(), "## Task Log")

            self.dbx.files_upload(
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"Obsidianのタスクログを更新しました: {daily_note_path}")
        except Exception as e:
            logging.error(f"Obsidianタスクログの更新に失敗: {e}")


async def setup(bot: commands.Bot):
    """Cogをボットに登録するためのセットアップ関数"""
    await bot.add_cog(CalendarCog(bot))