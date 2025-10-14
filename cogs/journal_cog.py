import os
import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, time, timedelta, date
import zoneinfo
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import aiohttp
import openai
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import asyncio
import jpholiday
import json

# --- 共通関数をインポート ---
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
PLANNING_PROMPT_TIME = time(hour=7, minute=30, tzinfo=JST)
JOURNAL_PROMPT_TIME = time(hour=21, minute=30, tzinfo=JST)
IDLE_CHECK_INTERVAL_HOURS = 1
HIGHLIGHT_EMOJI = "✨"
SUPPORTED_AUDIO_TYPES = ['audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm']

# --- UIコンポーネント ---

class HighlightInputModal(discord.ui.Modal, title="ハイライトの手動入力"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
    highlight_text = discord.ui.TextInput(label="今日のハイライトを入力してください", style=discord.TextStyle.short, required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.set_highlight_on_calendar(self.highlight_text.value, interaction)
        await interaction.followup.send(f"✅ ハイライト「**{self.highlight_text.value}**」を設定しました。", ephemeral=True)

class HighlightOptionsView(discord.ui.View):
    def __init__(self, cog, event_options: list):
        super().__init__(timeout=3600)
        self.cog = cog
        
        if event_options:
            self.add_item(discord.ui.Select(
                placeholder="今日の予定からハイライトを選択...",
                options=event_options,
                custom_id="select_highlight_from_calendar"
            ))
        self.add_item(discord.ui.Button(label="その他のハイライトを入力", style=discord.ButtonStyle.primary, custom_id="input_other_highlight"))

    async def interaction_check(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id")
        if custom_id == "select_highlight_from_calendar":
            selected_highlight = interaction.data["values"][0]
            await interaction.response.defer(ephemeral=True)
            await self.cog.set_highlight_on_calendar(selected_highlight, interaction)
            await interaction.followup.send(f"✅ ハイライト「**{selected_highlight}**」を設定しました。", ephemeral=True)
            self.stop()
            await interaction.message.edit(view=None)
        
        elif custom_id == "input_other_highlight":
            modal = HighlightInputModal(self.cog)
            await interaction.response.send_modal(modal)
            self.stop()
            await interaction.message.edit(view=None)
        return True

class ScheduleInputModal(discord.ui.Modal, title="今日の予定を入力"):
    tasks_input = discord.ui.TextInput(
        label="今日の予定を改行区切りで入力",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n- 読書\n- 1時間の散歩\n- 昼寝 30分\n- 買い物",
        required=True
    )
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.process_schedule(interaction, self.tasks_input.value)

class ScheduleConfirmView(discord.ui.View):
    def __init__(self, cog, proposed_schedule: list):
        super().__init__(timeout=1800)
        self.cog = cog
        self.schedule = proposed_schedule
    
    @discord.ui.button(label="この内容でカレンダーに登録", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.register_schedule_to_calendar(interaction, self.schedule)
        self.stop()
        await interaction.message.edit(content="✅ 予定をGoogleカレンダーに登録しました。次に今日一日を象徴する**ハイライト**を決めましょう。", view=None, embed=None)
        await self.cog._ask_for_highlight(interaction.channel)

    @discord.ui.button(label="修正する", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("お手数ですが、再度ボタンを押して予定を再入力してください。", ephemeral=True)
        self.stop()
        await interaction.message.delete()

# --- シンプルなジャーナルUI ---
class SimpleJournalModal(discord.ui.Modal, title="今日一日の振り返り"):
    pass
class SimpleJournalView(discord.ui.View):
    pass

# --- Cog本体 ---
class JournalCog(commands.Cog):
    """朝の計画と夜の振り返りを支援するCog"""

    def __init__(self, bot: commands.Bot):
        pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.daily_planning_task.is_running(): self.daily_planning_task.start()
            if not self.prompt_daily_journal.is_running(): self.prompt_daily_journal.start()
            if not self.check_idle_time_loop.is_running(): self.check_idle_time_loop.start()

    async def cog_unload(self):
        await self.session.close()
        self.daily_planning_task.cancel()
        self.prompt_daily_journal.cancel()
        self.check_idle_time_loop.cancel()

    # --- 朝の計画機能 (修正・統合) ---
    async def _get_todays_events(self) -> list:
        pass
    
    async def set_highlight_on_calendar(self, highlight_text: str, interaction: discord.Interaction):
        # ... (colorIdを'5'に設定)
        pass

    @tasks.loop(time=PLANNING_PROMPT_TIME)
    async def daily_planning_task(self):
        """毎日、朝の計画プロセスを開始する"""
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        
        self.idle_reminders_sent.clear()
        
        view = discord.ui.View(timeout=7200) # 2時間
        button = discord.ui.Button(label="1日の計画を立てる", style=discord.ButtonStyle.success, custom_id="plan_day")
        
        async def planning_callback(interaction: discord.Interaction):
            await interaction.response.send_modal(ScheduleInputModal(self))
            view.stop()
            await interaction.message.edit(content="AIがスケジュール案を作成中です...", view=None)

        button.callback = planning_callback
        view.add_item(button)
        await channel.send("おはようございます！有意義な一日を過ごすために、まず1日の計画を立てませんか？", view=view)

    async def _ask_for_highlight(self, channel: discord.TextChannel):
        """ハイライト選択のメッセージを送信する共通関数"""
        await asyncio.sleep(2) # カレンダー登録の反映待ち
        events = await self._get_todays_events()
        event_summaries = [e.get('summary', '名称未設定') for e in events if 'date' not in e.get('start', {}) and HIGHLIGHT_EMOJI not in e.get('summary', '')]
        
        description = "今日のハイライトを決めて、一日に集中する軸を作りましょう。\n\n"
        if event_summaries:
            description += "今日の予定リストからハイライトを選択するか、新しいハイライトを入力してください。"
        else:
            description += "ハイライトとして取り組みたいことを入力してください。"

        embed = discord.Embed(title=f"{HIGHLIGHT_EMOJI} 今日のハイライト決め", description=description, color=discord.Color.blue())
        event_options = [discord.SelectOption(label=s[:100], value=s[:100]) for s in event_summaries]
        view = HighlightOptionsView(self, event_options)
        
        await channel.send(embed=embed, view=view)

    async def process_schedule(self, interaction: discord.Interaction, tasks_text: str):
        """AIに予定リストを渡し、タイムスケジュール案を作成させる"""
        existing_events = await self._get_todays_events()
        events_context = "\n".join([f"- {e['summary']} (開始: {e.get('start', {}).get('dateTime', e.get('start', {}).get('date'))})" for e in existing_events])

        prompt = f"""
        あなたは優秀なパーソナルアシスタントです。以下のユーザーの予定リストと既存の予定を元に、最適なタイムスケジュールを提案してください。
        # 指示
        - 各タスクの所要時間を常識の範囲で推測してください（ユーザーが指定している場合はそれを優先）。
        - 既存の予定と重ならないように、各タスクの開始時刻と終了時刻を決定してください。
        - 移動時間や休憩時間も考慮し、無理のないスケジュールを作成してください。
        - 結果は必ず以下のJSON形式のリストで出力してください。説明文は不要です。
        # 既存の予定
        {events_context if events_context else "なし"}
        # ユーザーが今日やりたいことのリスト
        {tasks_text}
        # 出力形式 (JSONのみ)
        [
            {{"summary": "タスク名1", "start_time": "HH:MM", "end_time": "HH:MM"}},
            {{"summary": "タスク名2", "start_time": "HH:MM", "end_time": "HH:MM"}}
        ]
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            json_match = re.search(r'\[.*\]', response.text, re.DOTALL)
            if not json_match:
                await interaction.followup.send("AIによるスケジュール提案の生成に失敗しました。形式が正しくありません。", ephemeral=True)
                return

            proposed_schedule = json.loads(json_match.group(0))
            
            embed = discord.Embed(title="AIによるスケジュール提案", description="AIが作成した本日のスケジュール案です。これでよろしいですか？", color=discord.Color.green())
            for event in proposed_schedule:
                embed.add_field(name=event['summary'], value=f"{event['start_time']} - {event['end_time']}", inline=False)

            view = ScheduleConfirmView(self, proposed_schedule)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"スケジュールの解析中にエラーが発生しました: {e}", ephemeral=True)
            logging.error(f"AIスケジュール提案の処理エラー: {e}")

    async def register_schedule_to_calendar(self, interaction: discord.Interaction, schedule: list):
        pass

    # --- 空き時間リマインダー (休日のみ) ---
    @tasks.loop(hours=IDLE_CHECK_INTERVAL_HOURS)
    async def check_idle_time_loop(self):
        now = datetime.now(JST)
        today = now.date()
        # 休日かつ活動時間帯（9時～21時）のみ実行
        if (today.weekday() < 5 and not jpholiday.is_holiday(today)) or not (9 <= now.hour <= 21):
            return

        events = await self._get_todays_events()
        if not events: return
        
        sorted_events = sorted([e for e in events if e.get('start', {}).get('dateTime')], key=lambda e: e.get('start', {}).get('dateTime'))
        
        last_end_time = now
        for event in sorted_events:
            start_str = event.get('start', {}).get('dateTime')
            start_time = datetime.fromisoformat(start_str)
            
            if start_time < now:
                last_end_time = max(last_end_time, datetime.fromisoformat(event.get('end', {}).get('dateTime')))
                continue

            idle_duration = start_time - last_end_time
            if idle_duration >= timedelta(hours=2):
                reminder_key = f"{today.isoformat()}-{last_end_time.hour}"
                if reminder_key not in self.idle_reminders_sent:
                    channel = self.bot.get_channel(self.channel_id)
                    if channel:
                        await channel.send(f"💡 **空き時間のお知らせ**\n現在、**{last_end_time.strftime('%H:%M')}** から **{start_time.strftime('%H:%M')}** まで**約{int(idle_duration.total_seconds()/3600)}時間**の空きがあります。何か予定を入れませんか？")
                        self.idle_reminders_sent.add(reminder_key)
            
            last_end_time = max(last_end_time, datetime.fromisoformat(event.get('end', {}).get('dateTime')))

async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))