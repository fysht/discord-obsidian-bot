import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import asyncio
import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError

# 共通ユーティリティのインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数設定 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HABIT_DATA_PATH = "/.bot/habit_data.json"
DAILY_NOTE_SECTION = "## Habits"

# 自動投稿する時間 (JST) - ライフログチャンネルへのボタン表示用
SCHEDULED_TIME = datetime.time(hour=7, minute=0, tzinfo=JST)

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
        new_id = str(max([int(h['id']) for h in data['habits']] + [0]) + 1)
        
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
        view: HabitManagerView = self.view
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
        """表示を最新状態に更新"""
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
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("LIFELOG_CHANNEL_ID", 0))
        
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.dbx = None
        if self.dropbox_refresh_token:
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
            except Exception as e:
                print(f"HabitCog Init Error: {e}")

        self.daily_task.start()

    def cog_unload(self):
        self.daily_task.cancel()

    async def _load_data(self):
        default_data = {"habits": [], "logs": {}}
        if not self.dbx: return default_data
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, HABIT_DATA_PATH)
            return json.loads(res.content.decode('utf-8'))
        except ApiError:
            return default_data
        except Exception:
            return default_data

    async def _save_data(self, data):
        if not self.dbx: return False
        try:
            json_str = json.dumps(data, ensure_ascii=False, indent=2)
            await asyncio.to_thread(
                self.dbx.files_upload,
                json_str.encode('utf-8'),
                HABIT_DATA_PATH,
                mode=WriteMode('overwrite')
            )
            return True
        except Exception as e:
            print(f"Save Data Error: {e}")
            return False

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
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                content = res.content.decode('utf-8')
            except ApiError:
                content = f"# Daily Note {date_str}\n"

            checklist_lines = []
            active_habits = [h for h in data['habits'] if h.get('active', True)]
            daily_log = data['logs'].get(date_str, [])
            
            for habit in active_habits:
                check_mark = "x" if habit['id'] in daily_log else " "
                checklist_lines.append(f"- [{check_mark}] {habit['name']}")
            
            new_section_text = "\n".join(checklist_lines)

            lines = content.split('\n')
            new_lines = []
            in_habit_section = False
            for line in lines:
                if line.strip().startswith(DAILY_NOTE_SECTION):
                    in_habit_section = True
                    continue
                if in_habit_section and line.strip().startswith("##"):
                    in_habit_section = False
                
                if not in_habit_section:
                    new_lines.append(line)
            
            clean_content = "\n".join(new_lines).strip()
            final_content = update_section(clean_content, new_section_text, DAILY_NOTE_SECTION)
            
            await asyncio.to_thread(
                self.dbx.files_upload,
                final_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            print(f"Obsidian Sync Error: {e}")

    # --- 外部呼び出し用メソッド (NewsCog連携) ---
    async def get_weekly_stats_embed(self) -> discord.Embed:
        """直近1週間の習慣達成状況のEmbedを作成して返す"""
        data = await self._load_data()
        
        active_habits = {h['id']: h['name'] for h in data['habits'] if h.get('active', True)}
        logs = data['logs']
        
        today = datetime.datetime.now(JST)
        dates = [(today - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)]
        dates.reverse() # 古い順
        
        # ヘッダー (日付)
        header_date = " ".join([d[5:].replace("-", "/") for d in dates]) # 01/01 形式
        
        description = "```\n"
        description += f"{'':<10} {header_date}\n"
        description += "-" * (10 + len(dates)*6) + "\n"
        
        for hid, name in active_habits.items():
            row_name = name[:8] # 長すぎる名前はカット
            checks = []
            for d in dates:
                # 達成なら■、未達成なら･
                mark = "■" if hid in logs.get(d, []) else "･"
                checks.append(f"{mark:^5}")
            
            description += f"{row_name:<10} {''.join(checks)}\n"
            
        description += "```"
        
        embed = discord.Embed(
            title="🔥 Habit Streak (Last 7 Days)",
            description=description,
            color=discord.Color.orange()
        )
        return embed

    # --- 定期実行タスク (ライフログチャンネルへボタン送信) ---
    @tasks.loop(time=SCHEDULED_TIME)
    async def daily_task(self):
        """毎朝自動でハビットトラッカーを送信 (LifeLogチャンネル)"""
        if not self.channel_id: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        view = HabitManagerView(self)
        await view.refresh_view(message=None)

        # 送信処理
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

    # --- 手動コマンド ---
    @app_commands.command(name="habit", description="習慣トラッカーを手動で表示します")
    async def habit(self, interaction: discord.Interaction):
        if self.channel_id and interaction.channel_id != self.channel_id:
             await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
             return
        
        await interaction.response.defer()
        
        # 処理はdaily_taskと同様
        view = HabitManagerView(self)
        data = await self._load_data()
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        daily_log = data['logs'].get(today_str, [])
        active_habits = [h for h in data['habits'] if h.get('active', True)]

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
            description="手動呼び出しモード",
            color=discord.Color.green()
        )
        if active_habits:
            rate = int((len(daily_log) / len(active_habits)) * 100)
            embed.set_footer(text=f"今日の達成率: {rate}%")
        
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="habit_list", description="習慣の履歴一覧を表示します")
    async def habit_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.get_weekly_stats_embed()
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(HabitCog(bot))