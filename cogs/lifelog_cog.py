import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import json
from datetime import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError

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

class LifeLogView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None) # Persistent View
        self.cog = cog

    @discord.ui.button(label="終了", style=discord.ButtonStyle.secondary, custom_id="lifelog_finish")
    async def finish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ボタンを押したときは「次のタスクなし」で終了処理
        await self.cog.finish_current_task(interaction.user, interaction, next_task_name=None)

class LifeLogCog(commands.Cog):
    """
    チャットに書き込むだけで作業時間を計測し、Obsidianに記録するライフログ機能
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lifelog_channel_id = int(os.getenv("LIFELOG_CHANNEL_ID", 0))
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.dbx = None
        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
            except Exception as e:
                logging.error(f"LifeLogCog: Dropbox初期化エラー: {e}")

    async def on_ready(self):
        self.bot.add_view(LifeLogView(self))

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

    # --- メインロジック: チャット監視 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """指定チャンネルへの投稿を検知してタスクを開始/切り替え"""
        if message.author.bot: return
        if message.channel.id != self.lifelog_channel_id: return
        
        task_name = message.content.strip()
        if not task_name: return

        # 以前のタスクがあれば終了し、新しいタスクを開始する
        await self.switch_task(message, task_name)

    async def switch_task(self, message: discord.Message, new_task_name: str):
        """タスクの切り替え処理（終了 -> 開始）"""
        user = message.author
        
        # 1. 進行中のタスクがあれば終了処理を行う
        prev_task_log = await self.finish_current_task(user, message, next_task_name=new_task_name)
        
        # 2. 新しいタスクを開始
        await self.start_new_task(message, new_task_name, prev_task_log)

    async def start_new_task(self, message: discord.Message, task_name: str, prev_task_log: str = None):
        """新しいタスクの開始処理"""
        user_id = str(message.author.id)
        now = datetime.now(JST)
        start_time_str = now.strftime('%H:%M')

        # メッセージ作成
        embed = discord.Embed(color=discord.Color.green())
        if prev_task_log:
            embed.description = f"✅ **前回の記録:** `{prev_task_log}`\n⬇️\n⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ )"
        else:
            embed.description = f"⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ )"

        # ボタン付きで返信
        reply_msg = await message.channel.send(embed=embed, view=LifeLogView(self))

        # 状態保存
        active_logs = await self._get_active_logs()
        active_logs[user_id] = {
            "task": task_name,
            "start_time": now.isoformat(),
            "message_id": reply_msg.id,
            "channel_id": reply_msg.channel.id
        }
        await self._save_active_logs(active_logs)

    async def finish_current_task(self, user: discord.User, context, next_task_name: str = None) -> str:
        """
        現在進行中のタスクを終了し、Obsidianに保存する。
        context: discord.Message (チャット投稿時) or discord.Interaction (ボタン押し時)
        return: 保存したログ文字列（ない場合はNone）
        """
        user_id = str(user.id)
        active_logs = await self._get_active_logs()

        if user_id not in active_logs:
            # タスクがないのにボタンを押した場合などのエラーハンドリング
            if isinstance(context, discord.Interaction):
                await context.response.send_message("⚠️ 進行中のタスクはありません。", ephemeral=True)
            return None

        # データの取り出しと保存
        log_data = active_logs.pop(user_id)
        await self._save_active_logs(active_logs)

        # 時間計算
        start_time = datetime.fromisoformat(log_data['start_time'])
        end_time = datetime.now(JST)
        duration = end_time - start_time
        
        # 所要時間の整形
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        duration_str = ""
        if hours > 0: duration_str += f"{hours}h"
        duration_str += f"{minutes}m"
        if total_seconds < 60: duration_str = "0m" # 秒単位は切り捨てて0mとするか、1mにするかはお好みで

        # Obsidian用フォーマット
        date_str = start_time.strftime('%Y-%m-%d')
        start_hm = start_time.strftime('%H:%M')
        end_hm = end_time.strftime('%H:%M')
        task_name = log_data['task']
        
        obsidian_line = f"- {start_hm} - {end_hm} ({duration_str}) {task_name}"

        # Obsidianに保存
        saved = await self._save_to_obsidian(date_str, obsidian_line)

        # 以前のパネル（Embed）を更新して「完了」状態にする
        try:
            channel = self.bot.get_channel(log_data['channel_id'])
            if channel:
                try:
                    old_msg = await channel.fetch_message(log_data['message_id'])
                    embed = old_msg.embeds[0]
                    embed.color = discord.Color.dark_grey() # 色をグレーにして完了感を出す
                    embed.description = f"✅ **完了:** {task_name} ({start_hm} - {end_hm}, {duration_str})"
                    await old_msg.edit(embed=embed, view=None) # ボタンを削除
                except discord.NotFound:
                    pass
        except Exception as e:
            logging.warning(f"LifeLogCog: 過去メッセージ更新失敗: {e}")

        # インタラクション（ボタン押し）の場合はフィードバックを返す
        if isinstance(context, discord.Interaction):
            await context.response.send_message(f"お疲れ様でした！記録しました: `{obsidian_line}`", ephemeral=True)
        
        return obsidian_line

    async def _save_to_obsidian(self, date_str: str, line_to_add: str) -> bool:
        """Obsidianのデイリーノートに追記"""
        if not self.dbx: return False
        
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# {date_str}\n"
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

async def setup(bot: commands.Bot):
    await bot.add_cog(LifeLogCog(bot))