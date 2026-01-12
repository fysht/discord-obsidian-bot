import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import json
from datetime import datetime, time, timezone, timedelta
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import asyncio
import aiohttp
import feedparser
from urllib.parse import quote_plus
import re
import requests
from typing import Optional

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# JMA (天気) 関連
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {
    "晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"
}
# Dropbox上の設定ファイルパス
BASE_PATH = os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')
PINNED_NEWS_JSON_PATH = f"{BASE_PATH}/.bot/pinned_news_memos.json"
STOCK_WATCHLIST_PATH = f"{BASE_PATH}/.bot/stock_watchlist.json"
NEWS_SCHEDULE_PATH = f"{BASE_PATH}/.bot/news_schedule.json"


# ==============================================================================
# === 株式ウォッチリスト編集用 UI コンポーネント ===
# ==============================================================================

class StockAddModal(discord.ui.Modal, title="銘柄の追加"):
    entries_input = discord.ui.TextInput(
        label="追加する銘柄 (複数可)",
        placeholder="例:\n7203,トヨタ自動車\n9984,ソフトバンクグループ\n4755,楽天グループ",
        style=discord.TextStyle.paragraph,
        required=True
    )

    def __init__(self, cog: 'NewsCog', parent_view: 'StockEditView'):
        super().__init__(timeout=300)
        self.cog = cog
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        lines = self.entries_input.value.splitlines()
        added_stocks = []
        skipped_stocks = []
        
        async with self.parent_view.lock: 
            watchlist = await self.cog._get_watchlist()
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                parts = re.split(r'[,\s:;]+', line, 1)
                
                if len(parts) == 2:
                    code = parts[0].strip()
                    name = parts[1].strip()
                    if code and name:
                        if code not in watchlist:
                            watchlist[code] = name
                            added_stocks.append(f"{name} ({code})")
                        else:
                            skipped_stocks.append(f"{name} ({code}) (既に存在)")
                    else:
                        skipped_stocks.append(f"{line} (形式不正: コードまたは名前が空)")
                else:
                    skipped_stocks.append(f"{line} (形式不正: 2要素に分割不可)")
            
            await self.cog._save_watchlist(watchlist)

        message_parts = []
        if added_stocks:
            message_parts.append(f"✅ 以下の銘柄を追加しました:\n- " + "\n- ".join(added_stocks))
        if skipped_stocks:
            message_parts.append(f"⚠️ 以下の入力はスキップされました:\n- " + "\n- ".join(skipped_stocks))
        if not message_parts:
            message_parts.append("有効な入力がありませんでした。")

        await interaction.followup.send("\n\n".join(message_parts), ephemeral=True)
        await self.parent_view.update_message(interaction)


    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.error(f"StockAddModalでエラー: {error}", exc_info=True)
        await interaction.followup.send("銘柄の追加中にエラーが発生しました。", ephemeral=True)

