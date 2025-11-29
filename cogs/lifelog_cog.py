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
# ライフログサマリータスクの時刻を早朝に設定
DAILY_SUMMARY_TIME = time(hour=6, minute=0, tzinfo=JST) 

# --- メモ入力モーダル ---
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

# --- タスク開始確認用View ---
class LifeLogConfirmTaskView(discord.ui.View):
    def __init__(self, cog, task_name: str, original_message: discord.Message):
        super().__init__(timeout=60)
        self.cog = cog
        self.task_name = task_name
        self.original_message = original_message

    @discord.ui.button(label="開始", style=discord.ButtonStyle.success)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return
        
        await interaction.response.defer()
        # メッセージを更新してボタンを消す
        try:
            await interaction.edit_original_response(content=f"✅ タスク「**{self.task_name}**」の計測を開始します。", view=None)
        except: pass
        
        # タスク切り替え処理を実行
        await self.cog.switch_task(self.original_message, self.task_name)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("他のユーザーの操作です。", ephemeral=True)
            return
        
        await interaction.response.edit_message(content="❌ 開始をキャンセルしました。", view=None)
        self.stop()

    async def on_timeout(self):
        try:
            await self.original_message.edit(content=f"{self.original_message.content}\n(タイムアウトしました)", view=None)
        except: pass


# --- 書籍選択用View ---
class LifeLogBookSelectView(discord.ui.View):
    def __init__(self, cog, book_options: list[discord.SelectOption], original_author: discord.User):
        super().__init__(timeout=60)
        self.cog = cog
        self.original_author = original_author
        
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
        
        await interaction.response.edit_message(content=f"📖 書籍を選択しました: **{task_name}**", view=None, embed=None)
        await self.cog.switch_task_from_interaction(interaction, task_name)
        self.stop()

# --- 計画タスク選択用View ---
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
        
        await interaction.response.edit_message(content=f"📅 計画から開始: **{selected_task}**", view=None, embed=None)
        await self.cog.switch_task_from_interaction(interaction, selected_task)
        self.stop()

# --- タイムアウト確認用View ---
class LifeLogTimeoutView(discord.ui.View):
    def __init__(self, cog, user_id: str):
        super().__init__(timeout=300) # 5分間有効
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="延長する", style=discord.ButtonStyle.success, emoji="🔄")
    async def extend_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("他のユーザーのタスクです。", ephemeral=True)
            return
        
        await interaction.response.defer()
        await self.cog.extend_task(interaction)
        for item in self.children: item.disabled = True
        
        await interaction.message.edit(content="✅ タスクを延長しました。引き続き計測します。", view=self)
        self.stop()

    @discord.ui.button(label="終了する", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def finish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("他のユーザーのタスクです。", ephemeral=True)
            return
        
        await interaction.response.defer()
        await self.cog.finish_current_task(interaction.user, interaction)
        for item in self.children: item.disabled = True
        
        await interaction.message.edit(content="✅ タスクを終了しました。", view=self)
        self.stop()


# --- メイン操作パネルView ---
class LifeLogView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None) # Persistent View
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


