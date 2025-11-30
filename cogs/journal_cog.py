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

# --- UIコンポーネント ---

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
            await self.cog._save_planning_entry(
                interaction,
                self.highlight.value,
                self.schedule.value
            )

            schedule_text = self.schedule.value
            if schedule_text:
                schedule_list = self.cog._parse_schedule_text(schedule_text)
                now = datetime.now(JST)
                today = now.date()
                if await self.cog._register_schedule_to_calendar(interaction, schedule_list, today):
                    await interaction.followup.send("✅ Googleカレンダーに追加しました。", ephemeral=True)
                else:
                    await interaction.followup.send("⚠️ カレンダーへの追加に失敗しました。", ephemeral=True)

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
            events = await self.cog._get_todays_events()
            event_text = "\n".join([f"{e['start'].get('dateTime','')[11:16] or '終日'} {e['summary']}" for e in events]) or "予定なし"
            log_summary = "(昨日のサマリー取得中...)" # 必要に応じて実装

            await interaction.response.send_modal(
                MorningPlanningModal(self.cog, event_text, log_summary)
            )
        except Exception as e:
             await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

class NightlyReviewModal(discord.ui.Modal, title="夜の振り返り"):
    wins = discord.ui.TextInput(
        label="良かったこと (Wins)",
        style=discord.TextStyle.paragraph,
        placeholder="今日うまくいったことは？",
        required=True
    )
    learnings = discord.ui.TextInput(
        label="学んだこと (Learnings)",
        style=discord.TextStyle.paragraph,
        placeholder="今日学んだことや気付きは？",
        required=True
    )
    todays_events = discord.ui.TextInput(
        label="出来事 / ハイライトの結果",
        style=discord.TextStyle.paragraph,
        required=False
    )
    tomorrows_schedule = discord.ui.TextInput(
        label="明日の予定",
        style=discord.TextStyle.paragraph,
        placeholder="10:00 ミーティング",
        required=False,
        max_length=1000
    )
    
    def __init__(self, cog):
        super().__init__(timeout=1800)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            await self.cog._save_journal_entry(
                interaction, 
                self.wins.value, 
                self.learnings.value, 
                self.todays_events.value,
                self.tomorrows_schedule.value
            )
        except Exception as e:
             logging.error(f"Journal error: {e}", exc_info=True)
             await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