class StockRemoveSelectView(discord.ui.View):
    def __init__(self, cog: 'NewsCog', parent_view: 'StockEditView', current_watchlist: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.parent_view = parent_view

        options = [
            discord.SelectOption(label=f"{name} ({code})", value=code)
            for code, name in current_watchlist.items()
        ]
        
        if not options:
             self.add_item(discord.ui.Select(
                 placeholder="削除する銘柄がありません",
                 disabled=True,
                 options=[discord.SelectOption(label="dummy", value="dummy")]
             ))
             return

        self.select_menu = discord.ui.Select(
            placeholder="削除する銘柄を選択 (複数可)...",
            options=options[:25],
            min_values=1,
            max_values=min(len(options), 25)
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        codes_to_remove = interaction.data.get("values", [])
        removed_names = []
        
        async with self.parent_view.lock: 
            watchlist = await self.cog._get_watchlist()
            for code in codes_to_remove:
                if code in watchlist:
                    name = watchlist.pop(code)
                    removed_names.append(name)
            
            await self.cog._save_watchlist(watchlist)

        if removed_names:
            await interaction.followup.send(f"🗑️ 以下の銘柄を削除しました:\n- {', '.join(removed_names)}", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ 削除対象の銘柄が見つかりませんでした。", ephemeral=True)

        await self.parent_view.update_message(interaction)
        
        self.stop()
        try:
            await interaction.edit_original_response(content="削除が完了しました。", view=None)
        except discord.HTTPException:
            pass 

class StockEditView(discord.ui.View):
    def __init__(self, cog: 'NewsCog', interaction: discord.Interaction):
        super().__init__(timeout=600) 
        self.cog = cog
        self.interaction = interaction 
        self.lock = asyncio.Lock() 

    async def update_message(self, interaction: Optional[discord.Interaction] = None):
        async with self.lock:
            watchlist = await self.cog._get_watchlist()
        
        embed = discord.Embed(title="📈 株式ニュース 監視リスト編集", color=discord.Color.blue())
        
        if not watchlist:
            embed.description = "現在、監視リストは空です。"
        else:
            list_str = "\n".join([f"• **{name}** (`{code}`)" for code, name in watchlist.items()])
            embed.description = f"**現在のリスト:**\n{list_str}"
        
        embed.set_footer(text="下のボタンでリストを編集してください。")
        
        try:
            await self.interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException as e:
            logging.warning(f"StockEditView message update failed: {e}")
            self.stop()

    @discord.ui.button(label="➕ 銘柄を追加 (複数可)", style=discord.ButtonStyle.success, custom_id="stock_edit_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = StockAddModal(self.cog, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="➖ 銘柄を削除 (複数可)", style=discord.ButtonStyle.danger, custom_id="stock_edit_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        async with self.lock:
            watchlist = await self.cog._get_watchlist()
            
        if not watchlist:
            await interaction.followup.send("⚠️ 削除できる銘柄がありません。", ephemeral=True)
            return

        remove_view = StockRemoveSelectView(self.cog, self, watchlist)
        await interaction.followup.send("削除する銘柄を選択してください:", view=remove_view, ephemeral=True)

    @discord.ui.button(label="完了", style=discord.ButtonStyle.secondary, custom_id="stock_edit_done")
    async def done_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="監視リストの編集を完了しました。", embed=None, view=None)
        self.stop()

    async def on_timeout(self):
        try:
            await self.interaction.edit_original_response(content="監視リストの編集がタイムアウトしました。", embed=None, view=None)
        except discord.HTTPException:
            pass

# ==============================================================================
# === NewsCog 本体 =============================================================
# ==============================================================================

class NewsCog(commands.Cog):
    """天気予報、ピン留めメモ、株式関連ニュースを定時通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables() 

        if not self._are_credentials_valid():
            logging.error("NewsCog: 必須の環境変数が不足。Cogを無効化します。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            )
            self.stock_watchlist_path = STOCK_WATCHLIST_PATH
            self.news_schedule_path = NEWS_SCHEDULE_PATH
            
            self.briefing_lock = asyncio.Lock()
            self.is_ready = True
            
            self.daily_news_briefing.add_exception_type(Exception)

            logging.info("✅ NewsCogが正常に初期化されました。")

        except Exception as e:
            logging.error(f"❌ NewsCogの初期化中にエラー: {e}", exc_info=True)

    def _load_environment_variables(self):
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        self.jma_area_name = os.getenv("JMA_AREA_NAME", "南部")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") 

    def _are_credentials_valid(self) -> bool:
        return all([
            self.news_channel_id,
            self.dropbox_app_key,
            self.dropbox_app_secret,
            self.dropbox_refresh_token,
        ])

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready:
            return

        await self.bot.wait_until_ready()
        
        schedule_data = await self._load_schedule_from_db()
        
        if schedule_data:
            hour = schedule_data['hour']
            minute = schedule_data['minute']
            saved_time = time(hour=hour, minute=minute, tzinfo=JST)
            
            self.daily_news_briefing.change_interval(time=saved_time)
            
            if not self.daily_news_briefing.is_running():
                self.daily_news_briefing.start()
            logging.info(f"定時ニュースブリーフィングタスクを開始しました (毎日 {saved_time} JSTに設定)")
        else:
            logging.info("定時ニューススケジュールが設定されていません。タスクは開始しません。")


    def cog_unload(self):
        self.daily_news_briefing.cancel()

    def _get_emoji_for_weather(self, weather_text: str) -> str:
        for key, emoji in WEATHER_EMOJI_MAP.items():
            if key in weather_text:
                return emoji
        return "❓"

    async def _get_jma_weather_forecast(self) -> discord.Embed:
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"
        embed = discord.Embed(
            title=f"🗓️ {datetime.now(JST).strftime('%Y年%m月%d日')} のお知らせ",
            color=discord.Color.blue()
        )
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    data = await response.json()

                area_weather_today = next((area for area in data[0]["timeSeries"][0]["areas"] if area["area"]["name"] == self.jma_area_name), None)
                area_temp_today = next((area for area in data[0]["timeSeries"][2]["areas"] if area["area"]["name"] == self.location_name), None)

                if area_weather_today and area_temp_today:
                    weather_summary = area_weather_today["weathers"][0]
                    weather_emoji = self._get_emoji_for_weather(weather_summary)
                    max_temp = area_temp_today.get("temps", ["--"])[1]
                    min_temp = area_temp_today.get("temps", ["--"])[0]
                    embed.add_field(name=f"今日の天気 ({self.location_name})", value=f"{weather_emoji} {weather_summary}\n🌡️ 最高: {max_temp}℃ / 最低: {min_temp}℃", inline=False)
                else:
                    embed.add_field(name=f"今日の天気 ({self.location_name})", value="⚠️ エリア情報を取得できませんでした。", inline=False)

                time_defines_pop = data[0]["timeSeries"][1]["timeDefines"]
                area_pops = next((area["pops"] for area in data[0]["timeSeries"][1]["areas"] if area["area"]["name"] == self.jma_area_name), None)
                time_defines_temp = data[0]["timeSeries"][2]["timeDefines"]
                area_temps = next((area["temps"] for area in data[0]["timeSeries"][2]["areas"] if area["area"]["name"] == self.location_name), None)

                if area_pops and area_temps:
                    pop_text, temp_text = "", ""
                    for i, time_str in enumerate(time_defines_pop):
                        dt = datetime.fromisoformat(time_str)
                        if dt.date() == datetime.now(JST).date(): pop_text += f"**{dt.strftime('%H時')}**: {area_pops[i]}% "
                    for i, time_str in enumerate(time_defines_temp):
                         dt = datetime.fromisoformat(time_str)
                         if dt.date() == datetime.now(JST).date(): temp_text += f"**{dt.strftime('%H時')}**: {area_temps[i]}℃ "
                    if pop_text: embed.add_field(name="☂️ 降水確率", value=pop_text.strip(), inline=False)
                    if temp_text: embed.add_field(name="🕒 時間別気温", value=temp_text.strip(), inline=False)
            except Exception as e:
                logging.error(f"天気予報取得に失敗: {e}", exc_info=True)
                embed.add_field(name="エラー", value="⚠️ 天気情報の取得に失敗しました。", inline=False)
        return embed

    def _resolve_actual_url(self, google_news_url: str) -> str:
        try:
            response = requests.head(google_news_url, allow_redirects=True, timeout=10)
            return response.url
        except requests.RequestException as e:
            logging.warning(f"リダイレクト先の解決に失敗しました: {e}")
            match = re.search(r"url=([^&]+)", google_news_url)
            if match:
                return requests.utils.unquote(match.group(1))
        return google_news_url

    async def _get_pinned_news_from_db(self) -> list:
        if not self.dbx: return []
        try:
            _, res = self.dbx.files_download(PINNED_NEWS_JSON_PATH)
            data = json.loads(res.content.decode('utf-8'))
            return data if isinstance(data, list) else []
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return []
            logging.error(f"ピン留めニュースの読み込みに失敗: {e}")
            return []
        except (json.JSONDecodeError, Exception) as e:
            logging.error(f"ピン留めニュースの解析に失敗: {e}")
            return []

    async def run_daily_briefing(self, channel: discord.TextChannel):
        """ブリーフィング（天気、ピン留めメモ、株価、Todo、習慣）の実行"""
        if not channel: return
        if self.briefing_lock.locked(): return

        async with self.briefing_lock:
            logging.info(f"デイリーニュースブリーフィングを開始します (Channel: {channel.name})")
            
            # 1. 天気予報
            try:
                weather_embed = await self._get_jma_weather_forecast()
                await channel.send(embed=weather_embed)
            except Exception as e:
                 logging.error(f"天気予報の投稿中にエラー: {e}", exc_info=True)
                 await channel.send(f"⚠️ 天気予報エラー: `{e}`")
            
            # 2. ピン留めメモ
            try:
                pinned_memos = await self._get_pinned_news_from_db()
                if pinned_memos:
                    await channel.send("--- 📌 Today's Pinned Memos ---")
                    for memo in pinned_memos:
                        content = memo.get("content", "内容不明")
                        author = memo.get("author", "不明なユーザー")
                        memo_embed = discord.Embed(
                            description=content,
                            color=discord.Color.from_rgb(255, 238, 153)
                        ).set_footer(text=f"Memo creator: {author}")
                        await channel.send(embed=memo_embed)
                        await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"ピン留めメモの投稿中にエラー: {e}", exc_info=True)
            
            # 3. Todoリスト
            try:
                todo_cog = self.bot.get_cog("TodoCog")
                if todo_cog:
                    await channel.send("--- 📝 Today's Todo List ---")
                    todo_embed = await todo_cog.get_todos_formatted()
                    await channel.send(embed=todo_embed)
            except Exception as e:
                logging.error(f"Todoリストの投稿中にエラー: {e}", exc_info=True)

            # 4. ★ 新規追加: 習慣トラッカー（週間レポート）
            try:
                habit_cog = self.bot.get_cog("HabitCog")
                if habit_cog:
                    # 見出しなしでEmbedを直接投稿（Embed内にタイトルがあるため）
                    habit_embed = await habit_cog.get_weekly_stats_embed()
                    await channel.send(embed=habit_embed)
                else:
                    logging.warning("HabitCogが見つからないため、習慣レポートをスキップします。")
            except Exception as e:
                logging.error(f"習慣レポートの投稿中にエラー: {e}", exc_info=True)

            # 5. 株式ウォッチリスト
            try:
                watchlist = await self._get_watchlist()
                if watchlist:
                    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
                    async with aiohttp.ClientSession() as session:
                        for code, name in watchlist.items():
                            try:
                                query = f'"{name}" AND "{code}" when:1d'
                                encoded_query = quote_plus(query)
                                rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ja&gl=JP&ceid=JP:ja"

                                async with session.get(rss_url) as response:
                                    if response.status != 200: continue
                                    feed_text = await response.text()
                                    feed = feedparser.parse(feed_text)

                                if not feed.entries: continue

                                for entry in feed.entries:
                                    published_time = datetime.strptime(entry.published, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                                    if published_time < one_day_ago: continue

                                    try:
                                        actual_url = await asyncio.to_thread(self._resolve_actual_url, entry.links[0].href)
                                    except Exception:
                                         actual_url = entry.links[0].href 

                                    news_embed = discord.Embed(
                                        title=f"📈関連ニュース: {entry.title}",
                                        url=actual_url,
                                        color=discord.Color.green()
                                    ).set_footer(text=f"銘柄: {name} ({code}) | {entry.source.title}")
                                    await channel.send(embed=news_embed)
                                    await asyncio.sleep(3) 
                            except Exception:
                                pass
                            await asyncio.sleep(5) 
            except Exception as e:
                 logging.error(f"株式ニュースの処理中にエラー: {e}", exc_info=True)
        
        logging.info(f"デイリーニュースブリーフィングが完了しました")

    @tasks.loop()
    async def daily_news_briefing(self):
        """定時ブリーフィングタスク"""
        if not self.daily_news_briefing.time: return
             
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel: return
            
        await self.run_daily_briefing(channel)

    async def _load_schedule_from_db(self) -> Optional[dict]:
        if not self.dbx: return None
        try:
            _, res = self.dbx.files_download(self.news_schedule_path)
            data = json.loads(res.content.decode('utf-8'))
            return {"hour": int(data.get('hour')), "minute": int(data.get('minute'))}
        except Exception:
            return None

    async def _save_schedule_to_db(self, hour: int, minute: int):
        if not self.dbx: raise Exception("Dropbox client not initialized")
        data = {"hour": hour, "minute": minute}
        content = json.dumps(data, indent=2).encode('utf-8')
        self.dbx.files_upload(content, self.news_schedule_path, mode=WriteMode('overwrite'))

    async def _delete_schedule_from_db(self):
        if not self.dbx: raise Exception("Dropbox client not initialized")
        try:
            self.dbx.files_delete_v2(self.news_schedule_path)
        except ApiError as e:
            if not (isinstance(e.error, dropbox.exceptions.PathLookupError) and e.error.is_not_found()):
                raise

    async def _get_watchlist(self) -> dict:
        try:
            _, res = self.dbx.files_download(self.stock_watchlist_path)
            data = json.loads(res.content)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _save_watchlist(self, watchlist: dict):
        try:
            self.dbx.files_upload(json.dumps(watchlist, ensure_ascii=False, indent=2).encode('utf-8'), self.stock_watchlist_path, mode=WriteMode('overwrite'))
        except Exception:
            pass

    briefing_group = app_commands.Group(name="briefing", description="ニュースブリーフィングの実行やスケジュールを管理します。")
    stock_group = app_commands.Group(name="stock", description="株価ニュースの監視リストを管理します。")

    @stock_group.command(name="edit", description="監視リストを対話形式で編集します。")
    async def stock_edit(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False, thinking=True) 
        view = StockEditView(self, interaction)
        await interaction.followup.send("監視リストをロード中...", embed=None, view=view)
        await view.update_message()

    @briefing_group.command(name="run_now", description="毎朝のニュースブリーフィングを手動で実行します。")
    async def news_run_now(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドはニュースチャンネル (<#{self.news_channel_id}>) で実行してください。", ephemeral=True)
            return
        await interaction.response.send_message("✅ 手動ブリーフィングを開始します...", ephemeral=True)
        await self.run_daily_briefing(interaction.channel)

    @briefing_group.command(name="set_schedule", description="ニュースブリーフィングの定時実行時刻 (JST) を設定します。")
    @app_commands.describe(schedule_time="実行時刻 (HH:MM形式, 24時間表記, JST)。例: 06:30")
    async def news_set_schedule(self, interaction: discord.Interaction, schedule_time: str):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        match = re.match(r'^([0-2]?[0-9]):([0-5]?[0-9])$', schedule_time.strip())
        if not match:
            await interaction.followup.send(f"❌ 時刻の形式が正しくありません。\n必ず `HH:MM` (例: `06:30`) の形式で入力してください。", ephemeral=True)
            return
        try:
            hour = int(match.group(1))
            minute = int(match.group(2))
            if not (0 <= hour <= 23 and 0 <= minute <= 59): raise ValueError("時刻の範囲が不正です")
            
            await self._save_schedule_to_db(hour, minute)
            new_time_obj = time(hour=hour, minute=minute, tzinfo=JST)
            self.daily_news_briefing.change_interval(time=new_time_obj)
            
            if not self.daily_news_briefing.is_running():
                self.daily_news_briefing.start()
            
            await interaction.followup.send(f"✅ 定時実行時刻を **{hour:02d}:{minute:02d} (JST)** に設定しました。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

    @briefing_group.command(name="cancel_schedule", description="ニュースブリーフィングの定時実行を停止し、スケジュールを削除します。")
    async def news_cancel_schedule(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if self.daily_news_briefing.is_running(): self.daily_news_briefing.stop()
            await self._delete_schedule_from_db()
            await interaction.followup.send(f"✅ 定時実行を停止し、スケジュールを削除しました。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))