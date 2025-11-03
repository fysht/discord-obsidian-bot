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
import re # ★ 時刻のバリデーションのために re をインポート
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
# === 株式ウォッチリスト編集用 UI コンポーネント (変更なし) ===
# ==============================================================================

class StockAddModal(discord.ui.Modal, title="銘柄の追加"):
    code_input = discord.ui.TextInput(
        label="銘柄コード", placeholder="例: 7203", required=True, style=discord.TextStyle.short, max_length=10
    )
    name_input = discord.ui.TextInput(
        label="企業名", placeholder="例: トヨタ自動車", required=True, style=discord.TextStyle.short, max_length=100
    )

    def __init__(self, cog: 'NewsCog', parent_view: 'StockEditView'):
        super().__init__(timeout=300)
        self.cog = cog
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        code = self.code_input.value.strip()
        name = self.name_input.value.strip()
        
        async with self.parent_view.lock: 
            watchlist = await self.cog._get_watchlist()
            if code in watchlist:
                await interaction.followup.send(f"⚠️ {name} ({code}) は既にリストに存在します。", ephemeral=True)
            else:
                watchlist[code] = name
                await self.cog._save_watchlist(watchlist)
                await interaction.followup.send(f"✅ {name} ({code}) を監視リストに追加しました。", ephemeral=True)
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

    @discord.ui.button(label="➕ 銘柄を追加", style=discord.ButtonStyle.success, custom_id="stock_edit_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = StockAddModal(self.cog, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="➖ 銘柄を削除", style=discord.ButtonStyle.danger, custom_id="stock_edit_remove")
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
# === NewsCog 本体 (★ スケジュールロジック修正) =================================
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
            
            # ★ 修正: タスクの定義 (ここでは開始しない)
            self.daily_news_briefing.add_exception_type(Exception) # エラー時にタスクが停止しないように

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
        """★ 修正: 起動時にスケジュールを読み込み、タスクを開始する"""
        if not self.is_ready:
            return

        await self.bot.wait_until_ready()
        
        # Dropboxから保存された時刻を読み込む
        schedule_data = await self._load_schedule_from_db()
        
        if schedule_data:
            hour = schedule_data['hour']
            minute = schedule_data['minute']
            saved_time = time(hour=hour, minute=minute, tzinfo=JST)
            
            # タスクの実行時刻を動的に変更
            self.daily_news_briefing.change_interval(time=saved_time)
            
            # タスクを開始する
            if not self.daily_news_briefing.is_running():
                self.daily_news_briefing.start()
            logging.info(f"定時ニュースブリーフィングタスクを開始しました (毎日 {saved_time} JSTに設定)")
        else:
            logging.info("定時ニューススケジュールが設定されていません。タスクは開始しません。")


    def cog_unload(self):
        self.daily_news_briefing.cancel() # Cogアンロード時にタスクを停止

    # --- 天気・ニュース・ピン留め関連 (変更なし) ---
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
                logging.info(f"ピン留めニュースファイル ({PINNED_NEWS_JSON_PATH}) が見つかりません。")
                return []
            logging.error(f"ピン留めニュースの読み込みに失敗: {e}")
            return []
        except (json.JSONDecodeError, Exception) as e:
            logging.error(f"ピン留めニュースの解析に失敗: {e}")
            return []

    # --- ブリーフィング実行ロジック (変更なし) ---
    async def run_daily_briefing(self, channel: discord.TextChannel):
        """ブリーフィング（天気、ピン留めメモ、株価）の実行"""
        if not channel:
            logging.error("run_daily_briefing: 投稿対象のチャンネルが無効です。")
            return

        if self.briefing_lock.locked():
            logging.warning("run_daily_briefing: 既にブリーフィングが実行中のため、今回の実行はスキップします。")
            try:
                await channel.send("⚠️ 現在、別のブリーフィング処理が実行中です。完了するまでお待ちください。", delete_after=30)
            except discord.HTTPException:
                pass
            return

        async with self.briefing_lock:
            logging.info(f"デイリーニュースブリーフィングを開始します (Channel: {channel.name})")
            
            # 1. 天気予報の投稿
            try:
                weather_embed = await self._get_jma_weather_forecast()
                await channel.send(embed=weather_embed)
                logging.info("天気予報を投稿しました。")
            except Exception as e:
                 logging.error(f"天気予報の投稿中にエラー: {e}", exc_info=True)
                 await channel.send(f"⚠️ 天気予報の取得または投稿中にエラーが発生しました。\n`{e}`")
            
            # 2. ピン留めメモの投稿
            try:
                pinned_memos = await self._get_pinned_news_from_db()
                if pinned_memos:
                    logging.info(f"{len(pinned_memos)}件のピン留めメモを投稿します。")
                    await channel.send("--- 📌 今朝のピン留めメモ ---")
                    
                    for memo in pinned_memos:
                        content = memo.get("content", "内容不明")
                        author = memo.get("author", "不明なユーザー")
                        memo_embed = discord.Embed(
                            description=content,
                            color=discord.Color.from_rgb(255, 238, 153)
                        ).set_footer(text=f"メモ作成者: {author}")
                        
                        await channel.send(embed=memo_embed)
                        await asyncio.sleep(1)
                else:
                    logging.info("投稿するピン留めメモはありません。")
                    
            except Exception as e:
                logging.error(f"ピン留めメモの投稿中にエラー: {e}", exc_info=True)
                await channel.send(f"⚠️ ピン留めメモの取得または投稿中にエラーが発生しました。\n`{e}`")

            # 3. 株式ウォッチリストの投稿
            try:
                watchlist = await self._get_watchlist()
                if not watchlist:
                    logging.info("株式ウォッチリストが空のため、ニュースの取得をスキップします。")
                    return 

                logging.info(f"ウォッチリストのGoogleニュースRSSを巡回します: {list(watchlist.values())}")
                one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)

                async with aiohttp.ClientSession() as session:
                    for code, name in watchlist.items():
                        try:
                            query = f'"{name}" AND "{code}" when:1d'
                            encoded_query = quote_plus(query)
                            rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ja&gl=JP&ceid=JP:ja"

                            async with session.get(rss_url) as response:
                                if response.status != 200:
                                    logging.error(f"GoogleニュースRSSの取得に失敗 ({name}): Status {response.status}")
                                    continue
                                feed_text = await response.text()
                                feed = feedparser.parse(feed_text)

                            if not feed.entries:
                                logging.info(f"関連ニュースは見つかりませんでした ({name})")
                                continue

                            for entry in feed.entries:
                                published_time = datetime.strptime(entry.published, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                                if published_time < one_day_ago:
                                    continue

                                logging.info(f"関連ニュースを発見: {entry.title} ({name})")
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

                        except Exception as e:
                            logging.error(f"株式ニュースの処理中にエラーが発生 ({name}): {e}", exc_info=True)
                            await channel.send(f"⚠️ {name}のニュース取得中にエラーが発生しました。")

                        await asyncio.sleep(5) 
            except Exception as e:
                 logging.error(f"株式ニュースの処理中にエラー: {e}", exc_info=True)
                 await channel.send(f"⚠️ 株式ニュースの取得または投稿中にエラーが発生しました。\n`{e}`")
        
        logging.info(f"デイリーニュースブリーフィングが完了しました (Channel: {channel.name})")

    # --- ★ 修正: 定時実行タスク (時刻指定なしで定義) ---
    @tasks.loop() # time 引数を削除
    async def daily_news_briefing(self):
        """
        スケジュールされた時刻にブリーフィングを実行するタスク。
        実際の時刻は on_ready または set_schedule コマンドで設定される。
        """
        logging.info(f"定時ブリーフィングタスクがトリガーされました (実行時刻: {self.daily_news_briefing.time})")
        
        # ★ 追加: 実行時刻が設定されていない場合は何もしない
        # (on_readyで起動しなかった場合、change_intervalされていない可能性への念押し)
        if not self.daily_news_briefing.time:
             logging.warning("daily_news_briefing: タスクが実行されましたが、有効な実行時刻が設定されていません。")
             return
             
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel:
            logging.error(f"定時ブリーフィング失敗: チャンネルID {self.news_channel_id} が見つかりません。")
            return
            
        await self.run_daily_briefing(channel)

    # ★ 削除: before_loop は on_ready のロジックに統合されたため不要

    # --- ★ 修正: スケジュール保存/読み込み/削除用ヘルパー ---
    async def _load_schedule_from_db(self) -> Optional[dict]:
        """Dropboxからスケジュール(HH:MM)を読み込む。失敗または存在しない場合は None を返す"""
        if not self.dbx:
            logging.warning("_load_schedule_from_db: Dropboxクライアントがありません。")
            return None
        try:
            _, res = self.dbx.files_download(self.news_schedule_path)
            data = json.loads(res.content.decode('utf-8'))
            hour = int(data.get('hour')) # キーが存在しない/int変換失敗でValueError
            minute = int(data.get('minute'))
            logging.info(f"Dropboxからスケジュールを読み込みました: {hour:02d}:{minute:02d}")
            return {"hour": hour, "minute": minute}
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.info(f"スケジュールファイル ({self.news_schedule_path}) が見つかりません。")
                return None
            logging.error(f"スケジュールファイルの読み込みに失敗: {e}")
            return None
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, Exception) as e:
            logging.error(f"スケジュールファイルの解析に失敗: {e}。")
            return None

    async def _save_schedule_to_db(self, hour: int, minute: int):
        """Dropboxにスケジュール(HH:MM)を保存する"""
        if not self.dbx:
            logging.error("_save_schedule_to_db: Dropboxクライアントがありません。")
            raise Exception("Dropbox client not initialized")
        try:
            data = {"hour": hour, "minute": minute}
            content = json.dumps(data, indent=2).encode('utf-8')
            self.dbx.files_upload(content, self.news_schedule_path, mode=WriteMode('overwrite'))
            logging.info(f"Dropboxにスケジュールを保存しました: {hour:02d}:{minute:02d}")
        except Exception as e:
            logging.error(f"スケジュールファイルの保存に失敗: {e}")
            raise # エラーを呼び出し元に伝播させる

    async def _delete_schedule_from_db(self):
        """Dropboxからスケジュールファイルを削除する"""
        if not self.dbx:
            logging.error("_delete_schedule_from_db: Dropboxクライアントがありません。")
            raise Exception("Dropbox client not initialized")
        try:
            self.dbx.files_delete_v2(self.news_schedule_path)
            logging.info(f"Dropboxからスケジュールファイル ({self.news_schedule_path}) を削除しました。")
        except ApiError as e:
            if isinstance(e.error, dropbox.exceptions.PathLookupError) and e.error.is_not_found():
                logging.info(f"スケジュールファイル ({self.news_schedule_path}) は既に削除されています。")
                pass # 存在しない場合は成功とみなす
            else:
                logging.error(f"スケジュールファイルの削除に失敗: {e}")
                raise # エラーを呼び出し元に伝播させる
        except Exception as e:
            logging.error(f"スケジュールファイルの削除中に予期せぬエラー: {e}")
            raise # エラーを呼び出し元に伝播させる


    # --- 株式ウォッチリスト管理 (変更なし) ---
    async def _get_watchlist(self) -> dict:
        try:
            _, res = self.dbx.files_download(self.stock_watchlist_path)
            data = json.loads(res.content)
            return data if isinstance(data, dict) else {}
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return {}
            logging.error(f"ウォッチリストの読み込みに失敗: {e}")
            return {}

    async def _save_watchlist(self, watchlist: dict):
        try:
            self.dbx.files_upload(json.dumps(watchlist, ensure_ascii=False, indent=2).encode('utf-8'), self.stock_watchlist_path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"ウォッチリストの保存に失敗: {e}")

    stock_group = app_commands.Group(name="stock", description="株価ニュースの監視リストを管理します。")

    @stock_group.command(name="add", description="[非推奨] 監視リストに銘柄コードと企業名を追加します。")
    @app_commands.describe(code="追加する銘柄コード（例: 7203）", name="企業名（例: トヨタ自動車）")
    async def stock_add(self, interaction: discord.Interaction, code: str, name: str):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if code not in watchlist:
            watchlist[code] = name
            await self._save_watchlist(watchlist)
            await interaction.followup.send(f"✅ {name} ({code}) を監視リストに追加しました。\n（推奨: `/stock edit` コマンドの使用をおすすめします）")
        else:
            await interaction.followup.send(f"⚠️ {code} は既にリストに存在します。")

    @stock_group.command(name="remove", description="[非推奨] 監視リストから銘柄コードを削除します。")
    @app_commands.describe(code="削除する銘柄コード")
    async def stock_remove(self, interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if code in watchlist:
            name = watchlist.pop(code)
            await self._save_watchlist(watchlist)
            await interaction.followup.send(f"🗑️ {name} ({code}) を監視リストから削除しました。\n（推奨: `/stock edit` コマンドの使用をおすすめします）")
        else:
            await interaction.followup.send(f"⚠️ {code} はリストに存在しません。")

    @stock_group.command(name="list", description="現在の監視リストを表示します。")
    async def stock_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if watchlist:
            list_str = "\n".join([f"• **{name}** (`{code}`)" for code, name in watchlist.items()])
            embed = discord.Embed(title="📈 株式ニュース 監視リスト", description=list_str, color=discord.Color.blue())
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send("監視リストは現在空です。", ephemeral=True)

    @stock_group.command(name="edit", description="監視リストを対話形式で編集します。")
    async def stock_edit(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=False, thinking=True) 
        
        view = StockEditView(self, interaction)
        await interaction.followup.send("監視リストをロード中...", embed=None, view=view)
        await view.update_message()

    @stock_group.command(name="run_briefing", description="毎朝のニュースブリーフィングを手動で実行します。")
    async def stock_run_briefing(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドはニュースチャンネル (<#{self.news_channel_id}>) で実行してください。", ephemeral=True)
            return

        await interaction.response.send_message("✅ 手動ブリーフィングを開始します...", ephemeral=True)
        await self.run_daily_briefing(interaction.channel)

    # --- ★ 修正: スケジュール設定コマンド ---
    @stock_group.command(name="set_schedule", description="ニュースブリーフィングの定時実行時刻 (JST) を設定します。")
    @app_commands.describe(schedule_time="実行時刻 (HH:MM形式, 24時間表記, JST)。例: 06:30")
    async def stock_set_schedule(self, interaction: discord.Interaction, schedule_time: str):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # 1. 時刻のバリデーション (HH:MM または H:MM)
        match = re.match(r'^([0-2]?[0-9]):([0-5]?[0-9])$', schedule_time.strip())
        if not match:
            await interaction.followup.send(
                f"❌ 時刻の形式が正しくありません。\n"
                f"必ず `HH:MM` (例: `06:30` や `21:00`) の形式で入力してください。", 
                ephemeral=True
            )
            return

        try:
            hour = int(match.group(1))
            minute = int(match.group(2))
            
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                 raise ValueError("時刻の範囲が不正です")

            # 2. Dropboxに保存
            await self._save_schedule_to_db(hour, minute)

            # 3. 実行中のタスクの時刻を動的に変更
            new_time_obj = time(hour=hour, minute=minute, tzinfo=JST)
            self.daily_news_briefing.change_interval(time=new_time_obj)
            
            # 4. タスクが停止していれば、開始する
            if not self.daily_news_briefing.is_running():
                self.daily_news_briefing.start()
                logging.info(f"ニュース実行時刻がコマンドにより {new_time_obj} JST に設定され、タスクが開始されました。")
                await interaction.followup.send(
                    f"✅ ニュースの定時実行時刻を毎日 **{hour:02d}:{minute:02d} (JST)** に設定し、タスクを開始しました。\n"
                    f"（この設定はボットが再起動しても保持されます）",
                    ephemeral=True
                )
            else:
                logging.info(f"ニュース実行時刻がコマンドにより {new_time_obj} JST に変更されました。")
                await interaction.followup.send(
                    f"✅ ニュースの定時実行時刻を毎日 **{hour:02d}:{minute:02d} (JST)** に変更しました。\n"
                    f"（この設定はボットが再起動しても保持されます）",
                    ephemeral=True
                )

        except ValueError:
             await interaction.followup.send(
                f"❌ 時刻の値が不正です (例: `25:00`)。\n"
                f"`00:00` から `23:59` の間で入力してください。", 
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"スケジュール設定中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ スケジュールの設定中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    # ★ 新規追加: /stock cancel_schedule コマンド
    @stock_group.command(name="cancel_schedule", description="ニュースブリーフィングの定時実行を停止し、スケジュールを削除します。")
    async def stock_cancel_schedule(self, interaction: discord.Interaction):
        if interaction.channel_id != self.news_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.news_channel_id}> で実行してください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # 1. タスクを停止
            if self.daily_news_briefing.is_running():
                self.daily_news_briefing.stop()
                logging.info("ニュースブリーフィングタスクをコマンドにより停止しました。")
            
            # 2. Dropboxからスケジュールファイルを削除
            await self._delete_schedule_from_db()
            
            await interaction.followup.send(
                f"✅ ニュースの定時実行を停止し、スケジュールを削除しました。\n"
                f"（ボットが再起動しても自動実行されません）",
                ephemeral=True
            )

        except Exception as e:
            logging.error(f"スケジュール削除中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ スケジュールの削除中に予期せぬエラーが発生しました: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))