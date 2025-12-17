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
from dropbox.exceptions import ApiError
import re
import asyncio
import json
from typing import Optional

# --- 共通関数をインポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
BASE_PATH = os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')
JOURNAL_SCHEDULE_PATH = f"{BASE_PATH}/.bot/journal_schedule.json"

# ==========================================
# UI Components
# ==========================================

class JournalSelectionView(discord.ui.View):
    """AIが整理した一日の出来事リストから、ジャーナルに記載する項目を選択するView"""
    def __init__(self, cog, organized_events: list[str]):
        super().__init__(timeout=None)
        self.cog = cog
        self.organized_events = organized_events
        self.current_selection = []

        # オプション作成 (最大25件)
        options = []
        for i, event in enumerate(organized_events[:25]):
            label = event[:95] + "..." if len(event) > 95 else event
            options.append(discord.SelectOption(label=label, value=str(i), default=False))

        if options:
            select = discord.ui.Select(
                placeholder="ジャーナルに含める項目を選択...",
                min_values=0,
                max_values=len(options),
                options=options
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.current_selection = [int(v) for v in interaction.data["values"]]

    @discord.ui.button(label="次へ (振り返り入力)", style=discord.ButtonStyle.primary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 選択された項目をリスト化して次のモーダルへ渡す
        selected_text_list = [self.organized_events[i] for i in self.current_selection if i < len(self.organized_events)]
        await interaction.response.send_modal(JournalReflectionModal(self.cog, selected_text_list))

class JournalReflectionModal(discord.ui.Modal, title="夜の振り返り"):
    feelings = discord.ui.TextInput(
        label="感想・感じたこと",
        style=discord.TextStyle.paragraph,
        placeholder="今日の出来事について感じたことを自由に記述してください。",
        required=True,
        max_length=1000
    )
    
    wins = discord.ui.TextInput(
        label="うまくいったこと (Wins)",
        style=discord.TextStyle.paragraph,
        placeholder="今日達成できたことや、良かった点を記述してください。",
        required=False,
        max_length=1000
    )
    
    learnings = discord.ui.TextInput(
        label="学んだこと (Learnings)",
        style=discord.TextStyle.paragraph,
        placeholder="今日得た気づきや学び、改善点を記述してください。",
        required=False,
        max_length=1000
    )

    def __init__(self, cog, selected_logs):
        super().__init__(timeout=1800)
        self.cog = cog
        self.selected_logs = selected_logs

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False, thinking=True)
        # 統合処理の呼び出し (3つの振り返り項目を渡す)
        await self.cog._process_unified_journal(
            interaction, 
            self.selected_logs, 
            self.feelings.value,
            self.wins.value,
            self.learnings.value
        )

class NightlyJournalView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="今日を振り返る", style=discord.ButtonStyle.primary, emoji="📝", custom_id="journal_nightly_review")
    async def write_journal(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ボタンを押すと、まずログの収集と整理(AI)が走り、SelectionViewが表示される
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog.start_nightly_review_flow(interaction)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


# ==========================================
# Cog Class
# ==========================================

class JournalCog(commands.Cog):
    """夜の振り返りを行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()

        if not self._validate_env_vars():
            return

        try:
            self.session = aiohttp.ClientSession()
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") 
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            self.journal_schedule_path = JOURNAL_SCHEDULE_PATH
            self.is_ready = True
            logging.info("JournalCog initialized.")
        except Exception as e:
            logging.error(f"JournalCog init failed: {e}")

    def _load_env_vars(self):
        self.channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        required = ["JOURNAL_CHANNEL_ID", "GEMINI_API_KEY", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"]
        if not all(getattr(self, name.lower(), None) or (name == "JOURNAL_CHANNEL_ID" and self.channel_id) for name in required):
            return False
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            self.bot.add_view(NightlyJournalView(self))
            await self.bot.wait_until_ready()
            
            # ジャーナルタスク登録
            for path, task in [(self.journal_schedule_path, self.prompt_daily_journal)]:
                sched = await self._load_schedule_from_db(path)
                if sched:
                    task.change_interval(time=time(hour=sched['hour'], minute=sched['minute'], tzinfo=JST))
                    if not task.is_running(): task.start()

    async def cog_unload(self):
        if self.session: await self.session.close()
        self.prompt_daily_journal.cancel()

    # --- Helper Methods ---

    async def _get_todays_life_logs_content(self) -> str:
        """今日のLifeLogsセクションの中身（時間記録）をそのまま取得する"""
        if not self.dbx: return ""
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            content = res.content.decode('utf-8')
            match = re.search(r'##\s*Life\s*Logs\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
            return ""
        except: return ""

    async def _get_todays_all_logs(self) -> list[str]:
        """今日のすべてのログ（Memo, Life Logs, Todo）を取得する"""
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        logs = []
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            content = res.content.decode('utf-8')
            
            for header in ["## Memo", "## Life Logs", "## Completed Tasks"]:
                match = re.search(rf'{header}\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
                if match:
                    section_text = match.group(1).strip()
                    for line in section_text.split('\n'):
                        if line.strip():
                            logs.append(line.strip())
        except ApiError:
            pass 
        except Exception as e:
            logging.error(f"Log fetch error: {e}")
        
        return logs

    async def _organize_logs_with_ai(self, raw_logs: list[str]) -> list[str]:
        """AIを使ってログを「出来事リスト」に整理・要約する"""
        if not raw_logs: return []
        
        logs_text = "\n".join(raw_logs)
        prompt = f"""
        以下のテキストは、あるユーザーの今日一日の活動ログ（メモ、作業記録、完了タスク）の断片です。
        これらを分析し、**重複を統合**し、**意味のある「出来事」のリスト**として整理してください。

        # 指示
        - 単なる作業記録（例: "10:00 - 11:00 作業A"）は、"作業Aを行った" のように自然な日本語の項目にしてください。
        - 些細なメモも、文脈から重要な出来事であればリストに含めてください。
        - **出力はJSON形式のリスト（文字列の配列）のみ**を行ってください。余計な説明は不要です。
        - 最大で25項目程度に収めてください。

        # ログデータ
        {logs_text}
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            json_match = re.search(r'\[.*\]', response.text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            else:
                return raw_logs[:25]
        except Exception as e:
            logging.error(f"AI organization error: {e}")
            return raw_logs[:25]

    # --- Flow Start ---

    async def start_nightly_review_flow(self, interaction: discord.Interaction):
        """夜の振り返りフロー開始"""
        raw_logs = await self._get_todays_all_logs()
        
        if not raw_logs:
            view = JournalSelectionView(self, [])
            await interaction.followup.send("今日のログは見つかりませんでした。手動で出来事を入力してください。", view=view)
            return

        await interaction.followup.send("🤖 今日のログを整理しています...", ephemeral=True)
        organized_events = await self._organize_logs_with_ai(raw_logs)

        view = JournalSelectionView(self, organized_events)
        embed = discord.Embed(title="🌙 夜の振り返り", description="ジャーナルに記録したい出来事を選択してください。", color=discord.Color.purple())
        embed.set_footer(text="選択後、「次へ」を押して振り返りを入力します。")
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # --- Task Loops ---

    @tasks.loop()
    async def prompt_daily_journal(self):
        if not self.is_ready: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        try:
            embed = discord.Embed(title="🌙 夜の振り返り", description="今日一日を振り返りましょう。\n下のボタンを押して開始してください。", color=discord.Color.purple())
            view = NightlyJournalView(self)
            await channel.send(embed=embed, view=view)
        except Exception as e:
            logging.error(f"Journal prompt error: {e}")

    # --- Core Logic ---

    async def _process_unified_journal(self, interaction: discord.Interaction, selected_logs: list[str], feelings: str, wins: str, learnings: str):
        """統合ジャーナルの生成と保存（ライフログ分析を含む）"""
        if not self.is_ready: return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        
        # 1. 出来事リスト (ユーザー選択)
        events_text = "\n".join([f"- {log}" for log in selected_logs]) if selected_logs else "(特になし)"

        # 2. ライフログ (時間記録) の取得
        life_logs_content = await self._get_todays_life_logs_content()

        # 3. ユーザーの振り返りテキスト
        reflection_content = f"""
**感想:**
{feelings}

**Wins (うまくいったこと):**
{wins}

**Learnings (学んだこと):**
{learnings}
"""

        # AI生成
        try:
            prompt = f"""
            あなたはユーザーの優秀なコーチかつアナリストです。
            以下の情報を元に、**今日一日の包括的なジャーナル（日誌）**を作成してください。
            これまでの「ライフログ分析（客観的事実・時間の使い方の傾向）」と「ユーザーの主観的な振り返り」を統合し、シンプルで洞察に富んだ内容にしてください。

            # 入力情報

            ## 【A】ライフログ（作業時間の記録）
            {life_logs_content if life_logs_content else "(記録なし)"}

            ## 【B】今日の主な出来事（ユーザー選択）
            {events_text}

            ## 【C】ユーザーの振り返り
            {reflection_content}

            # 指示
            以下の2つのセクションで構成されるMarkdownテキストを出力してください。

            ### 1. Daily Journal
            - 今日の活動の要約と、ユーザーの振り返りを統合して記述してください。
            - ライフログから読み取れる客観的な事実（総作業時間や、集中できた時間帯、時間の使い方の傾向など）を織り交ぜてください。
            - ユーザーが挙げたWinsやLearningsを強調し、ポジティブに締めくくってください。

            ### 2. Feedback & Insights
            - ユーザーへのフィードバックや、明日への具体的なアドバイスを記述してください。
            - 時間の使い方に関する改善点があれば指摘してください。

            # 出力例
            ### Daily Journal
            今日は合計約8時間の作業を行い、特に午前中の「企画書作成」に集中できていました。午後は会議が続きましたが...（振り返り内容を統合）...という気付きも得られました。

            ### Feedback & Insights
            お疲れ様でした。午前中の集中力は素晴らしいです。午後の...について、明日は...を試してみると良いでしょう。
            """
            
            response = await self.gemini_model.generate_content_async(prompt)
            ai_content = response.text.strip()
        except Exception as e:
            logging.error(f"AI Journal Generation Error: {e}")
            ai_content = f"⚠️ AI生成に失敗しました。\n\n{reflection_content}"

        # 3. Obsidianに保存する完全なコンテンツを作成
        full_journal_content = f"""
{ai_content}

### User Reflections
#### Feelings
{feelings}
#### Wins
{wins}
#### Learnings
{learnings}

### Key Events (Source)
{events_text}
"""

        # Obsidianに保存
        success = await self._save_to_obsidian(date_str, full_journal_content, "## Journal")

        # 結果送信 (DiscordにはAI生成部分を表示)
        embed = discord.Embed(title=f"📓 統合ジャーナル ({date_str})", color=discord.Color.purple())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        
        embed.description = ai_content[:4000]
        
        footer_text = "Obsidianに保存しました" if success else "⚠️ 保存に失敗しました"
        embed.set_footer(text=f"{footer_text} | {now.strftime('%H:%M')}")
        
        await interaction.followup.send(embed=embed)

    async def _save_to_obsidian(self, date_str: str, content_to_add: str, section: str) -> bool:
        path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, path)
                current = res.content.decode('utf-8')
            except: current = ""
            
            new_content = update_section(current, content_to_add, section)
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), path, mode=WriteMode('overwrite'))
            return True
        except Exception as e:
            logging.error(f"Obsidian save error: {e}")
            return False

    async def _load_schedule_from_db(self, path):
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, path)
            return json.loads(res.content.decode('utf-8'))
        except: return None

    async def _save_schedule_to_db(self, path, hour, minute):
        if not self.dbx: return
        try:
            data = {"hour": hour, "minute": minute}
            content = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
            await asyncio.to_thread(self.dbx.files_upload, content, path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"Schedule save error: {e}")
            raise

    @app_commands.command(name="set_journal_time", description="夜の振り返り（ジャーナル）の通知時刻を設定します。")
    @app_commands.describe(schedule_time="設定する時刻 (HH:MM形式, 24時間表記)。例: 22:00")
    async def set_journal_time(self, interaction: discord.Interaction, schedule_time: str):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ実行できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        match = re.match(r'^([0-2]?[0-9]):([0-5]?[0-9])$', schedule_time.strip())
        if not match:
            await interaction.followup.send(f"❌ 時刻の形式が正しくありません。`HH:MM` (例: `22:30`) で入力してください。", ephemeral=True)
            return

        try:
            hour = int(match.group(1))
            minute = int(match.group(2))
            
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                 raise ValueError("時刻の範囲が不正です")

            # Dropboxに保存
            await self._save_schedule_to_db(self.journal_schedule_path, hour, minute)

            # タスクのスケジュール変更
            new_time = time(hour=hour, minute=minute, tzinfo=JST)
            self.prompt_daily_journal.change_interval(time=new_time)
            
            if not self.prompt_daily_journal.is_running():
                self.prompt_daily_journal.start()

            await interaction.followup.send(f"✅ 夜の振り返り通知を毎日 **{hour:02d}:{minute:02d}** に設定しました。", ephemeral=True)

        except ValueError:
             await interaction.followup.send("❌ 正しい時刻を入力してください（例: 23:59）。", ephemeral=True)
        except Exception as e:
            logging.error(f"Set journal time error: {e}", exc_info=True)
            await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))