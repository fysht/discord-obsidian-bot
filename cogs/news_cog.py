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

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
NEWS_BRIEFING_TIME = time(hour=6, minute=00, tzinfo=JST)
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {
    "晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"
}

class NewsCog(commands.Cog):
    """天気予報と株式関連ニュースを定時通知するCog"""

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

            self.is_ready = True
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
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") # Keep for validation check maybe? Or remove if truly unused. Let's keep for now.
        self.watchlist_path = f"{self.dropbox_vault_path}/.bot/stock_watchlist.json"

    def _are_credentials_valid(self) -> bool:
        return all([
            self.news_channel_id,
            self.dropbox_app_key,
            self.dropbox_app_secret,
            self.dropbox_refresh_token,
        ])

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready and not self.daily_news_briefing.is_running():
            self.daily_news_briefing.start()

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
        """GoogleニュースのリダイレクトURLから実際の記事URLを取り出す"""
        try:
            # HEADリクエストでリダイレクト先を追跡
            response = requests.head(google_news_url, allow_redirects=True, timeout=10)
            return response.url
        except requests.RequestException as e:
            logging.warning(f"リダイレクト先の解決に失敗しました: {e}")
            # マッチングによるフォールバック
            match = re.search(r"url=([^&]+)", google_news_url)
            if match:
                return requests.utils.unquote(match.group(1))
        return google_news_url

    @tasks.loop(time=NEWS_BRIEFING_TIME)
    async def daily_news_briefing(self):
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel: return

        logging.info("デイリーニュースブリーフィングを開始します...")
        weather_embed = await self._get_jma_weather_forecast()
        await channel.send(embed=weather_embed)
        logging.info("天気予報を投稿しました。")

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

                        # --- Create Embed without description (summary) ---
                        try:
                            # Attempt to resolve the actual URL
                            actual_url = await asyncio.to_thread(self._resolve_actual_url, entry.links[0].href)
                        except Exception:
                             actual_url = entry.links[0].href # Fallback to original if resolution fails

                        news_embed = discord.Embed(
                            title=f"📈関連ニュース: {entry.title}",
                            url=actual_url, # Use resolved or original URL
                            color=discord.Color.green()
                        ).set_footer(text=f"銘柄: {name} ({code}) | {entry.source.title}")
                        await channel.send(embed=news_embed)
                        await asyncio.sleep(3) # Keep sleep to avoid rate limits

                except Exception as e:
                    logging.error(f"株式ニュースの処理中にエラーが発生 ({name}): {e}", exc_info=True)
                    await channel.send(f"⚠️ {name}のニュース取得中にエラーが発生しました。")

                await asyncio.sleep(5) # Keep sleep between stocks

    # --- 株式ウォッチリスト管理機能 ---
    async def _get_watchlist(self) -> dict:
        try:
            _, res = self.dbx.files_download(self.watchlist_path)
            data = json.loads(res.content)
            return data if isinstance(data, dict) else {}
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return {}
            logging.error(f"ウォッチリストの読み込みに失敗: {e}")
            return {}

    async def _save_watchlist(self, watchlist: dict):
        try:
            self.dbx.files_upload(json.dumps(watchlist, ensure_ascii=False, indent=2).encode('utf-8'), self.watchlist_path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"ウォッチリストの保存に失敗: {e}")

    stock_group = app_commands.Group(name="stock", description="株価ニュースの監視リストを管理します。")

    @stock_group.command(name="add", description="監視リストに銘柄コードと企業名を追加します。")
    @app_commands.describe(code="追加する銘柄コード（例: 7203）", name="企業名（例: トヨタ自動車）")
    async def stock_add(self, interaction: discord.Interaction, code: str, name: str):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if code not in watchlist:
            watchlist[code] = name
            await self._save_watchlist(watchlist)
            await interaction.followup.send(f"✅ {name} ({code}) を監視リストに追加しました。")
        else:
            await interaction.followup.send(f"⚠️ {code} は既にリストに存在します。")

    @stock_group.command(name="remove", description="監視リストから銘柄コードを削除します。")
    @app_commands.describe(code="削除する銘柄コード")
    async def stock_remove(self, interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if code in watchlist:
            name = watchlist.pop(code)
            await self._save_watchlist(watchlist)
            await interaction.followup.send(f"🗑️ {name} ({code}) を監視リストから削除しました。")
        else:
            await interaction.followup.send(f"⚠️ {code} はリストに存在しません。")

    @stock_group.command(name="list", description="現在の監視リストを表示します。")
    async def stock_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if watchlist:
            list_str = "\n".join([f"- {name} ({code})" for code, name in watchlist.items()])
            await interaction.followup.send(f"現在の監視リスト:\n{list_str}")
        else:
            await interaction.followup.send("監視リストは現在空です。")

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))