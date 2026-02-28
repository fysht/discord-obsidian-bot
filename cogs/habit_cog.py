import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import asyncio
import datetime

# --- リファクタリング: 定数とユーティリティのクリーンなインポート ---
from config import JST, BOT_FOLDER
from utils.obsidian_utils import update_frontmatter

HABIT_DATA_FILE = "habit_data.json"

class HabitAddModal(discord.ui.Modal, title="新しい習慣を追加"):
    habit_name = discord.ui.TextInput(
        label="習慣の名前",
        placeholder="例: 筋トレ, 読書10分, 薬を飲む",
        style=discord.TextStyle.short,
        required=True,
        max_length=50
    )

    def __init__(self, cog, view):
        super().__init__()
        self.cog = cog
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name = self.habit_name.value.strip()
        
        data = await self.cog._load_data()
        existing_ids = [int(h['id']) for h in data['habits']]
        new_id = str(max(existing_ids) + 1) if existing_ids else "1"
        
        data['habits'].append({
            "id": new_id,
            "name": name,
            "created_at": datetime.datetime.now(JST).isoformat(),
            "active": True
        })
        
        if await self.cog._save_data(data):
            await interaction.followup.send(f"✅ 習慣「{name}」を追加しました。", ephemeral=True)
            await self.view.refresh_view(interaction)
        else:
            await interaction.followup.send("❌ 保存に失敗しました。", ephemeral=True)

class HabitDeleteSelect(discord.ui.Select):
    def __init__(self, habits):
        options = []
        for h in habits:
            if h.get('active', True):
                options.append(discord.SelectOption(
                    label=h['name'],
                    value=h['id'],
                    description="この習慣を削除(アーカイブ)します"
                ))
        
        if not options:
            options.append(discord.SelectOption(label="削除可能な習慣がありません", value="none"))

        super().__init__(
            placeholder="削除する習慣を選択...",
            min_values=1,
            max_values=1,
            options=options,
            disabled=(len(options) == 0 or options[0].value == "none")
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        habit_id = self.values[0]
        if habit_id == "none": return

        await interaction.response.defer(ephemeral=True)
        data = await view.cog._load_data()
        
        target_name = ""
        for h in data['habits']:
            if h['id'] == habit_id:
                h['active'] = False
                target_name = h['name']
                break
        
        if await view.cog._save_data(data):
            await interaction.followup.send(f"🗑️ 「{target_name}」をリストから削除しました。", ephemeral=True)
            await view.refresh_view(interaction)
        else:
            await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True)

class HabitManagerView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def refresh_view(self, interaction: discord.Interaction = None, message: discord.Message = None):
        data = await self.cog._load_data()
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        daily_log = data['logs'].get(today_str, [])
        active_habits = [h for h in data['habits'] if h.get('active', True)]

        self.clear_items()
        
        for habit in active_habits:
            is_done = habit['id'] in daily_log
            style = discord.ButtonStyle.success if is_done else discord.ButtonStyle.secondary
            label = f"{'✅' if is_done else '⬜'} {habit['name']}"
            
            button = discord.ui.Button(style=style, label=label, custom_id=f"habit_{habit['id']}")
            button.callback = self.create_toggle_callback(habit['id'])
            self.add_item(button)

        add_btn = discord.ui.Button(label="➕ 追加", style=discord.ButtonStyle.primary, row=4)
        add_btn.callback = self.add_callback
        self.add_item(add_btn)
        
        self.add_item(HabitDeleteSelect(active_habits))

        embed = discord.Embed(
            title=f"📅 習慣トラッカー ({today_str})",
            description="Good Morning! ☀️\n今日も一日、良い習慣を積み重ねましょう。",
            color=discord.Color.green()
        )
        if active_habits:
            rate = int((len(daily_log) / len(active_habits)) * 100)
            embed.set_footer(text=f"今日の達成率: {rate}% ({len(daily_log)}/{len(active_habits)})")

        if interaction:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        elif message:
            await message.edit(embed=embed, view=self)

    def create_toggle_callback(self, habit_id):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await self.cog.toggle_habit(habit_id)
            await self.refresh_view(interaction)
        return callback

    async def add_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(HabitAddModal(self.cog, self))

class HabitCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        # --- リファクタリング: Bot本体のサービスを使い回す ---
        self.drive_service = bot.drive_service
        
        self.daily_task.start()

    async def _load_data(self):
        default = {"habits": [], "logs": {}}
        service = self.drive_service.get_service()
        if not service: return default
        
        b_folder = await self.drive_service.find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return default
        
        f_id = await self.drive_service.find_file(service, b_folder, HABIT_DATA_FILE)
        if f_id:
            try:
                content = await self.drive_service.read_text_file(service, f_id)
                return json.loads(content)
            except Exception: pass
        return default

    async def _save_data(self, data):
        service = self.drive_service.get_service()
        if not service: return False
        
        b_folder = await self.drive_service.find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: 
            b_folder = await self.drive_service.create_folder(service, self.drive_folder_id, BOT_FOLDER)
        
        f_id = await self.drive_service.find_file(service, b_folder, HABIT_DATA_FILE)
        json_str = json.dumps(data, ensure_ascii=False)
        
        if f_id:
            await self.drive_service.update_text(service, f_id, json_str, mime_type='application/json')
        else:
            await self.drive_service.upload_text(service, b_folder, HABIT_DATA_FILE, json_str, mime_type='application/json')
        return True

    async def toggle_habit(self, habit_id):
        data = await self._load_data()
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        
        if today_str not in data['logs']:
            data['logs'][today_str] = []
        
        current_log = data['logs'][today_str]
        
        if habit_id in current_log:
            current_log.remove(habit_id)
        else:
            current_log.append(habit_id)
            
        await self._save_data(data)
        await self._sync_to_obsidian_daily(data, today_str)

    async def _sync_to_obsidian_daily(self, data, date_str):
        service = self.drive_service.get_service()
        if not service: return

        daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: return

        f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
        
        content = f"# Daily Note {date_str}\n"
        if f_id:
            try:
                content = await self.drive_service.read_text_file(service, f_id)
            except Exception: pass

        daily_log = data['logs'].get(date_str, [])
        completed = [h['name'] for h in data['habits'] if h['id'] in daily_log]
        
        new_content = update_frontmatter(content, {"habits": completed})
        
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", new_content)

    async def get_weekly_stats_embed(self) -> discord.Embed:
        data = await self._load_data()
        
        active_habits = {h['id']: h['name'] for h in data['habits'] if h.get('active', True)}
        logs = data['logs']
        
        today = datetime.datetime.now(JST)
        dates = [(today - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)]
        dates.reverse()
        
        header_date = " ".join([d[5:].replace("-", "/") for d in dates])
        description = "```\n"
        description += f"{'':<10} {header_date}\n"
        description += "-" * (10 + len(dates)*6) + "\n"
        
        for hid, name in active_habits.items():
            row_name = name[:8]
            checks = []
            for d in dates:
                mark = "■" if hid in logs.get(d, []) else "･"
                checks.append(f"{mark:^5}")
            description += f"{row_name:<10} {''.join(checks)}\n"
        description += "```"
        
        embed = discord.Embed(title="🔥 Habit Streak (Last 7 Days)", description=description, color=discord.Color.orange())
        return embed

    @tasks.loop(time=datetime.time(hour=7, minute=0, tzinfo=JST))
    async def daily_task(self):
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        view = HabitManagerView(self)
        await view.refresh_view(message=None)
        
        data = await self._load_data()
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        daily_log = data['logs'].get(today_str, [])
        active_habits = [h for h in data['habits'] if h.get('active', True)]

        view.clear_items()
        for habit in active_habits:
            is_done = habit['id'] in daily_log
            style = discord.ButtonStyle.success if is_done else discord.ButtonStyle.secondary
            label = f"{'✅' if is_done else '⬜'} {habit['name']}"
            button = discord.ui.Button(style=style, label=label, custom_id=f"habit_{habit['id']}")
            button.callback = view.create_toggle_callback(habit['id'])
            view.add_item(button)
        
        add_btn = discord.ui.Button(label="➕ 追加", style=discord.ButtonStyle.primary, row=4)
        add_btn.callback = view.add_callback
        view.add_item(add_btn)
        view.add_item(HabitDeleteSelect(active_habits))

        embed = discord.Embed(
            title=f"📅 習慣トラッカー ({today_str})",
            description="Good Morning! ☀️\n今日も一日、良い習慣を積み重ねましょう。",
            color=discord.Color.green()
        )
        if active_habits:
            rate = int((len(daily_log) / len(active_habits)) * 100)
            embed.set_footer(text=f"今日の達成率: {rate}%")

        await channel.send(embed=embed, view=view)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="habit", description="習慣トラッカーを手動で表示します")
    async def habit(self, interaction: discord.Interaction):
        if self.channel_id and interaction.channel_id != self.channel_id:
             await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
             return
        
        await interaction.response.defer()
        view = HabitManagerView(self)
        await view.refresh_view(interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(HabitCog(bot))