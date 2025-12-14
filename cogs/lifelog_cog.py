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

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("LifeLogCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
ACTIVE_LOGS_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/active_lifelogs.json"
DAILY_NOTE_HEADER = "## Life Logs"
SUMMARY_NOTE_HEADER = "## Life Logs Summary"
READING_NOTES_PATH = "/Reading Notes"
DAILY_SUMMARY_TIME = time(hour=6, minute=0, tzinfo=JST) 

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
            # 確認メッセージを更新して「開始」状態にする
            await interaction.edit_original_response(content=f"✅ タスク「**{self.task_name}**」の計測を開始します（予定: {self.duration}分）。", view=None)
        except: pass
        
        await self.cog.switch_task(self.original_message, self.task_name, self.duration)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return
        
        # キャンセル時はメッセージ自体を削除する
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except: pass
        self.stop()

    async def on_timeout(self):
        try:
            if self.bot_response_message:
                await self.bot_response_message.edit(content=f"✅ (自動開始) タスク「**{self.task_name}**」の計測を開始します（予定: {self.duration}分）。", view=None)
        except: pass
        
        await self.cog.switch_task(self.original_message, self.task_name, self.duration)

class LifeLogScheduleStartView(discord.ui.View):
    def __init__(self, cog, task_name, duration=30):
        super().__init__(timeout=300)
        self.cog = cog
        self.task_name = task_name
        self.duration = duration

    @discord.ui.button(label="開始する", style=discord.ButtonStyle.success, emoji="▶️")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.cog.switch_task_from_interaction(interaction, self.task_name, self.duration)
        try:
            await interaction.edit_original_response(content=f"✅ 予定されていたタスク「**{self.task_name}**」を開始しました。", view=None)
        except: pass
        self.stop()

    @discord.ui.button(label="見送る", style=discord.ButtonStyle.secondary, emoji="👋")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response() # メッセージ削除
        except: pass
        self.stop()

class LifeLogBookSelectView(discord.ui.View):
    def __init__(self, cog, book_options: list[discord.SelectOption], original_author: discord.User, duration: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.original_author = original_author
        self.duration = duration
        
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
        
        await interaction.response.edit_message(content=f"📖 書籍を選択しました: **{task_name}**（予定: {self.duration}分）", view=None, embed=None)
        await self.cog.switch_task_from_interaction(interaction, task_name, self.duration)
        self.stop()

class LifeLogPlanSelectView(discord.ui.View):
    def __init__(self, cog, task_options: list[str], original_author: discord.User):
        super().__init__(timeout=60)
        self.cog = cog
        self.original_author = original_author
        
        options = []
        for task in task_options[:25]:
            label = task[:100]
            options.append(discord.SelectOption(label=label, value=label))

        select = discord.ui.Select(
            placeholder="開始する計画タスクを選択...",
            options=options,
            custom_id="lifelog_plan_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.original_author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return

        selected_task = interaction.data["values"][0]
        task_name, duration = self.cog._parse_task_and_duration(selected_task)
        
        await interaction.response.edit_message(content=f"📅 計画から開始: **{task_name}**（予定: {duration}分）", view=None, embed=None)
        await self.cog.switch_task_from_interaction(interaction, task_name, duration)
        self.stop()

class LifeLogTimeUpView(discord.ui.View):
    def __init__(self, cog, user_id: str, task_name: str, alert_message: discord.Message = None):
        super().__init__(timeout=None) # タイムアウトはタスク側で管理
        self.cog = cog
        self.user_id = user_id
        self.task_name = task_name
        self.alert_message = alert_message # 自身（アラートメッセージ）への参照

    async def _delete_alert(self):
        """アラートメッセージを削除する"""
        if self.alert_message:
            try:
                await self.alert_message.delete()
            except: pass

    @discord.ui.button(label="延長する (+30分)", style=discord.ButtonStyle.primary, emoji="🔄")
    async def extend_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("他のユーザーのタスクです。", ephemeral=True)
            return
        
        await interaction.response.defer()
        await self._delete_alert() # ボタンを押したらアラート削除
        
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

class LifeLogView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="終了", style=discord.ButtonStyle.danger, custom_id="lifelog_finish")
    async def finish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.finish_current_task(interaction.user, interaction, next_task_name=None)
    
    @discord.ui.button(label="メモ入力", style=discord.ButtonStyle.primary, custom_id="lifelog_memo")
    async def memo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.prompt_memo_modal(interaction)

    @discord.ui.button(label="計画から選択", style=discord.ButtonStyle.secondary, custom_id="lifelog_from_plan", emoji="📅")
    async def plan_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.prompt_plan_selection(interaction)


# ==========================================
# Cog Class
# ==========================================

class LifeLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lifelog_channel_id = int(os.getenv("LIFELOG_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.notified_event_ids = set()
        self.monitor_tasks = {} # user_id: asyncio.Task

        self.dbx = None
        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token, self.gemini_api_key]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
                genai.configure(api_key=self.gemini_api_key)
                self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
                self.is_ready = True
            except Exception as e:
                logging.error(f"LifeLogCog: クライアント初期化エラー: {e}")
                self.is_ready = False
        else:
            self.is_ready = False
            logging.warning("LifeLogCog: 必須環境変数が不足。一部機能が無効です。")

    async def on_ready(self):
        self.bot.add_view(LifeLogView(self))
        if self.is_ready:
            await self.bot.wait_until_ready()
            if not self.daily_lifelog_summary.is_running():
                self.daily_lifelog_summary.start()
            
            if not self.check_schedule_loop.is_running():
                self.check_schedule_loop.start()
                logging.info("LifeLogCog: ✅ スケジュール監視タスクを開始しました。")
            
            # 再起動時に既存のタスクの監視を再開
            await self._resume_monitoring()

    def cog_unload(self):
        self.daily_lifelog_summary.cancel()
        self.check_schedule_loop.cancel()
        # 監視タスクのキャンセル
        for task in self.monitor_tasks.values():
            task.cancel()

    # --- 監視タスク管理 (Timer) ---
    async def _resume_monitoring(self):
        """起動時にDBから読み込んでタイマーを再セットする"""
        active_logs = await self._get_active_logs()
        now = datetime.now(JST)
        
        for user_id, log in active_logs.items():
            try:
                start_time = datetime.fromisoformat(log['start_time'])
                duration_minutes = log.get('planned_duration', 30)
                end_time = start_time + timedelta(minutes=duration_minutes)
                
                # まだ終わっていなければタイマーをセット
                self._start_monitor_task(user_id, log['task'], log['channel_id'], end_time)
                logging.info(f"LifeLogCog: タスク監視を再開しました User:{user_id}, Task:{log['task']}")
            except Exception as e:
                logging.error(f"LifeLogCog: 監視再開エラー User:{user_id}: {e}")

    def _start_monitor_task(self, user_id, task_name, channel_id, end_time):
        """指定時刻にアラートを出すタスクを開始"""
        # 既存タスクがあればキャンセル
        if user_id in self.monitor_tasks:
            self.monitor_tasks[user_id].cancel()
        
        self.monitor_tasks[user_id] = self.bot.loop.create_task(
            self._monitor_logic(user_id, task_name, channel_id, end_time)
        )

    async def _monitor_logic(self, user_id, task_name, channel_id, end_time):
        """監視ロジック本体: 待機 -> アラート -> 待機 -> 自動終了"""
        try:
            # 1. 終了予定時刻まで待機
            now = datetime.now(JST)
            wait_seconds = (end_time - now).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            
            # タスクがまだアクティブか確認
            active_logs = await self._get_active_logs()
            if user_id not in active_logs or active_logs[user_id]['task'] != task_name:
                return

            # 2. アラート送信
            alert_msg = None
            channel = self.bot.get_channel(channel_id)
            if channel:
                user = self.bot.get_user(int(user_id))
                if not user:
                    try: user = await self.bot.fetch_user(int(user_id))
                    except: pass
                
                mention = user.mention if user else f"User {user_id}"
                
                view = LifeLogTimeUpView(self, user_id, task_name)
                alert_msg = await channel.send(
                    f"{mention} ⏰ タスク「**{task_name}**」の予定時間が経過しました。\n"
                    "延長しますか？それとも終了しますか？（反応がない場合、5分後に自動終了します）", 
                    view=view
                )
                view.alert_message = alert_msg # Viewにメッセージを渡して削除可能にする

            # 3. 反応待ち (5分)
            await asyncio.sleep(300) 

            # 4. 自動終了処理
            # アラートメッセージを削除
            if alert_msg:
                try: await alert_msg.delete()
                except: pass

            # 再度アクティブ確認
            active_logs = await self._get_active_logs()
            if user_id in active_logs and active_logs[user_id]['task'] == task_name:
                # 強制終了
                user_obj = discord.Object(id=int(user_id))
                await self.finish_current_task(user_obj, context=None, end_time=datetime.now(JST))
                
                if channel:
                    await channel.send(f"🛑 応答がなかったため、タスク「{task_name}」を自動終了しました。")

        except asyncio.CancelledError:
            # タスクがキャンセルされた（終了/延長された）場合
            pass
        except Exception as e:
            logging.error(f"LifeLogCog: Monitor logic error for {user_id}: {e}", exc_info=True)
        finally:
            # タスクリストから削除（自身のIDと一致する場合のみ）
            current = asyncio.current_task()
            if user_id in self.monitor_tasks and self.monitor_tasks[user_id] == current:
                del self.monitor_tasks[user_id]

    # --- ヘルパー: タスク名と時間のパース ---
    def _parse_task_and_duration(self, content: str) -> tuple[str, int]:
        match = DURATION_REGEX.search(content)
        if match:
            duration_str = match.group(1)
            unit = match.group(2)
            try:
                value = float(duration_str)
                if unit and unit.lower() in ['h', 'hr', 'hour', '時間']:
                    minutes = int(value * 60)
                else:
                    minutes = int(value)
                task_name = content[:match.start()].strip()
                return task_name, minutes
            except ValueError:
                return content, 30
        return content, 30

    # --- 状態管理 ---
    async def _get_active_logs(self) -> dict:
        if not self.dbx: return {}
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, ACTIVE_LOGS_PATH)
            return json.loads(res.content.decode('utf-8'))
        except (ApiError, Exception):
            return {}

    async def _save_active_logs(self, data: dict):
        if not self.dbx: return
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
            await asyncio.to_thread(self.dbx.files_upload, content, ACTIVE_LOGS_PATH, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"LifeLogCog: アクティブログ保存エラー: {e}")

    # --- メモ入力ロジック ---
    async def prompt_memo_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LifeLogMemoModal(self))

    async def add_memo_to_task(self, interaction: discord.Interaction, memo_content: str):
        user_id = str(interaction.user.id)
        active_logs = await self._get_active_logs()
        if user_id not in active_logs:
            await interaction.followup.send("⚠️ メモを追加する進行中のタスクが見つかりませんでした。", ephemeral=True)
            return
        current_memos = active_logs[user_id].get("memos", [])
        memo_with_time = f"{datetime.now(JST).strftime('%H:%M')} {memo_content}"
        current_memos.append(memo_with_time)
        active_logs[user_id]["memos"] = current_memos
        await self._save_active_logs(active_logs)
        embed = discord.Embed(title="✅ 作業メモを追加しました", description=memo_content, color=discord.Color.green())
        embed.set_footer(text=f"Task: {active_logs[user_id]['task']}")
        await interaction.followup.send(embed=embed, ephemeral=False)

    async def _add_memo_from_message(self, message: discord.Message, memo_content: str):
        user_id = str(message.author.id)
        active_logs = await self._get_active_logs()
        if user_id not in active_logs:
            await message.reply("⚠️ メモを追加する進行中のタスクが見つかりませんでした。")
            return
        current_memos = active_logs[user_id].get("memos", [])
        memo_with_time = f"{datetime.now(JST).strftime('%H:%M')} {memo_content}"
        current_memos.append(memo_with_time)
        active_logs[user_id]["memos"] = current_memos
        await self._save_active_logs(active_logs)
        embed = discord.Embed(title="✅ 作業メモを追加しました", description=memo_content, color=discord.Color.green())
        embed.set_footer(text=f"Task: {active_logs[user_id]['task']}")
        await message.reply(embed=embed)

    # --- 計画からのタスク選択ロジック ---
    async def prompt_plan_selection(self, interaction: discord.Interaction):
        if not self.dbx:
            await interaction.response.send_message("⚠️ Dropboxクライアントが利用できません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        tasks = await self._fetch_todays_plan()
        if not tasks:
            await interaction.followup.send("⚠️ 今日の計画（## Planning > ### Schedule）が見つかりませんでした。", ephemeral=True)
            return
        view = LifeLogPlanSelectView(self, tasks, interaction.user)
        await interaction.followup.send("開始する計画タスクを選択してください:", view=view, ephemeral=True)

    async def _fetch_todays_plan(self) -> list[str]:
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            content = res.content.decode('utf-8')
            planning_match = re.search(r'##\s*Planning\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
            if not planning_match: return []
            planning_text = planning_match.group(1)
            schedule_match = re.search(r'###\s*Schedule\s*(.*?)(?=\n#|$)', planning_text, re.DOTALL | re.IGNORECASE)
            target_text = schedule_match.group(1) if schedule_match else planning_text
            tasks = []
            for line in target_text.split('\n'):
                line = line.strip()
                if not line: continue
                clean_line = re.sub(r'^[-*+]\s*', '', line)
                if clean_line: tasks.append(clean_line)
            return tasks
        except Exception as e:
            logging.error(f"LifeLogCog: 計画読み込みエラー: {e}")
            return []

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
        await message.reply(f"読む書籍を選択してください（予定: {duration}分）:", view=view)

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
            except:
                prev_task_display = prev_task_log
                
            embed.description = f"✅ **前回の記録:** `{prev_task_display}`\n⬇️\n⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ {end_time_str} 予定: {duration}分)"
        else:
            embed.description = f"⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ {end_time_str} 予定: {duration}分)"
        embed.set_footer(text="メモ入力ボタンで詳細を記録できます。")

        reply_msg = await channel.send(f"{user.mention}", embed=embed, view=LifeLogView(self))

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
        
        # ★ 監視タスク開始
        self._start_monitor_task(user_id, task_name, reply_msg.channel.id, end_time_plan)

    async def finish_current_task(self, user: discord.User | discord.Object, context, next_task_name: str = None, end_time: datetime = None) -> str:
        user_id = str(user.id)
        
        # ★ 監視タスクのキャンセル (自分自身の場合はキャンセルしない)
        if user_id in self.monitor_tasks:
            task = self.monitor_tasks[user_id]
            current_task = asyncio.current_task()
            if task != current_task:
                task.cancel()
            del self.monitor_tasks[user_id]

        active_logs = await self._get_active_logs()

        if user_id not in active_logs:
            if isinstance(context, discord.Interaction):
                if not context.response.is_done():
                    await context.response.send_message("⚠️ 進行中のタスクはありません。", ephemeral=True)
                else:
                    await context.followup.send("⚠️ 進行中のタスクはありません。", ephemeral=True)
            return None

        log_data = active_logs.pop(user_id)
        await self._save_active_logs(active_logs)

        start_time = datetime.fromisoformat(log_data['start_time'])
        
        if end_time is None:
            end_time = datetime.now(JST)
            
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
                if lines:
                    formatted_memos.append(f"\t- {lines[0]}")
                for line in lines[1:]:
                    if line.strip():
                        formatted_memos.append(f"\t- {line.strip()}")
            
            if formatted_memos:
                obsidian_line += "\n" + "\n".join(formatted_memos)

        saved = await self._save_to_obsidian(date_str, obsidian_line)

        # 読書ノートへの連携
        if self.dbx:
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", task_name)
            book_path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}/{safe_title}.md"
            try:
                self.dbx.files_get_metadata(book_path)
                _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
                book_content = res.content.decode('utf-8')
                book_log_line = f"- {date_str} {start_hm} - {end_hm} ({duration_str}) 読書ログ"
                if formatted_memos: book_log_line += "\n" + "\n".join(formatted_memos)
                new_book_content = update_section(book_content, book_log_line, "## Notes")
                await asyncio.to_thread(self.dbx.files_upload, new_book_content.encode('utf-8'), book_path, mode=WriteMode('overwrite'))
                
                if isinstance(context, discord.Interaction) and not next_task_name:
                    if not context.response.is_done():
                        await context.response.send_message(f"📖 読書ノート `{task_name}` にも記録しました。", ephemeral=True)
                    else:
                        await context.followup.send(f"📖 読書ノート `{task_name}` にも記録しました。", ephemeral=True)
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
        except Exception:
            pass

        if isinstance(context, discord.Interaction) and not next_task_name:
            embed = discord.Embed(title="✅ タスク完了", color=discord.Color.light_grey())
            embed.add_field(name="Task", value=task_name, inline=True)
            embed.add_field(name="Duration", value=duration_str, inline=True)
            embed.set_footer(text=f"{start_hm} - {end_hm}")
            
            if not context.response.is_done():
                await context.response.send_message(embed=embed, ephemeral=True)
            else:
                await context.followup.send(embed=embed, ephemeral=True)
        
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
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = ""
                else:
                    raise

            new_content = update_section(current_content, line_to_add, DAILY_NOTE_HEADER)

            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            return True
        except Exception as e:
            logging.error(f"LifeLogCog: Obsidian保存エラー: {e}", exc_info=True)
            return False

    # --- タスク延長処理 ---
    async def extend_task(self, interaction: discord.Interaction, minutes: int = 30):
        user_id = str(interaction.user.id)
        active_logs = await self._get_active_logs()
        
        if user_id in active_logs:
            active_logs[user_id]['planned_duration'] += minutes
            await self._save_active_logs(active_logs)
            
            # ★ 監視タスクの再設定
            task_name = active_logs[user_id]['task']
            channel_id = active_logs[user_id]['channel_id']
            start_time = datetime.fromisoformat(active_logs[user_id]['start_time'])
            new_duration = active_logs[user_id]['planned_duration']
            new_end_time = start_time + timedelta(minutes=new_duration)
            
            self._start_monitor_task(user_id, task_name, channel_id, new_end_time)
            
        else:
            await interaction.followup.send("延長する進行中のタスクが見つかりませんでした。", ephemeral=True)

    # --- スケジュール監視ループ ---
    @tasks.loop(minutes=1)
    async def check_schedule_loop(self):
        """JournalCogから今日の予定を取得し、現在時刻と一致するものがあれば通知する"""
        if not self.is_ready: return
        
        journal_cog = self.bot.get_cog("JournalCog")
        if not journal_cog: return

        try:
            events = await journal_cog._get_todays_events()
        except Exception as e:
            logging.error(f"Schedule check error: {e}")
            return

        now = datetime.now(JST)
        current_time_str = now.strftime('%H:%M')

        for event in events:
            start_str = event.get('start', {}).get('dateTime')
            if not start_str: continue 
            
            event_id = event['id']
            summary = event.get('summary', '不明な予定')

            start_dt = datetime.fromisoformat(start_str).astimezone(JST)
            event_time_str = start_dt.strftime('%H:%M')
            
            if event_time_str == current_time_str:
                if event_id not in self.notified_event_ids:
                    channel = self.bot.get_channel(self.lifelog_channel_id)
                    if channel:
                        # 予定の長さを取得してデフォルト値として設定
                        duration = 30
                        end_str = event.get('end', {}).get('dateTime')
                        if end_str:
                            end_dt = datetime.fromisoformat(end_str).astimezone(JST)
                            duration = int((end_dt - start_dt).total_seconds() / 60)

                        view = LifeLogScheduleStartView(self, summary, duration)
                        await channel.send(f"⏰ **予定の時間です**: {summary}\nこのタスクを開始しますか？（予定: {duration}分）", view=view)
                        self.notified_event_ids.add(event_id)

    @tasks.loop(time=DAILY_SUMMARY_TIME)
    async def daily_lifelog_summary(self):
        if not self.is_ready: return
        target_date = datetime.now(JST).date() - timedelta(days=1)
        logging.info(f"LifeLogCog: 昨日のライフログサマリー生成を開始します。対象日: {target_date}")
        await self._generate_and_save_summary(target_date)

    @daily_lifelog_summary.before_loop
    async def before_summary_task(self):
        await self.bot.wait_until_ready()

    async def _generate_and_save_summary(self, target_date: date):
        if not self.dbx or not self.is_ready: return

        date_str = target_date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        current_content = "" 

        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            current_content = res.content.decode('utf-8')

            log_section_match = re.search(r'##\s*Life\s*Logs\s*(.*?)(?=\n##|$)', current_content, re.DOTALL | re.IGNORECASE)
            
            if not log_section_match or not log_section_match.group(1).strip():
                logging.info(f"LifeLogCog: {date_str} のライフログが見つかりませんでした。サマリーをスキップします。")
                return

            life_logs_text = log_section_match.group(1).strip()
            
            prompt = f"""
            あなたは生産性向上のためのコーチです。以下の作業ログを分析し、
            **客観的な事実**（総時間、主な活動、傾向）と**次の日の計画に役立つ洞察**を、
            Markdown形式で簡潔にまとめてください。

            # 洞察のポイント
            1.  **事実**: 昨日の総活動時間と、最も長く費やしたタスク（カテゴリ）は何ですか？
            2.  **傾向**: どの時間帯が最も集中できた（タスクが長く続いた）傾向がありますか？
            3.  **提案**: このログから見て、今日の計画で避けるべきことや、実行すべきことを1つ提案してください。
            
            # 昨日のライフログ（{date_str}）
            {life_logs_text}
            """
            
            response = await asyncio.wait_for(self.gemini_model.generate_content_async(prompt), timeout=120)
            summary_text = response.text.strip()
            
            new_content = update_section(current_content, summary_text, SUMMARY_NOTE_HEADER)

            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"LifeLogCog: {date_str} のAIサマリーをObsidianに保存しました。")
            
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                 logging.warning(f"LifeLogCog: 昨日のデイリーノートが見つかりません。サマリーをスキップします。")
            else:
                 logging.error(f"LifeLogCog: サマリー生成/保存中にDropboxエラー: {e}")
        except Exception as e:
            logging.error(f"LifeLogCog: サマリー生成中に予期せぬエラー: {e}", exc_info=True)
            summary_text = f"❌ AIサマリー生成中にエラーが発生しました: {type(e).__name__}"
            try:
                if current_content:
                    await asyncio.to_thread(
                        self.dbx.files_upload,
                        update_section(current_content, summary_text, SUMMARY_NOTE_HEADER).encode('utf-8'),
                        daily_note_path,
                        mode=WriteMode('overwrite')
                    )
            except Exception as e_save:
                 logging.error(f"エラー後のサマリー保存に失敗: {e_save}")
            
async def setup(bot: commands.Bot):
    if int(os.getenv("LIFELOG_CHANNEL_ID", 0)) == 0:
        logging.error("LifeLogCog: LIFELOG_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    await bot.add_cog(LifeLogCog(bot))