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
# ライフログサマリータスクの時刻を早朝に設定
DAILY_SUMMARY_TIME = time(hour=6, minute=0, tzinfo=JST) 

# --- 新規モーダル: メモ入力 ---
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
        await interaction.response.defer(ephemeral=True)
        await self.cog.add_memo_to_task(interaction, self.memo_text.value)

# --- Viewの修正: メモボタンを追加 ---
class LifeLogView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None) # Persistent View
        self.cog = cog

    @discord.ui.button(label="終了", style=discord.ButtonStyle.danger, custom_id="lifelog_finish")
    async def finish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.finish_current_task(interaction.user, interaction, next_task_name=None)
    
    @discord.ui.button(label="メモ入力", style=discord.ButtonStyle.primary, custom_id="lifelog_memo")
    async def memo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.prompt_memo_modal(interaction)


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
                self.gemini_model = genai.GenerativeModel("gemini-3-pro-preview")
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


    # --- 状態管理 ---
    async def _get_active_logs(self) -> dict:
        if not self.dbx: return {}
        try:
            # Dropbox APIコールは asyncio.to_thread で実行
            _, res = await asyncio.to_thread(self.dbx.files_download, ACTIVE_LOGS_PATH)
            return json.loads(res.content.decode('utf-8'))
        except (ApiError, Exception):
            return {}

    async def _save_active_logs(self, data: dict):
        if not self.dbx: return
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
            # Dropbox APIコールは asyncio.to_thread で実行
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
        # メモを時刻付きで保存
        memo_with_time = f"{datetime.now(JST).strftime('%H:%M')} {memo_content}"
        current_memos.append(memo_with_time)
        active_logs[user_id]["memos"] = current_memos
        await self._save_active_logs(active_logs)

        await interaction.followup.send(f"✅ メモをタスクに追加しました。\n> `{memo_content}`", ephemeral=True)


    # --- チャット監視＆切り替え ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.lifelog_channel_id: return
        
        task_name = message.content.strip()
        if not task_name: return

        await self.switch_task(message, task_name)

    async def switch_task(self, message: discord.Message, new_task_name: str):
        user = message.author
        
        # 1. 進行中のタスクがあれば終了処理を行う
        prev_task_log = await self.finish_current_task(user, message, next_task_name=new_task_name)
        
        # 2. 新しいタスクを開始
        await self.start_new_task(message, new_task_name, prev_task_log)

    async def start_new_task(self, message: discord.Message, task_name: str, prev_task_log: str = None):
        user_id = str(message.author.id)
        now = datetime.now(JST)
        start_time_str = now.strftime('%H:%M')

        # メッセージ作成
        embed = discord.Embed(color=discord.Color.green())
        if prev_task_log:
            # ログには日付が入るため、時刻以降を切り出す
            try:
                # - HH:MM - HH:MM (Duration) **Task**
                # ログ文字列から duration と task name を抽出
                prev_log_text = prev_task_log.split("(", 1)[0].strip() # - HH:MM - HH:MM
                duration_text = prev_task_log.split("(", 1)[1].split(")", 1)[0] # duration
                task_text = prev_task_log.split(")", 1)[1].strip() # **task_name**
                prev_task_display = f"{prev_log_text} ({duration_text}) {task_text}"
            except:
                prev_task_display = prev_task_log
                
            embed.description = f"✅ **前回の記録:** `{prev_task_display}`\n⬇️\n⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ )"
        else:
            embed.description = f"⏱️ **計測開始:** **{task_name}** ({start_time_str} ~ )"
        embed.set_footer(text="メモ入力ボタンで詳細を記録できます。")

        # ボタン付きで返信
        reply_msg = await message.channel.send(embed=embed, view=LifeLogView(self))

        # 状態保存
        active_logs = await self._get_active_logs()
        active_logs[user_id] = {
            "task": task_name,
            "start_time": now.isoformat(),
            "message_id": reply_msg.id,
            "channel_id": reply_msg.channel.id,
            "memos": []
        }
        await self._save_active_logs(active_logs)

    async def finish_current_task(self, user: discord.User, context, next_task_name: str = None) -> str:
        user_id = str(user.id)
        active_logs = await self._get_active_logs()

        if user_id not in active_logs:
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
        
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        duration_str = (f"{hours}h" if hours > 0 else "") + f"{minutes}m"
        if total_seconds < 60: duration_str = "0m"

        # Obsidian用フォーマット
        date_str = start_time.strftime('%Y-%m-%d')
        start_hm = start_time.strftime('%H:%M')
        end_hm = end_time.strftime('%H:%M')
        task_name = log_data['task']
        memos = log_data.get('memos', [])
        
        # 1. メインライン
        obsidian_line = f"- {start_hm} - {end_hm} ({duration_str}) **{task_name}**"
        
        # 2. メモをネストされた箇条書きとして整形して追記
        if memos:
            # メモの各行から Markdown 箇条書きを作成 (改行はスペースに置換)
            nested_memos = "\n".join([f"\t- {m.replace('\n', ' ').strip()}" for m in memos])
            obsidian_line += f"\n{nested_memos}"

        # Obsidianに保存
        saved = await self._save_to_obsidian(date_str, obsidian_line)

        # 以前のパネル（Embed）を更新
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

        # インタラクション（ボタン押し）の場合はフィードバックを返す
        if isinstance(context, discord.Interaction) and not next_task_name:
            await context.response.send_message(f"お疲れ様でした！記録しました: `{task_name} ({duration_str})`", ephemeral=True)
        
        return obsidian_line

    async def _save_to_obsidian(self, date_str: str, line_to_add: str) -> bool:
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

    # --- ジャーナル連携フック（日次タスク） ---
    @tasks.loop(time=DAILY_SUMMARY_TIME)
    async def daily_lifelog_summary(self):
        """昨日のライフログを読み込み、AIでサマリーしてObsidianに保存するタスク（早朝実行）"""
        if not self.is_ready: return
        target_date = datetime.now(JST).date() - timedelta(days=1)
        logging.info(f"LifeLogCog: 昨日のライフログサマリー生成を開始します。対象日: {target_date}")
        await self._generate_and_save_summary(target_date)

    @daily_lifelog_summary.before_loop
    async def before_summary_task(self):
        await self.bot.wait_until_ready()

    async def _generate_and_save_summary(self, target_date: date):
        """Obsidianの昨日分からライフログを読み取り、AIでサマリーして保存する"""
        if not self.dbx or not self.is_ready: return

        date_str = target_date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        # current_content を try 外で初期化
        current_content = "" 

        try:
            # 1. 昨日のデイリーノートを読み込む
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            current_content = res.content.decode('utf-8')

            # 2. ## Life Logs セクションを抽出
            log_section_match = re.search(r'##\s*Life\s*Logs\s*(.*?)(?=\n##|$)', current_content, re.DOTALL | re.IGNORECASE)
            
            if not log_section_match or not log_section_match.group(1).strip():
                logging.info(f"LifeLogCog: {date_str} のライフログが見つかりませんでした。サマリーをスキップします。")
                return

            life_logs_text = log_section_match.group(1).strip()
            
            # 3. Gemini APIでサマリーを生成
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
            
            # 4. ## Life Logs Summary セクションに保存
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
            # エラー時もObsidianに追記してエラーを記録
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