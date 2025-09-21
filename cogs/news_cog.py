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
import google.generativeai as genai
import feedparser
from bs4 import BeautifulSoup
import re

# 他のファイルから関数をインポート
from google_search import search as google_search_function

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
NEWS_BRIEFING_TIME = time(hour=19, minute=52, tzinfo=JST)

# ニュースソース
MACRO_NEWS_RSS_URLS = ["https://www.nhk.or.jp/rss/news/cat2.xml"]
YAHOO_FINANCE_RSS_URL = "https://finance.yahoo.co.jp/rss/company"

# 気象庁のエリアコード
JMA_AREA_CODE = "330000" # 岡山県

# 天気の絵文字マッピング
WEATHER_EMOJI_MAP = {"晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"}

class NewsCog(commands.Cog):
    """天気予報と株式関連ニュースを定時通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.session = aiohttp.ClientSession()

        if not self._are_credentials_valid():
            logging.error("NewsCog: 必須の環境変数が不足。Cogを無効化します。")
            return

        try:
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            if self.gemini_api_key:
                genai.configure(api_key=self.gemini_api_key)
                self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            else:
                self.gemini_model = None
            self.is_ready = True
            logging.info("✅ NewsCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ NewsCogの初期化中にエラー: {e}", exc_info=True)

    async def cog_unload(self):
        await self.session.close()

    def _load_environment_variables(self):
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.location_name = os.getenv("LOCATION_NAME", "現在地")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.watchlist_path = f"{self.dropbox_vault_path}/.bot/stock_watchlist.json"

    def _are_credentials_valid(self) -> bool:
        return all([self.news_channel_id, self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token, self.gemini_api_key])

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready and not self.daily_news_briefing.is_running():
            self.daily_news_briefing.start()

    def cog_unload(self):
        self.daily_news_briefing.cancel()

    def _get_emoji_for_weather(self, weather_text: str) -> str:
        for key, emoji in WEATHER_EMOJI_MAP.items():
            if key in weather_text: return emoji
        return "❓"

    async def _get_jma_weather_forecast(self, area_code: str, location_name: str) -> str:
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json"
        try:
            async with self.session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
            
            report_dt = datetime.fromisoformat(data[0]["reportDatetime"]).astimezone(JST)

            weather_ts = next((ts for ts in data[0]["timeSeries"] if "weathers" in ts["areas"][0]), None)
            temp_ts = next((ts for ts in data[0]["timeSeries"] if "temps" in ts["areas"][0] and len(ts["timeDefines"]) > 2), None)
            precip_ts = next((ts for ts in data[0]["timeSeries"] if "pops" in ts["areas"][0]), None)
            
            if not weather_ts or not temp_ts:
                raise ValueError("必要な天気または気温データが見つかりませんでした。")
            
            today_weather_summary = weather_ts["areas"][0]["weathers"][0]
            weather_emoji = self._get_emoji_for_weather(today_weather_summary)
            min_temp = temp_ts["areas"][0]["temps"][0]
            max_temp = temp_ts["areas"][0]["temps"][1]
            summary_line = f"**{location_name}**: {weather_emoji} {today_weather_summary} | 🌡️ 最高 {max_temp}℃ / 最低 {min_temp}℃"

            forecast_lines = []
            now = datetime.now(JST)
            
            weather_map = {datetime.fromisoformat(t).astimezone(JST): w.split("　")[0] for t, w in zip(weather_ts["timeDefines"], weather_ts["areas"][0]["weathers"])}
            temp_map = {datetime.fromisoformat(t).astimezone(JST): tmp for t, tmp in zip(temp_ts["timeDefines"], temp_ts["areas"][0]["temps"])}
            precip_map = {datetime.fromisoformat(t).astimezone(JST): pop for t, pop in zip(precip_ts["timeDefines"], precip_ts["areas"][0]["pops"])} if precip_ts else {}

            combined_forecast = {}
            all_times = sorted(list(set(weather_map.keys()) | set(temp_map.keys()) | set(precip_map.keys())))

            for dt in all_times:
                if dt.date() != now.date() or dt < now: continue
                
                # 各時間帯のデータを取得（最も近い時間から）
                weather_time = min(weather_map.keys(), key=lambda t: abs(t - dt))
                temp_time = min(temp_map.keys(), key=lambda t: abs(t - dt))
                
                weather = weather_map[weather_time]
                temperature = temp_map[temp_time]
                precipitation = ""
                if precip_map:
                    precip_time = min(precip_map.keys(), key=lambda t: abs(t - dt))
                    precip_chance = precip_map[precip_time]
                    if precip_chance and int(precip_chance) > 0:
                        precipitation = f" (💧{precip_chance}%)"
                
                combined_forecast[dt] = (weather, temperature, precipitation)
            
            # 3時間おきに表示
            for dt, (weather, temp, precip) in sorted(combined_forecast.items()):
                 if dt.hour % 3 == 0:
                     emoji = self._get_emoji_for_weather(weather)
                     forecast_lines.append(f"・`{dt.strftime('%H:%M')}`: {emoji} {weather}, {temp}℃{precip}")

            return f"{summary_line}\n" + "\n".join(forecast_lines) if forecast_lines else summary_line

        except Exception as e:
            logging.error(f"{location_name}の天気予報取得に失敗: {e}", exc_info=True)
            return f"**{location_name}**: ⚠️ 天気情報の取得に失敗しました。"

    async def _summarize_article(self, content: str) -> str:
        # ... (変更なし)
        if not self.gemini_model or not content: return "要約できませんでした。"
        soup = BeautifulSoup(content, 'html.parser')
        text_content = soup.get_text()
        try:
            prompt = f"以下のニュース記事を分析し、この記事を読むべきか判断できるように、最も重要な要点だけを1〜2文で教えてください。出力は「だ・である調」で、要約本文のみとしてください。\n---\n{text_content[:8000]}"
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception: return "要約中にエラーが発生しました。"

    async def _fetch_macro_news(self, rss_urls: list, since: datetime) -> list:
        # ... (変更なし)
        news_items = []
        for url in rss_urls:
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
                for entry in feed.entries:
                    if not getattr(entry, "published_parsed", None): continue
                    pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(JST)
                    if pub_time > since:
                        summary = await self._summarize_article(entry.get("summary", entry.get("content", "")))
                        news_items.append({"title": entry.title, "link": entry.link, "summary": summary})
            except Exception as e:
                logging.error(f"RSSフィードの取得に失敗: {url}, Error: {e}")
        return news_items

    async def _fetch_stock_news(self, stock_code: str, since: datetime) -> list:
        # ... (変更なし)
        news_items = []
        url = f"{YAHOO_FINANCE_RSS_URL}?code={stock_code}.T"
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
            for entry in feed.entries:
                 if not getattr(entry, "published_parsed", None): continue
                 pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(JST)
                 if pub_time > since:
                    summary = await self._summarize_article(entry.get("summary", ""))
                    news_items.append({"title": entry.title, "link": entry.link, "summary": summary})
        except Exception as e:
            logging.error(f"Yahoo!ファイナンス RSSの取得に失敗 (Code: {stock_code}): {e}")
        return news_items

    @tasks.loop(time=NEWS_BRIEFING_TIME)
    async def daily_news_briefing(self):
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel: return

        logging.info("デイリーニュースブリーフィングを開始します...")
        weather_text = await self._get_jma_weather_forecast(JMA_AREA_CODE, self.location_name)
        weather_embed = discord.Embed(title=f"🗓️ {datetime.now(JST).strftime('%Y年%m月%d日')} のお知らせ", color=discord.Color.blue())
        weather_embed.add_field(name="🌦️ 今日の天気", value=weather_text, inline=False)
        await channel.send(embed=weather_embed)
        logging.info("天気予報を投稿しました。")
        since_time = datetime.now(JST) - timedelta(days=1)
        
        try:
            # ... (マクロニュースの処理は変更なし)
            market_news = await self._fetch_macro_news(MACRO_NEWS_RSS_URLS, since_time)
            if market_news:
                embeds_to_send = []
                current_embed = discord.Embed(title="🌐 NHK経済ニュース", color=discord.Color.dark_gold())
                current_length = 0
                for item in market_news:
                    title, summary, link = item.get('title', ''), item.get('summary', ''), item.get('link')
                    if not all([title, summary, link]): continue
                    field_value = f"```{summary}```[記事を読む]({link})"
                    if len(current_embed.fields) >= 25 or (current_length + len(title) + len(field_value)) > 5500:
                        if current_embed.fields: embeds_to_send.append(current_embed)
                        current_embed = discord.Embed(title="🌐 NHK経済ニュース (続き)", color=discord.Color.dark_gold())
                        current_length = 0
                    current_embed.add_field(name=title[:256], value=field_value[:1024], inline=False)
                    current_length += len(title) + len(field_value)
                if current_embed.fields: embeds_to_send.append(current_embed)
                for embed in embeds_to_send: await channel.send(embed=embed)
                logging.info(f"{len(market_news)}件のNHK経済ニュースを処理しました。")
        except Exception as e:
            logging.error(f"NHK経済ニュースの処理でエラー: {e}", exc_info=True)

        try:
            watchlist = await self._get_watchlist()
            if watchlist:
                logging.info(f"{len(watchlist)}件の保有銘柄ニュースをチェックします。")
                all_stock_news = []
                for name, code in watchlist.items():
                    if not code: continue # 銘柄コードがなければスキップ
                    news = await self._fetch_stock_news(code, since_time)
                    if news:
                        all_stock_news.append({"name": name, "news": news[0]})
                    await asyncio.sleep(1)
                if all_stock_news:
                    stock_embed = discord.Embed(title="📈 保有銘柄ニュース (Yahoo!ファイナンス)", color=discord.Color.green())
                    for item in all_stock_news:
                        summary = item['news']['summary'][:150] + "..." if len(item['news']['summary']) > 150 else item['news']['summary']
                        stock_embed.add_field(name=f"{item['name']} ({item['news']['title']})", value=f"```{summary}```[記事を読む]({item['news']['link']})\n", inline=False)
                    await channel.send(embed=stock_embed)
                else:
                    await channel.send(embed=discord.Embed(title="📈 保有銘柄ニュース", description="ウォッチリストの企業の新規記事は見つかりませんでした。", color=discord.Color.greyple()))
        except Exception as e:
            logging.error(f"保有銘柄ニュースの処理でエラーが発生しました: {e}", exc_info=True)
        logging.info("デイリーニュースブリーフィングを完了しました。")

    async def _get_watchlist(self) -> dict:
        try:
            _, res = self.dbx.files_download(self.watchlist_path)
            data = json.loads(res.content)
            if isinstance(data, list):
                logging.warning("古い形式のウォッチリストを検出しました。新しい形式に変換します。")
                new_watchlist = {item: "" for item in data}
                await self._save_watchlist(new_watchlist)
                return new_watchlist
            return data
        except ApiError:
            return {}

    async def _save_watchlist(self, watchlist: dict):
        # ... (変更なし)
        try:
            self.dbx.files_upload(json.dumps(watchlist, ensure_ascii=False, indent=2).encode('utf-8'), self.watchlist_path, mode=WriteMode('overwrite'))
        except Exception as e: logging.error(f"ウォッチリストの保存に失敗: {e}")

    async def _find_stock_code(self, company_name: str) -> str | None:
        # ... (変更なし)
        search_results = await google_search_function([f"{company_name} 銘柄コード"])
        if not search_results or not search_results[0].results: return None
        for result in search_results[0].results:
            match = re.search(r'\b(\d{4})\b', result.description)
            if match:
                logging.info(f"銘柄コードが見つかりました: {company_name} -> {match.group(1)}")
                return match.group(1)
        logging.warning(f"銘柄コードが見つかりませんでした: {company_name}")
        return None

    stock_group = app_commands.Group(name="stock", description="株価ニュースの監視リストを管理します。")

    @stock_group.command(name="add", description="監視リストに企業名を追加します。")
    @app_commands.describe(name="追加する企業名（例: トヨタ自動車）")
    async def stock_add(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if name in watchlist:
            await interaction.followup.send(f"⚠️ ` {name} ` は既にリストに存在します。")
            return
        stock_code = await self._find_stock_code(name)
        if not stock_code:
            await interaction.followup.send(f"❌ ` {name} ` の銘柄コードが見つかりませんでした。正式名称で試してください。")
            return
        watchlist[name] = stock_code
        await self._save_watchlist(watchlist)
        await interaction.followup.send(f"✅ ` {name} ` (コード: {stock_code}) を監視リストに追加しました。")

    @stock_group.command(name="remove", description="監視リストから企業名を削除します。")
    @app_commands.describe(name="削除する企業名")
    async def stock_remove(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if name in watchlist:
            watchlist.pop(name)
            await self._save_watchlist(watchlist)
            await interaction.followup.send(f"🗑️ ` {name} ` を監視リストから削除しました。")
        else:
            await interaction.followup.send(f"⚠️ ` {name} ` はリストに存在しません。")
            
    @stock_group.command(name="list", description="現在の監視リストを表示します。")
    async def stock_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        watchlist = await self._get_watchlist()
        if watchlist:
            list_str = "\n".join([f"- {name} ({code or 'コード未設定'})" for name, code in watchlist.items()])
            await interaction.followup.send(f"現在の監視リスト:\n{list_str}")
        else:
            await interaction.followup.send("監視リストは現在空です。")

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))