class LifeLogCog(commands.Cog):
    """
    チャットに書き込むだけで作業時間を計測し、Obsidianに記録するライフログ機能
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lifelog_channel_id = int(os.getenv("LIFELOG_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
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
            if not self.daily_lifelog_summary.is_running():
                self.daily_lifelog_summary.start()
                logging.info("LifeLogCog: 日次サマリータスクを開始しました。")
            if not self.check_task_timeout.is_running():
                self.check_task_timeout.start()
                logging.info("LifeLogCog: タイムアウト監視タスクを開始しました。")


    def cog_unload(self):
        self.check_task_timeout.cancel()
        self.daily_lifelog_summary.cancel()

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
        user_id = str(interaction.user.id)
        active_logs = await self._get_active_logs()
        if user_id not in active_logs:
            await interaction.response.send_message("⚠️ メモを追加する進行中のタスクがありません。", ephemeral=True)
            return

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
                task_content = re.sub(r'^(\d{1,2}:\d{2}(?:[~-]\d{1,2}:\d{2})?)\s*', '', clean_line).strip()
                if task_content: tasks.append(task_content)
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

        # "m " で始まる場合はメモとして処理
        if content.lower().startswith("m ") or content.startswith("ｍ "):
            memo_text = content[2:].strip()
            await self._add_memo_from_message(message, memo_text)
            return

        if content == "読書":
            await self.prompt_book_selection(message)
            return

        # ★ 修正: いきなり開始せず、確認Viewを表示する
        view = LifeLogConfirmTaskView(self, content, message)
        await message.reply(f"タスク「**{content}**」として計測を開始しますか？", view=view)

    async def prompt_book_selection(self, message: discord.Message):
        book_cog = self.bot.get_cog("BookCog")
        if not book_cog:
            await message.reply("⚠️ BookCogが見つからないため、書籍リストを取得できません。「読書」タスクとして開始します。")
            # 読書の場合も確認を入れる
            view = LifeLogConfirmTaskView(self, "読書", message)
            await message.reply(f"タスク「**読書**」として計測を開始しますか？", view=view)
            return

        book_files, error = await book_cog.get_book_list()
        if error or not book_files:
            await message.reply(f"⚠️ 書籍リストの取得に失敗したか、書籍がありません ({error})。「読書」タスクとして開始します。")
            view = LifeLogConfirmTaskView(self, "読書", message)
            await message.reply(f"タスク「**読書**」として計測を開始しますか？", view=view)
            return

        options = []
        for entry in book_files[:25]:
            file_name = os.path.basename(entry.path_display)
            label = os.path.splitext(file_name)[0][:100]
            options.append(discord.SelectOption(label=label, value=file_name))

        view = LifeLogBookSelectView(self, options, message.author)
        await message.reply("読む書籍を選択してください（これまでのタスクは終了します）:", view=view)

    async def switch_task_from_interaction(self, interaction: discord.Interaction, new_task_name: str):
        user = interaction.user
        prev_task_log = await self.finish_current_task(user, interaction, next_task_name=new_task_name)
        await self.start_new_task_context(interaction.channel, user, new_task_name, prev_task_log)

    async def switch_task(self, message: discord.Message, new_task_name: str):
        user = message.author
        prev_task_log = await self.finish_current_task(user, message, next_task_name=new_task_name)
        await self.start_new_task_context(message.channel, user, new_task_name, prev_task_log)

    async def start_new_task_context(self, channel, user: discord.User, task_name: str, prev_task_log: str = None):
        user_id = str(user.id)
        now = datetime.now(JST)
        start_time_str = now.strftime('%H:%M')

        embed = discord.Embed(color=discord.Color.green())
        if prev_task_log:
            try:
                prev_log_text = prev_task_log.split("(", 1)[0].strip()
                duration_text = prev_task_log.split("(", 1)[1].split(")", 1)[0]
                task_text = prev_task_log.split(")", 1)[1].strip()
                prev_task_display = f"{prev_log_text} ({duration_text}) {task_text}"
            except:
                prev_task_display = prev_task_log
                
            embed.description = f"✅ **前回の記録:** `{prev_task_display}`\n⬇️\n⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ )"
        else:
            embed.description = f"⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ )"
        embed.set_footer(text="メモ入力ボタンで詳細を記録できます。")

        reply_msg = await channel.send(f"{user.mention}", embed=embed, view=LifeLogView(self))

        active_logs = await self._get_active_logs()
        active_logs[user_id] = {
            "task": task_name,
            "start_time": now.isoformat(),
            "message_id": reply_msg.id,
            "channel_id": reply_msg.channel.id,
            "memos": [],
            "notification_count": 0 
        }
        await self._save_active_logs(active_logs)

    async def finish_current_task(self, user: discord.User | discord.Object, context, next_task_name: str = None, end_time: datetime = None) -> str:
        user_id = str(user.id)
        active_logs = await self._get_active_logs()

        if user_id not in active_logs:
            if isinstance(context, discord.Interaction):
                if context.response.is_done():
                    await context.followup.send("⚠️ 進行中のタスクはありません。", ephemeral=True)
                else:
                    await context.response.send_message("⚠️ 進行中のタスクはありません。", ephemeral=True)
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

        if self.dbx:
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", task_name)
            book_path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}/{safe_title}.md"
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
                book_content = res.content.decode('utf-8')
                book_log_line = f"- {date_str} {start_hm} - {end_hm} ({duration_str}) 読書ログ"
                if formatted_memos: book_log_line += "\n" + "\n".join(formatted_memos)
                new_book_content = update_section(book_content, book_log_line, "## Notes")
                await asyncio.to_thread(self.dbx.files_upload, new_book_content.encode('utf-8'), book_path, mode=WriteMode('overwrite'))
                logging.info(f"LifeLogCog: 読書ノート「{task_name}」にログを連携しました。")
                
                if isinstance(context, discord.Interaction) and not next_task_name:
                    if context.response.is_done():
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
            
            if context.response.is_done():
                await context.followup.send(embed=embed, ephemeral=True)
            else:
                await context.response.send_message(embed=embed, ephemeral=True)
        
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
    async def extend_task(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        active_logs = await self._get_active_logs()
        
        if user_id in active_logs:
            if 'last_warning' in active_logs[user_id]:
                del active_logs[user_id]['last_warning']
                await self._save_active_logs(active_logs)
                await interaction.followup.send("タスクを延長しました。引き続き計測します。", ephemeral=True)
            else:
                await interaction.followup.send("タスクは既に延長されているか、警告状態ではありません。", ephemeral=True)
        else:
            await interaction.followup.send("延長する進行中のタスクが見つかりませんでした。", ephemeral=True)

    # --- タイムアウト監視ループ (★ 修正: ロジック見直し) ---
    @tasks.loop(minutes=1)
    async def check_task_timeout(self):
        if not self.is_ready: return
        try:
            active_logs = await self._get_active_logs()
            changed = False
            now = datetime.now(JST)

            for user_id, log in list(active_logs.items()):
                try:
                    start_time = datetime.fromisoformat(log['start_time'])
                    elapsed_seconds = (now - start_time).total_seconds()
                    
                    # カウントがなければ0で初期化
                    count = log.get('notification_count', 0)
                    last_warning_str = log.get('last_warning')
                    
                    # 60分(3600秒)ごとに通知
                    threshold_seconds = (count + 1) * 60 * 60
                    
                    # 1. 1時間毎の経過警告
                    if elapsed_seconds >= threshold_seconds:
                        if not last_warning_str:
                            channel = self.bot.get_channel(log.get('channel_id'))
                            if channel:
                                user = self.bot.get_user(int(user_id))
                                if not user:
                                    try: user = await self.bot.fetch_user(int(user_id))
                                    except: pass
                                mention = user.mention if user else f"User {user_id}"
                                
                                view = LifeLogTimeoutView(self, user_id)
                                await channel.send(
                                    f"{mention} ⚠️ タスク「**{log['task']}**」開始から {int(elapsed_seconds//3600)} 時間が経過しました。\n"
                                    "継続しますか？（反応がない場合、約5分後に自動終了します）", 
                                    view=view
                                )
                            
                            log['last_warning'] = now.isoformat()
                            log['notification_count'] = count + 1
                            changed = True
                    
                    # 2. 警告から5分経過後の自動終了
                    if last_warning_str:
                        last_warning = datetime.fromisoformat(last_warning_str)
                        if (now - last_warning).total_seconds() >= 300: # 5分
                            user_obj = discord.Object(id=int(user_id))
                            # 終了時刻は警告時刻とする
                            await self.finish_current_task(user_obj, context=None, end_time=last_warning)
                            
                            channel = self.bot.get_channel(log.get('channel_id'))
                            if channel:
                                await channel.send(f"🛑 応答がなかったため、タスク「{log['task']}」を自動終了しました。")
                            continue 

                except Exception as e:
                    logging.error(f"LifeLogCog: Timeout check error for user {user_id}: {e}")

            if changed:
                await self._save_active_logs(active_logs)
        except Exception as e:
            logging.error(f"LifeLogCog: check_task_timeout main loop error: {e}")

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