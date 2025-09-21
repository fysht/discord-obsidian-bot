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
from collections import Counter
import re

# 他のファイルから関数をインポート
from web_parser import parse_url_with_readability

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
NEWS_BRIEFING_TIME = time(hour=18, minute=37, tzinfo=JST)

# ニュースソース
MACRO_NEWS_RSS_URLS = [
    "https://www.nhk.or.jp/rss/news/cat2.xml",  # NHKニュース 経済
]
# Yahoo!ファイナンスのRSSフィードURL
YAHOO_FINANCE_RSS_URL = "https://finance.yahoo.co.jp/rss/company"

# 気象庁のエリアコード (例: 岡山県)
# 参考: https://www.jma.go.jp/bosai/common/const/area.json
JMA_AREA_CODE_HOME = "330000" # 自宅エリアの予報区コード
JMA_AREA_CODE_WORK = "330000" # 勤務先エリアの予報区コード

# 天気の絵文字マッピング
WEATHER_EMOJI_MAP = {
    "晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️",
}

class NewsCog(commands.Cog):
    """天気予報と株式関連ニュースを定時通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.session = aiohttp.ClientSession()

        if not self._are_credentials_valid():
            logging.error("NewsCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            )

            if self.gemini_api_key:
                genai.configure(api_key=self.gemini_api_key)
                self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            else:
                self.gemini_model = None
                logging.warning("NewsCog: GEMINI_API_KEYが設定されていないため、ニュース要約機能は無効です。")

            self.is_ready = True
            logging.info("✅ NewsCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ NewsCogの初期化中にエラーが発生しました: {e}", exc_info=True)

    async def cog_unload(self):
        await self.session.close()

    def _load_environment_variables(self):
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.location_name = os.getenv("LOCATION_NAME", "岡山") 
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
            if key in weather_text:
                return emoji
        return "❓"

    async def _get_jma_weather_forecast(self, area_code: str, location_name: str) -> str:
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json"
        try:
            async with self.session.get(url) as response:
                response.raise_for_status()
                data = await response.json()

            weather_timeseries = next((ts for ts in data[0]["timeSeries"] if "weathers" in ts["areas"][0]), None)
            temp_timeseries = next((ts for ts in data[0]["timeSeries"] if "temps" in ts["areas"][0] and len(ts["timeDefines"]) > 2), None)
            
            if not weather_timeseries or not temp_timeseries:
                raise ValueError("必要な天気または気温データが見つかりません。")

            # サマリー情報
            today_weather_summary = weather_timeseries["areas"][0]["weathers"][0]
            weather_emoji = self._get_emoji_for_weather(today_weather_summary)
            min_temp = temp_timeseries["areas"][0]["temps"][0]
            max_temp = temp_timeseries["areas"][0]["temps"][1]
            summary_line = f"**{location_name}**: {weather_emoji} {today_weather_summary} | 🌡️ 最高 {max_temp}℃ / 最低 {min_temp}℃"
            
            # 時系列情報
            time_defines = temp_timeseries["timeDefines"]
            temps = temp_timeseries["areas"][0]["temps"]
            weather_map = {datetime.fromisoformat(t).hour: w for t, w in zip(weather_timeseries["timeDefines"], weather_timeseries["areas"][0]["weathers"])}

            forecast_lines = []
            for i, time_str in enumerate(time_defines):
                dt = datetime.fromisoformat(time_str)
                if dt.date() != datetime.now(JST).date(): continue
                
                time_formatted = dt.strftime('%H時')
                temp_str = f"{temps[i]}℃"
                
                weather_hour = (dt.hour // 6) * 6
                weather = weather_map.get(weather_hour, "").split("　")[0]
                emoji = self._get_emoji_for_weather(weather)
                
                forecast_lines.append(f"・{time_formatted}: {emoji} {weather}, {temp_str}")
            
            return f"{summary_line}\n" + "\n".join(forecast_lines) if forecast_lines else summary_line

        except Exception as e:
            logging.error(f"{location_name}の天気予報取得に失敗: {e}", exc_info=True)
            return f"**{location_name}**: ⚠️ 天気情報の取得に失敗しました。"

    async def _summarize_article(self, content: str) -> str:
        # ... (変更なし) ...
        if not self.gemini_model or not content:
            return "要約できませんでした。"
        soup = BeautifulSoup(content, 'html.parser')
        text_content = soup.get_text()
        try:
            prompt = f"""以下のニュース記事を分析し、この記事を読むべきか判断できるように、最も重要な要点だけを1〜2文で教えてください。
            出力は「だ・である調」で、要約本文のみとしてください。
            ---
            {text_content[:8000]}
            """
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"ニュースの要約中にエラー: {e}")
            return "要約中にエラーが発生しました。"

    async def _fetch_macro_news(self, rss_urls: list, since: datetime) -> list:
        # ... (変更なし) ...
        news_items = []
        for url in rss_urls:
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
                for entry in feed.entries:
                    if not getattr(entry, "published_parsed", None):
                        continue
                    pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(JST)
                    if pub_time > since:
                        summary = await self._summarize_article(entry.get("summary", entry.get("content", "")))
                        news_items.append({
                            "title": entry.title,
                            "link": entry.link,
                            "summary": summary
                        })
            except Exception as e:
                logging.error(f"RSSフィードの取得に失敗: {url}, Error: {e}")
        return news_items

    async def _fetch_stock_news(self, stock_code: str, since: datetime) -> list:
        """Yahoo!ファイナンスから指定された銘柄コードのニュースを取得"""
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
        
        # --- 天気予報の重複を排除 ---
        weather_text = await self._get_jma_weather_forecast(JMA_AREA_CODE_HOME, self.location_name)
        weather_embed = discord.Embed(
            title=f"🗓️ {datetime.now(JST).strftime('%Y年%m月%d日')} のお知らせ",
            color=discord.Color.blue()
        )
        weather_embed.add_field(name="🌦️ 今日の天気", value=weather_text, inline=False)
        await channel.send(embed=weather_embed)
        logging.info("天気予報を投稿しました。")
        
        since_time = datetime.now(JST) - timedelta(days=1)
        
        try:
            market_news = await self._fetch_macro_news(MACRO_NEWS_RSS_URLS, since_time)
            if market_news:
                embeds_to_send = []
                current_embed = discord.Embed(title="🌐 NHK経済ニュース", color=discord.Color.dark_gold())
                current_length = 0

                for item in market_news:
                    title = item.get('title', '').strip()
                    summary = item.get('summary', '').strip()
                    link = item.get('link')

                    if not title or not summary or not link:
                        continue

                    field_value = f"```{summary}```[記事を読む]({link})"
                    
                    if len(current_embed.fields) >= 25 or (current_length + len(title) + len(field_value)) > 5500:
                        if current_embed.fields:
                            embeds_to_send.append(current_embed)
                        current_embed = discord.Embed(title="🌐 NHK経済ニュース (続き)", color=discord.Color.dark_gold())
                        current_length = 0

                    current_embed.add_field(name=title[:256], value=field_value[:1024], inline=False)
                    current_length += len(title) + len(field_value)

                if current_embed.fields:
                    embeds_to_send.append(current_embed)

                for embed in embeds_to_send:
                    await channel.send(embed=embed)
                
                logging.info(f"{len(market_news)}件のNHK経済ニュースを処理しました。")
            else:
                logging.info("新しいNHK経済ニュースは見つかりませんでした。")
        except Exception as e:
            logging.error(f"NHK経済ニュースの処理でエラーが発生しました: {e}", exc_info=True)

        try:
            watchlist = await self._get_watchlist()
            if watchlist:
                logging.info(f"{len(watchlist)}件の保有銘柄ニュースをチェックします。")
                all_stock_news = []
                for code, name in watchlist.items():
                    news = await self._fetch_stock_news(code, since_time)
                    if news:
                        all_stock_news.append({"name": name, "news": news[0]})
                    await asyncio.sleep(2)

                if all_stock_news:
                    stock_embed = discord.Embed(title="📈 保有銘柄ニュース (Yahoo!ファイナンス)", color=discord.Color.green())
                    for item in all_stock_news:
                        summary = item['news']['summary'][:150] + "..." if len(item['news']['summary']) > 150 else item['news']['summary']
                        stock_embed.add_field(
                            name=f"{item['name']} ({item['news']['title']})",
                            value=f"```{summary}```[記事を読む]({item['news']['link']})\n",
                            inline=False
                        )
                    await channel.send(embed=stock_embed)
                else:
                    await channel.send(embed=discord.Embed(title="📈 保有銘柄ニュース", description="ウォッチリストの企業の新規記事は見つかりませんでした。", color=discord.Color.greyple()))
        except Exception as e:
            logging.error(f"保有銘柄ニュースの処理でエラーが発生しました: {e}", exc_info=True)
        
        logging.info("デイリーニュースブリーフィングを完了しました。")

    async def _get_watchlist(self) -> dict:
        try:
            _, res = self.dbx.files_download(self.watchlist_path)
            return json.loads(res.content)
        except ApiError:
            return {}

    async def _save_watchlist(self, watchlist: dict):
        # ... (変更なし) ...
        try:
            self.dbx.files_upload(json.dumps(watchlist, ensure_ascii=False, indent=2).encode('utf-8'),
                                  self.watchlist_path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"ウォッチリストの保存に失敗: {e}")

    stock_group = app_commands.Group(name="stock", description="株価ニュースの監視リストを管理します。")

    @stock_group.command(name="add", description="監視リストに銘柄コードと企業名を追加します。")
    @app_commands.describe(code="追加する銘柄コード（例: 7203）", name="企業名（例: トヨタ自動車）")
    async def stock_add(self, interaction: discord.Interaction, code: str, name: str):
        watchlist = await self._get_watchlist()
        if code not in watchlist:
            watchlist[code] = name
            await self._save_watchlist(watchlist)
            await interaction.response.send_message(f"✅ ` {name} ({code}) ` を監視リストに追加しました。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ ` {code} ` は既にリストに存在します。", ephemeral=True)

    @stock_group.command(name="remove", description="監視リストから銘柄コードを削除します。")
    @app_commands.describe(code="削除する銘柄コード")
    async def stock_remove(self, interaction: discord.Interaction, code: str):
        watchlist = await self._get_watchlist()
        if code in watchlist:
            name = watchlist.pop(code)
            await self._save_watchlist(watchlist)
            await interaction.response.send_message(f"🗑️ ` {name} ({code}) ` を監視リストから削除しました。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ ` {code} ` はリストに存在しません。", ephemeral=True)
            
    @stock_group.command(name="list", description="現在の監視リストを表示します。")
    async def stock_list(self, interaction: discord.Interaction):
        watchlist = await self._get_watchlist()
        if watchlist:
            list_str = "\n".join([f"- {name} ({code})" for code, name in watchlist.items()])
            await interaction.response.send_message(f"現在の監視リスト:\n{list_str}", ephemeral=True)
        else:
            await interaction.response.send_message("監視リストは現在空です。", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))