class NightlyJournalView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="今日を振り返る", style=discord.ButtonStyle.primary, emoji="📝", custom_id="journal_nightly_review")
    async def write_journal(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(NightlyReviewModal(self.cog))
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

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
            self.bot.add_view(NightlyJournalView(self))
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

    async def _get_todays_lifelog_content(self) -> str:
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"

        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            content = res.content.decode('utf-8')
            
            match = re.search(r'##\s*Life\s*Logs\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
            if match and match.group(1).strip():
                return match.group(1).strip()
            else:
                return "(ログはまだありません)"
        except ApiError:
            return "(ノートが見つかりません)"
        except Exception as e:
            return "(ログの取得エラー)"

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
        if not self.is_ready: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        try:
            todays_log = await self._get_todays_lifelog_content()
            embed = discord.Embed(title="🌙 夜の振り返り", description="今日一日を振り返りましょう。", color=discord.Color.purple())
            display_log = todays_log[:1000] + "..." if len(todays_log) > 1000 else todays_log
            embed.add_field(name="⏱️ ライフログ", value=f"```markdown\n{display_log}\n```", inline=False)
            view = NightlyJournalView(self)
            await channel.send(embed=embed, view=view)
        except Exception as e:
            logging.error(f"Journal prompt error: {e}")

    def _format_bullet_list(self, text: str, indent: str = "") -> str:
        if not text: return ""
        lines = []
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line: continue
            if not line.startswith(('-', '*', '+')):
                lines.append(f"{indent}- {line}")
            else:
                lines.append(f"{indent}{line}")
        return "\n".join(lines)

    async def _save_planning_entry(self, interaction: discord.Interaction, highlight: str, schedule: str):
        if not self.is_ready: return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')

        # Obsidianの見出しは英語 (## Planning)
        planning_content = f"- **Highlight:** {highlight}\n### Schedule\n{schedule}"
        success_obsidian = await self._save_to_obsidian(date_str, planning_content, "## Planning")
        
        # Discordの表示は日本語
        embed = discord.Embed(title=f"☀️ プランニング ({date_str})", color=discord.Color.orange())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.add_field(name=f"{HIGHLIGHT_EMOJI} 今日のハイライト", value=highlight, inline=False)
        embed.add_field(name="📅 スケジュール", value=f"```{schedule}```", inline=False)
        
        footer_text = "Obsidianに保存しました" if success_obsidian else "⚠️ Obsidianへの保存に失敗"
        embed.set_footer(text=f"{footer_text} | {now.strftime('%H:%M')}")

        await interaction.followup.send(embed=embed)

    async def _save_journal_entry(self, interaction: discord.Interaction, wins: str, learnings: str, todays_events: Optional[str], tomorrows_schedule: Optional[str]):
        if not self.is_ready: return

        formatted_wins = self._format_bullet_list(wins)
        formatted_learnings = self._format_bullet_list(learnings)
        formatted_events = self._format_bullet_list(todays_events)
        
        obsidian_wins = self._format_bullet_list(wins, indent="\t\t")
        obsidian_learnings = self._format_bullet_list(learnings, indent="\t\t")
        obsidian_events = self._format_bullet_list(todays_events, indent="\t\t")

        ai_comment = "(AIコメント生成失敗)"
        try:
            prompt = f"""あなたはコーチです。ユーザーの日報に対して、ポジティブで次につながるフィードバックを日本語で、300文字以内で記述してください。
# ユーザーの振り返り
## Wins (良かったこと)
{formatted_wins}
## Learnings (学んだこと)
{formatted_learnings}
## Events (出来事)
{formatted_events}
"""
            response = await self.gemini_model.generate_content_async(prompt)
            ai_comment = response.text.strip()
        except Exception: pass

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        
        # Obsidianの見出し・キーは英語 (## Journal, **Wins:**, etc.)
        journal_content = f"- {now.strftime('%H:%M')}\n\t- **Wins:**\n{obsidian_wins}\n\t- **Learnings:**\n{obsidian_learnings}\n"
        if obsidian_events: journal_content += f"\t- **Events:**\n{obsidian_events}"

        success_obsidian = await self._save_to_obsidian(date_str, journal_content, "## Journal")

        success_calendar = True
        if tomorrows_schedule:
            schedule_list = self._parse_schedule_text(tomorrows_schedule)
            tomorrow = (now + timedelta(days=1)).date()
            if not await self._register_schedule_to_calendar(interaction, schedule_list, tomorrow):
                success_calendar = False

        # Discordの表示は日本語
        embed = discord.Embed(title=f"🌙 振り返り ({date_str})", color=discord.Color.purple())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="🌟 良かったこと (Wins)", value=formatted_wins or "なし", inline=False)
        embed.add_field(name="💡 学んだこと (Learnings)", value=formatted_learnings or "なし", inline=False)
        if formatted_events: embed.add_field(name="📍 出来事 (Events)", value=formatted_events, inline=False)
        embed.add_field(name="🤖 AIコーチ", value=ai_comment, inline=False)
        
        status_text = []
        if not success_obsidian: status_text.append("⚠️ Obsidian保存失敗")
        if not success_calendar: status_text.append("⚠️ カレンダー更新失敗")
        if not status_text: status_text.append("Obsidianに保存しました")
        
        embed.set_footer(text=f"{' | '.join(status_text)} | {now.strftime('%H:%M')}")
        await interaction.followup.send(embed=embed)

    async def _save_to_obsidian(self, date_str: str, content_to_add: str, section: str) -> bool:
        path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, path)
                current = res.content.decode('utf-8')
            except: current = f"# {date_str}\n"
            
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

                event = {
                    'summary': summary,
                    'start': {'dateTime': start_dt.isoformat()},
                    'end': {'dateTime': end_dt.isoformat()},
                }
                await asyncio.to_thread(self.calendar_service.events().insert(calendarId=self.google_calendar_id, body=event).execute)
            return True
        except Exception as e:
            logging.error(f"Calendar error: {e}")
            return False

    async def _load_schedule_from_db(self, path):
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, path)
            return json.loads(res.content.decode('utf-8'))
        except: return None

async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))