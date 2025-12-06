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
HIGHLIGHT_EMOJI = "✨"
BASE_PATH = os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')
PLANNING_SCHEDULE_PATH = f"{BASE_PATH}/.bot/planning_schedule.json"
JOURNAL_SCHEDULE_PATH = f"{BASE_PATH}/.bot/journal_schedule.json"
TIME_SCHEDULE_REGEX = re.compile(r'^(\d{1,2}:\d{2}|\d{1,4})(?:[~-](\d{1,2}:\d{2}|\d{1,4}))?\s+(.+)$')

# ==========================================
# UI Components
# ==========================================

# --- 朝のプランニング用 ---
class MorningPlanningModal(discord.ui.Modal, title="朝のプランニング"):
    highlight = discord.ui.TextInput(
        label="今日のハイライト",
        style=discord.TextStyle.short,
        placeholder="例: プロジェクトAを完了させる",
        required=True
    )
    
    schedule = discord.ui.TextInput(
        label="今日のスケジュール",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500
    )
    
    log_summary_display = discord.ui.TextInput(
        label="昨日のサマリー（参考）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1500
    )

    def __init__(self, cog, existing_schedule_text: str, log_summary: str):
        super().__init__(timeout=1800)
        self.cog = cog
        self.schedule.default = existing_schedule_text
        self.log_summary_display.default = log_summary

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            # Obsidianへの保存
            await self.cog._save_planning_entry(
                interaction,
                self.highlight.value,
                self.schedule.value
            )

            # カレンダーへの同期
            schedule_text = self.schedule.value
            if schedule_text:
                schedule_list = self.cog._parse_schedule_text(schedule_text)
                now = datetime.now(JST)
                today = now.date()
                if await self.cog._register_schedule_to_calendar(interaction, schedule_list, today):
                    await interaction.followup.send("✅ Googleカレンダーを同期（更新）しました。", ephemeral=True)
                else:
                    await interaction.followup.send("⚠️ カレンダーの同期に失敗しました。", ephemeral=True)
        except Exception as e:
             logging.error(f"Planning error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)

class MorningPlanningView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="一日の計画を立てる", style=discord.ButtonStyle.success, emoji="☀️", custom_id="journal_morning_plan")
    async def plan_day(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # 今日の予定を取得（編集用デフォルト値）
            events = await self.cog._get_todays_events()
            event_text = "\n".join([f"{e['start'].get('dateTime','')[11:16] or '終日'} {e['summary']}" for e in events]) or ""
            
            # 昨日のサマリーを取得（参考情報）
            yesterday = datetime.now(JST).date() - timedelta(days=1)
            log_summary = await self.cog._get_daily_summary_content(yesterday)

            await interaction.response.send_modal(
                MorningPlanningModal(self.cog, event_text, log_summary)
            )
        except Exception as e:
             logging.error(f"Plan day error: {e}")
             await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

# --- 夜の振り返り用 (Unified) ---

class JournalSelectionView(discord.ui.View):
    """一日のログからジャーナルに含める項目を選択するView"""
    def __init__(self, cog, logs: list[str]):
        super().__init__(timeout=None)
        self.cog = cog
        self.logs = logs
        self.current_selection = []

        # Discordのセレクトメニューは最大25件まで
        recent_logs = logs[-25:]
        options = []
        for i, log in enumerate(recent_logs):
            # 表示用に整形（Markdown記号や時刻の除去）
            clean_label = re.sub(r'^[-*+]\s*(\d{2}:\d{2})?\s*', '', log).strip()
            if len(clean_label) > 95: clean_label = clean_label[:95] + "..."
            if not clean_label: clean_label = "Log Item"
            
            # valueはインデックス
            options.append(discord.SelectOption(label=clean_label, value=str(i), default=False))

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
        # 選択状態を一時保存（まだ確定しない）
        await interaction.response.defer()
        self.current_selection = [int(v) for v in interaction.data["values"]]

    @discord.ui.button(label="次へ (感想を入力)", style=discord.ButtonStyle.primary, row=1)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 選択されたログのテキストを取得
        recent_logs = self.logs[-25:]
        selected_text = [recent_logs[i] for i in self.current_selection]
        
        await interaction.response.send_modal(JournalReflectionModal(self.cog, selected_text))

class JournalReflectionModal(discord.ui.Modal, title="夜の振り返り (Unified)"):
    reflection = discord.ui.TextInput(
        label="今日のコメント / 総括",
        style=discord.TextStyle.paragraph,
        placeholder="今日の出来事について感じたことや、明日の目標などを自由に記述してください。",
        required=True
    )

    def __init__(self, cog, selected_logs):
        super().__init__(timeout=1800)
        self.cog = cog
        self.selected_logs = selected_logs

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False, thinking=True)
        await self.cog._process_unified_journal(interaction, self.selected_logs, self.reflection.value)


