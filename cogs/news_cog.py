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
NEWS_BRIEFING_TIME = time(hour=12, minute=10, tzinfo=JST)

# ニュースソースを役割分担
MACRO_NEWS_RSS_URLS = [
    "https://www.nhk.or.jp/rss/news/cat2.xml",  # NHKニュース 経済
]
# 個別銘柄はこちらから取得
TDNET_RSS_URL = "https://news.yahoo.co.jp/rss/categories/business.xml"

# 気象庁のエリアコード (例: 岡山県)
# 参考: https://www.jma.go.jp/bosai/common/const/area.json
JMA_AREA_CODE_HOME = "330000" # 岡山県の予報区コード
JMA_AREA_CODE_WORK = "330000" # 岡山県の予報区コード

# 天気の絵文字マッピング
WEATHER_EMOJI_MAP = {
    "晴": "☀️",
    "曇": "☁️",
    "雨": "☔️",
    "雪": "❄️",
    "雷": "⚡️",
    "霧": "🌫️",
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
        self.home_name = os.getenv("HOME_NAME", "自宅")
        self.work_name = os.getenv("WORK_NAME", "勤務先")
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
        """天気テキストに対応する絵文字を返す"""
        for key, emoji in WEATHER_EMOJI_MAP.items():
            if key in weather_text:
                return emoji
        return "❓"

    async def _get_jma_weather_forecast(self, area_code: str, location_name: str) -> str:
        """気象庁のAPIから詳細な天気予報を取得する"""
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json"
        try:
            async with self.session.get(url) as response:
                response.raise_for_status()
                data = await response.json()

            # --- サマリー情報の抽出 ---
            today_weather_summary = data[0]["timeSeries"][0]["areas"][0]["weathers"][0]
            weather_emoji = self._get_emoji_for_weather(today_weather_summary)
            # timeSeries[2]が日中の最高・最低気温
            temps_summary = data[0]["timeSeries"][2]["areas"][0]
            min_temp = temps_summary["temps"][0]
            max_temp = temps_summary["temps"][1]

            summary_line = f"**{location_name}**: {weather_emoji} {today_weather_summary} | 🌡️ 最高 {max_temp}℃ / 最低 {min_temp}℃"

            # --- 時系列情報の抽出 ---
            weather_timeseries_data = data[0]["timeSeries"][0]
            # timeSeries[1]が3時間ごとの気温
            temp_timeseries_data = data[0]["timeSeries"][1] 

            time_defines = weather_timeseries_data["timeDefines"]
            weathers = weather_timeseries_data["areas"][0]["weathers"]
            
            temp_time_defines = temp_timeseries_data["timeDefines"]
            temps = temp_timeseries_data["areas"][0]["temps"]
            
            # 気温データを時間でマッピングする辞書を作成
            temp_map = {}
            for i, time_str in enumerate(temp_time_defines):
                 dt = datetime.fromisoformat(time_str).astimezone(JST)
                 temp_map[dt.strftime('%H時')] = temps[i]

            forecast_lines = []
            for i, time_str in enumerate(time_defines):
                dt = datetime.fromisoformat(time_str).astimezone(JST)
                
                # 今日の日付の予報のみを対象
                if dt.date() != datetime.now(JST).date():
                    continue

                time_formatted = dt.strftime('%H時')
                weather = weathers[i].split("　")[0] # 「晴れ　後　くもり」のような場合、最初の天気を採用
                emoji = self._get_emoji_for_weather(weather)
                
                temp_str = f"{temp_map.get(time_formatted, '--')}℃"

                forecast_lines.append(f"・🕒 {time_formatted}: {emoji} {weather}, {temp_str}")

            if not forecast_lines:
                return summary_line # 時系列データがなければサマリーのみ返す

            detail_lines = "\n".join(forecast_lines)
            
            return f"{summary_line}\n{detail_lines}"

        except Exception as e:
            logging.error(f"{location_name}の天気予報取得に失敗: {e}", exc_info=True)
            return f"**{location_name}**: ⚠️ 天気情報の取得に失敗しました。"

    async def _summarize_article(self, content: str) -> str:
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

    async def _fetch_stock_news(self, company: str, rss_url: str, since: datetime) -> list:
        news_items = []
        try:
            feed = await asyncio.to_thread(feedparser.parse, rss_url)
            for entry in feed.entries:
                 if not getattr(entry, "published_parsed", None):
                     continue
                 pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(JST)
                 if company in entry.title and pub_time > since:
                    summary = await self._summarize_article(entry.get("summary", entry.get("content", "")))
                    news_items.append({
                        "title": entry.title,
                        "link": entry.link,
                        "summary": summary
                    })
        except Exception as e:
            logging.error(f"Yahoo!ニュース RSSの取得に失敗: Error: {e}")
        return news_items

    @tasks.loop(time=NEWS_BRIEFING_TIME)
    async def daily_news_briefing(self):
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel:
            logging.error(f"ニュースチャンネル(ID: {self.news_channel_id})が見つかりません。")
            return

        logging.info("デイリーニュースブリーフィングを開始します...")
        
        try:
            home_weather, work_weather = await asyncio.gather(
                self._get_jma_weather_forecast(JMA_AREA_CODE_HOME, self.home_name),
                self._get_jma_weather_forecast(JMA_AREA_CODE_WORK, self.work_name)
            )
            weather_embed = discord.Embed(
                title=f"🗓️ {datetime.now(JST).strftime('%Y年%m月%d日')} のお知らせ",
                color=discord.Color.blue()
            )
            weather_embed.add_field(name="🌦️ 今日の天気", value=f"{home_weather}\n\n{work_weather}", inline=False)
            await channel.send(embed=weather_embed)
            logging.info("天気予報を投稿しました。")
        except Exception as e:
            logging.error(f"天気予報の処理全体でエラーが発生しました: {e}", exc_info=True)
        
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
            no_article_companies = []
            if watchlist:
                logging.info(f"{len(watchlist)}件の保有銘柄ニュースをチェックします。")
                for company in watchlist:
                    company_news = await self._fetch_stock_news(company, TDNET_RSS_URL, since_time)
                    if company_news:
                        item = company_news[0]
                        stock_embed = discord.Embed(title=f"📈 保有銘柄ニュース: {company}", color=discord.Color.green())
                        summary = item['summary'][:200] + "..." if len(item['summary']) > 200 else item['summary']
                        stock_embed.description = f"**[{item['title']}]({item['link']})**\n```{summary}```\n"
                        await channel.send(embed=stock_embed)
                    else:
                        no_article_companies.append(company)
                    await asyncio.sleep(2)
                
                if no_article_companies:
                    no_news_embed = discord.Embed(
                        title="📈 保有銘柄ニュース",
                        description=f"以下の企業の新規記事は見つかりませんでした:\n- " + "\n- ".join(no_article_companies),
                        color=discord.Color.greyple()
                    )
                    await channel.send(embed=no_news_embed)
        except Exception as e:
            logging.error(f"保有銘柄ニュースの処理でエラーが発生しました: {e}", exc_info=True)
        
        logging.info("デイリーニュースブリーフィングを完了しました。")

    async def _get_watchlist(self) -> list:
        try:
            _, res = self.dbx.files_download(self.watchlist_path)
            return json.loads(res.content)
        except ApiError:
            return []

    async def _save_watchlist(self, watchlist: list):
        try:
            self.dbx.files_upload(json.dumps(watchlist, ensure_ascii=False, indent=2).encode('utf-8'),
                                  self.watchlist_path, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"ウォッチリストの保存に失敗: {e}")

    stock_group = app_commands.Group(name="stock", description="株価ニュースの監視リストを管理します。")

    @stock_group.command(name="add", description="監視リストに新しい企業を追加します。")
    @app_commands.describe(company="追加する企業名または銘柄コード")
    async def stock_add(self, interaction: discord.Interaction, company: str):
        watchlist = await self._get_watchlist()
        if company not in watchlist:
            watchlist.append(company)
            await self._save_watchlist(watchlist)
            await interaction.response.send_message(f"✅ ` {company} ` を監視リストに追加しました。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ ` {company} ` は既にリストに存在します。", ephemeral=True)

    @stock_group.command(name="remove", description="監視リストから企業を削除します。")
    @app_commands.describe(company="削除する企業名または銘柄コード")
    async def stock_remove(self, interaction: discord.Interaction, company: str):
        watchlist = await self._get_watchlist()
        if company in watchlist:
            watchlist.remove(company)
            await self._save_watchlist(watchlist)
            await interaction.response.send_message(f"🗑️ ` {company} ` を監視リストから削除しました。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ ` {company} ` はリストに存在しません。", ephemeral=True)
            
    @stock_group.command(name="list", description="現在の監視リストを表示します。")
    async def stock_list(self, interaction: discord.Interaction):
        watchlist = await self._get_watchlist()
        if watchlist:
            list_str = "\n".join([f"- {company}" for company in watchlist])
            await interaction.response.send_message(f"現在の監視リスト:\n{list_str}", ephemeral=True)
        else:
            await interaction.response.send_message("監視リストは現在空です。", ephemeral=True)

async def setup(bot: commands.Bot):
    """Cogをボットに登録するためのセットアップ関数"""
    await bot.add_cog(NewsCog(bot))