# ==========================================
# Cog Class
# ==========================================

class JournalCog(commands.Cog):
    """朝のプランニングと夜の振り返りを行うCog"""

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

            self.planning_schedule_path = PLANNING_SCHEDULE_PATH
            self.journal_schedule_path = JOURNAL_SCHEDULE_PATH

            self.google_creds = self._get_google_creds()
            self.calendar_service = build('calendar', 'v3', credentials=self.google_creds) if self.google_creds else None
            
            self.is_ready = True
            logging.info("JournalCog initialized.")
        except Exception as e:
            logging.error(f"JournalCog init failed: {e}")

    def _load_env_vars(self):
        self.channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.google_calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        required = ["JOURNAL_CHANNEL_ID", "GOOGLE_CALENDAR_ID", "GEMINI_API_KEY", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"]
        if not all(getattr(self, name.lower(), None) or (name == "JOURNAL_CHANNEL_ID" and self.channel_id) for name in required):
            return False
        return True

    def _get_google_creds(self):
        if not os.path.exists('token.json'): return None
        try:
            creds = Credentials.from_authorized_user_file('token.json', ['https://www.googleapis.com/auth/calendar'])
            if not creds.valid:
                if creds.expired and creds.refresh_token: creds.refresh(Request())
                else: return None
            return creds
        except: return None

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            self.bot.add_view(MorningPlanningView(self))
            await self.bot.wait_until_ready()
            
            for path, task in [(self.planning_schedule_path, self.daily_planning_task), (self.journal_schedule_path, self.prompt_daily_journal)]:
                sched = await self._load_schedule_from_db(path)
                if sched:
                    task.change_interval(time=time(hour=sched['hour'], minute=sched['minute'], tzinfo=JST))
                    if not task.is_running(): task.start()

    async def cog_unload(self):
        if self.session: await self.session.close()
        self.daily_planning_task.cancel()
        self.prompt_daily_journal.cancel()

    # --- Helper Methods ---

    async def _get_daily_summary_content(self, target_date: date) -> str:
        """指定日のJournalセクション（旧Daily Summary）を取得する"""
        if not self.dbx: return "(Dropbox接続エラー)"
        
        date_str = target_date.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            content = res.content.decode('utf-8')
            # "## Journal" から次の見出しまたはファイル末尾までを抽出
            match = re.search(r'##\s*Journal\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
            if match and match.group(1).strip():
                return match.group(1).strip()
            return "(昨日のジャーナルはありません)"
        except ApiError:
            return "(昨日のノートが見つかりません)"
        except Exception as e:
            logging.error(f"Summary fetch error: {e}")
            return "(取得エラー)"

    async def _get_todays_all_logs(self) -> list[str]:
        """今日のすべてのログ（Memo, Life Logs, Todo）を取得してリスト化する"""
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        logs = []
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            content = res.content.decode('utf-8')
            
            # 各セクションから行を抽出
            for header in ["## Memo", "## Life Logs", "## Completed Tasks"]:
                match = re.search(rf'{header}\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
                if match:
                    section_text = match.group(1).strip()
                    for line in section_text.split('\n'):
                        if line.strip():
                            logs.append(line.strip())
        except ApiError:
            pass # ノートがない場合は空リスト
        except Exception as e:
            logging.error(f"Log fetch error: {e}")
        
        return logs

    # --- Task Loops ---

    @tasks.loop()
    async def daily_planning_task(self):
        if not self.is_ready: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        try:
            events = await self._get_todays_events()
            event_text = "\n".join([f"{e['start'].get('dateTime','')[11:16] or '終日'} {e['summary']}" for e in events]) or "予定なし"
            view = MorningPlanningView(self)
            
            embed = discord.Embed(title="☀️ 朝のプランニング", description="今日一日の計画を立てましょう。", color=discord.Color.orange())
            embed.add_field(name="📅 カレンダー", value=f"```\n{event_text}\n```", inline=False)
            await channel.send(embed=embed, view=view)
        except Exception as e:
            logging.error(f"Planning task error: {e}")

    @tasks.loop()
    async def prompt_daily_journal(self):
        """夜の振り返りプロンプト：ログを表示し、選択を促す"""
        if not self.is_ready: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        try:
            logs = await self._get_todays_all_logs()
            
            if not logs:
                # ログがない場合は直接入力モーダルへ誘導するボタンを表示（ここでは簡易的にメッセージのみ）
                embed = discord.Embed(title="🌙 夜の振り返り", description="今日のログは見つかりませんでした。手動で振り返りを行いますか？", color=discord.Color.purple())
                # View with just a button to open modal (skipping selection)
                # (For simplicity, reusing the logic by passing empty logs which JournalSelectionView handles or adding a special button)
                view = JournalSelectionView(self, []) 
                await channel.send(embed=embed, view=view)
            else:
                embed = discord.Embed(title="🌙 夜の振り返り", description="今日一日の活動から、ジャーナルに含めたい項目を選択してください。", color=discord.Color.purple())
                # ログのプレビューを表示
                log_preview = "\n".join(logs[:15])
                if len(logs) > 15: log_preview += f"\n... (他 {len(logs)-15} 件)"
                embed.add_field(name="📝 今日のログ", value=f"```markdown\n{log_preview}\n```", inline=False)
                
                view = JournalSelectionView(self, logs)
                await channel.send(embed=embed, view=view)

        except Exception as e:
            logging.error(f"Journal prompt error: {e}")

    # --- Core Logic ---

    async def _save_planning_entry(self, interaction: discord.Interaction, highlight: str, schedule: str):
        if not self.is_ready: return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')

        planning_content = f"- **Highlight:** {highlight}\n### Schedule\n{schedule}"
        success_obsidian = await self._save_to_obsidian(date_str, planning_content, "## Planning")
        
        embed = discord.Embed(title=f"☀️ プランニング ({date_str})", color=discord.Color.orange())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.add_field(name=f"{HIGHLIGHT_EMOJI} 今日のハイライト", value=highlight, inline=False)
        embed.add_field(name="📅 スケジュール", value=f"```{schedule}```", inline=False)
        
        footer_text = "Obsidianに保存しました" if success_obsidian else "⚠️ Obsidianへの保存に失敗"
        embed.set_footer(text=f"{footer_text} | {now.strftime('%H:%M')}")

        await interaction.followup.send(embed=embed)

    async def _process_unified_journal(self, interaction: discord.Interaction, selected_logs: list[str], reflection: str):
        """選択されたログと振り返りコメントを統合してAIサマリーを生成し、保存する"""
        if not self.is_ready: return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        
        # ログの整形
        logs_text = "\n".join(selected_logs) if selected_logs else "(選択されたログなし)"

        # AI生成
        try:
            prompt = f"""
            あなたはユーザーの優秀な秘書であり、コーチです。
            ユーザーが選択した「今日の主要な活動ログ」と「ユーザー自身の振り返りコメント」を元に、
            **一日の統合ジャーナル（Unified Journal）**を作成してください。

            # 入力データ
            ## 選択された活動ログ
            {logs_text}

            ## ユーザーの振り返り
            {reflection}

            # 指示
            - 今日の成果（Wins）、学び（Learnings）、反省点などを統合し、ストーリー性のある一つの文章、あるいは構造化されたセクションとしてまとめてください。
            - ユーザーの振り返りコメントを最大限尊重し、そこにログの客観的事実を補足する形で記述してください。
            - 次のアクションや、明日へのモチベーションにつながるフィードバックを一言添えてください。
            - 出力はMarkdown形式で行ってください（見出しは `###` から始めてください）。

            """
            response = await self.gemini_model.generate_content_async(prompt)
            journal_content = response.text.strip()
        except Exception as e:
            logging.error(f"AI Journal Generation Error: {e}")
            journal_content = f"⚠️ AI生成に失敗しました。\n\n**コメント:**\n{reflection}\n\n**ログ:**\n{logs_text}"

        # Obsidianに保存 (## Journal セクションに統一)
        # 以前の ## Daily Summary などは統合するため、ここでは ## Journal のみを使用
        success = await self._save_to_obsidian(date_str, journal_content, "## Journal")

        # 結果送信
        embed = discord.Embed(title=f"📓 統合ジャーナル ({date_str})", color=discord.Color.purple())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.description = journal_content[:4000] # Limit for Discord
        
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

    async def _get_todays_events(self):
        if not self.calendar_service: return []
        try:
            now = datetime.now(JST)
            start = now.replace(hour=0, minute=0, second=0).isoformat()
            end = now.replace(hour=23, minute=59, second=59).isoformat()
            res = await asyncio.to_thread(self.calendar_service.events().list(calendarId=self.google_calendar_id, timeMin=start, timeMax=end, singleEvents=True, orderBy='startTime').execute)
            return res.get('items', [])
        except: return []

    def _parse_schedule_text(self, text):
        events = []
        for line in text.split('\n'):
            line = line.strip()
            if not line: continue
            m = TIME_SCHEDULE_REGEX.match(line)
            if m:
                start, end, summary = m.groups()
                events.append({"start_time": start, "end_time": end or start, "summary": summary})
        return events

    async def _register_schedule_to_calendar(self, interaction, schedule_list, target_date):
        if not self.calendar_service: return False
        try:
            start_check = datetime.combine(target_date, time.min).replace(tzinfo=JST).isoformat()
            end_check = datetime.combine(target_date, time.max).replace(tzinfo=JST).isoformat()
            
            existing_events_result = await asyncio.to_thread(
                self.calendar_service.events().list(
                    calendarId=self.google_calendar_id, 
                    timeMin=start_check, 
                    timeMax=end_check, 
                    singleEvents=True
                ).execute
            )
            existing_items = existing_events_result.get('items', [])

            new_events_payloads = []
            new_event_signatures = set()

            for item in schedule_list:
                start_str = item["start_time"]
                end_str = item["end_time"]
                summary = item["summary"]

                def parse_time_str(t_str):
                    if ':' in t_str: return datetime.strptime(t_str, "%H:%M").time()
                    elif len(t_str) == 3: return datetime.strptime(t_str, "%H%M").time()
                    elif len(t_str) == 4: return datetime.strptime(t_str, "%H%M").time()
                    return None

                start_time = parse_time_str(start_str)
                end_time = parse_time_str(end_str)
                if not start_time: continue
                if not end_time: end_time = start_time

                start_dt = datetime.combine(target_date, start_time).replace(tzinfo=JST)
                end_dt = datetime.combine(target_date, end_time).replace(tzinfo=JST)
                if end_dt < start_dt: end_dt += timedelta(days=1)
                if end_dt == start_dt: end_dt += timedelta(hours=1)

                event_body = {
                    'summary': summary,
                    'start': {'dateTime': start_dt.isoformat()},
                    'end': {'dateTime': end_dt.isoformat()},
                }
                new_events_payloads.append(event_body)
                sig = f"{start_dt.strftime('%H:%M')} {summary}"
                new_event_signatures.add(sig)

            # 削除処理
            for e in existing_items:
                start_val = e.get('start', {}).get('dateTime')
                if not start_val: continue
                dt_obj = datetime.fromisoformat(start_val)
                sig = f"{dt_obj.strftime('%H:%M')} {e.get('summary', '')}"

                if sig not in new_event_signatures:
                    await asyncio.to_thread(
                        self.calendar_service.events().delete(
                            calendarId=self.google_calendar_id, 
                            eventId=e['id']
                        ).execute
                    )

            # 追加処理
            existing_signatures_now = set()
            for e in existing_items:
                start_val = e.get('start', {}).get('dateTime')
                if start_val:
                    dt_obj = datetime.fromisoformat(start_val)
                    sig = f"{dt_obj.strftime('%H:%M')} {e.get('summary', '')}"
                    existing_signatures_now.add(sig)

            for payload in new_events_payloads:
                dt_obj = datetime.fromisoformat(payload['start']['dateTime'])
                sig = f"{dt_obj.strftime('%H:%M')} {payload['summary']}"
                
                if sig in existing_signatures_now:
                    continue

                await asyncio.to_thread(
                    self.calendar_service.events().insert(
                        calendarId=self.google_calendar_id, 
                        body=payload
                    ).execute
                )
            return True
        except Exception as e:
            logging.error(f"Calendar sync error: {e}")
            return False

    async def _load_schedule_from_db(self, path):
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, path)
            return json.loads(res.content.decode('utf-8'))
        except: return None

async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))