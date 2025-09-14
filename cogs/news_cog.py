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
from pyowm import OWM
import google.generativeai as genai
import feedparser
from bs4 import BeautifulSoup

# 他のファイルから関数をインポート
from web_parser import parse_url_with_readability

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
NEWS_BRIEFING_TIME = time(hour=9, minute=10, tzinfo=JST)

MACRO_NEWS_RSS_URLS = [
    "https://news.yahoo.co.jp/rss/categories/business.xml",  # Yahoo!ニュース 経済カテゴリ
]
TDNET_RSS_URL = "https://news.yahoo.co.jp/rss/categories/business.xml"
# -------------------------------------------------------------------------------


class NewsCog(commands.Cog):
    """天気予報と株式関連ニュースを定時通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()

        if not self._are_credentials_valid():
            logging.error("NewsCog: 必須の環境変数が不足しています。このCogは無効化されます。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            )
            if not self.openweathermap_api_key:
                 raise ValueError("OPENWEATHERMAP_API_KEYが設定されていません。")
            self.owm = OWM(self.openweathermap_api_key)
            self.mgr = self.owm.weather_manager()

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

    def _load_environment_variables(self):
        self.news_channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.home_coords = self._parse_coordinates(os.getenv("HOME_COORDINATES"))
        self.work_coords = self._parse_coordinates(os.getenv("WORK_COORDINATES"))
        self.home_name = os.getenv("HOME_NAME", "自宅")
        self.work_name = os.getenv("WORK_NAME", "勤務先")
        self.openweathermap_api_key = os.getenv("OPENWEATHERMAP_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.watchlist_path = f"{self.dropbox_vault_path}/.bot/stock_watchlist.json"

    def _are_credentials_valid(self) -> bool:
        return all([self.news_channel_id, self.home_coords, self.work_coords, self.openweathermap_api_key, self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token, self.gemini_api_key])

    def _parse_coordinates(self, coord_str: str | None) -> dict | None:
        if not coord_str: return None
        try:
            lat, lon = map(float, coord_str.split(','))
            return {'lat': lat, 'lon': lon}
        except (ValueError, TypeError):
            logging.error(f"座標の解析に失敗: {coord_str}")
            return None

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready and not self.daily_news_briefing.is_running():
            self.daily_news_briefing.start()

    def cog_unload(self):
        self.daily_news_briefing.cancel()

    async def _get_weather_forecast(self, coords: dict, location_name: str) -> str:
        try:
            # 現在の天気
            observation = await asyncio.to_thread(self.mgr.weather_at_coords, coords['lat'], coords['lon'])
            current_weather = observation.weather
            current_temp = current_weather.temperature('celsius').get('temp', None)
            current_status = current_weather.detailed_status if hasattr(current_weather, 'detailed_status') else None

            # 3時間毎予報（当日分を抽出）
            try:
                forecast = await asyncio.to_thread(self.mgr.forecast_at_coords, coords['lat'], coords['lon'], '3h')
                forecasts = getattr(forecast, "forecast", None)
                # forecasts.weathers が予報リスト（Weatherオブジェクト）になる想定
                forecast_weathers = getattr(forecasts, "weathers", []) if forecasts else []
            except Exception:
                # 予報が取得できない場合は空にする（エラーにしない）
                forecast_weathers = []

            # 本日（JST）の予報のみ抽出
            today = datetime.now(JST).date()
            today_forecasts = []
            for w in forecast_weathers:
                try:
                    rt = w.reference_time('date')  # datetime
                    rt_jst = rt.astimezone(JST)
                    if rt_jst.date() == today:
                        today_forecasts.append((rt_jst, w))
                except Exception:
                    continue

            # 予報から温度リストと表示行を作成（取得できない要素はスキップ）
            temps = []
            forecast_lines = []
            for rt_jst, w in today_forecasts:
                tdict = w.temperature('celsius')
                if not tdict:
                    continue
                temp = tdict.get('temp', None)
                if temp is None:
                    continue
                temps.append(temp)
                time_str = rt_jst.strftime('%H:%M')
                status = getattr(w, 'detailed_status', '') or ''
                forecast_lines.append(f"{time_str} {temp:.0f}℃{f' ({status})' if status else ''}")

            # 表示文を組み立てる（取得できたものだけを表示）
            parts = []
            if current_status and current_temp is not None:
                parts.append(f"**{location_name}**: {current_status} | 現在 {current_temp:.0f}℃")
            elif current_temp is not None:
                parts.append(f"**{location_name}**: 現在 {current_temp:.0f}℃")
            elif current_status:
                parts.append(f"**{location_name}**: {current_status}")
            else:
                parts.append(f"**{location_name}**: 天気情報（現在値）は取得できませんでした。")

            if temps:
                tmax = max(temps)
                tmin = min(temps)
                parts[0] += f" (最高 {tmax:.0f}℃ / 最低 {tmin:.0f}℃)"

            if forecast_lines:
                parts.append("本日の予報: " + ", ".join(forecast_lines))

            return "\n".join(parts)
        except Exception as e:
            logging.error(f"{location_name}の天気予報取得に失敗: {e}", exc_info=True)
            return f"**{location_name}**: 天気情報の取得に失敗しました。"
    # ---------------------------------------------------------------------------

    async def _summarize_article(self, content: str) -> str:
        if not self.gemini_model or not content:
            return "要約できませんでした。"
        soup = BeautifulSoup(content, 'html.parser')
        text_content = soup.get_text()
        try:
            prompt = f"以下のニュース記事を3～4文程度の簡潔な「だ・である調」で要約せよ。\n---{text_content[:8000]}"
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
                    # published_parsed が無い場合はスキップ
                    if not getattr(entry, "published_parsed", None):
                        continue
                    pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(JST)
                    if pub_time > since:
                        # 記事本文を可能なら取得して要約（なければRSSのsummaryを渡す）
                        try:
                            _, content = await asyncio.to_thread(parse_url_with_readability, entry.link)
                        except Exception:
                            content = entry.get("summary", "")
                        summary = await self._summarize_article(content)
                        news_items.append({
                            "title": entry.title,
                            "link": entry.link,
                            "summary": summary
                        })
            except Exception as e:
                logging.error(f"RSSフィードの取得に失敗: {url}, Error: {e}", exc_info=True)
        return news_items
    # ---------------------------------------------------------------------------

    async def _fetch_stock_news(self, company: str, rss_url: str, since: datetime) -> list:
        news_items = []
        try:
            feed = await asyncio.to_thread(feedparser.parse, rss_url)
            for entry in feed.entries:
                 if not getattr(entry, "published_parsed", None):
                     continue
                 pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(JST)
                 if pub_time <= since:
                     continue
                 title = entry.title or ""
                 summary_text = entry.get("summary", "") or ""
                 # タイトルまたは要約に企業名が含まれる記事を対象
                 if company in title or company in summary_text:
                    try:
                        _, content = await asyncio.to_thread(parse_url_with_readability, entry.link)
                    except Exception:
                        content = summary_text
                    summary = await self._summarize_article(content)
                    news_items.append({
                        "title": entry.title,
                        "link": entry.link,
                        "summary": summary
                    })
        except Exception as e:
            logging.error(f"TDnet/Yahoo RSS の取得に失敗: Error: {e}", exc_info=True)
        return news_items
    # ---------------------------------------------------------------------------

    @tasks.loop(time=NEWS_BRIEFING_TIME)
    async def daily_news_briefing(self):
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel:
            logging.error(f"ニュースチャンネル(ID: {self.news_channel_id})が見つかりません。")
            return

        logging.info("デイリーニュースブリーフィングを開始します...")
        
        # --- 天気予報を投稿 ---
        try:
            home_weather, work_weather = await asyncio.gather(
                self._get_weather_forecast(self.home_coords, self.home_name),
                self._get_weather_forecast(self.work_coords, self.work_name)
            )
            weather_embed = discord.Embed(
                title=f"🗓️ {datetime.now(JST).strftime('%Y年%m月%d日')} のお知らせ",
                color=discord.Color.blue()
            )
            weather_embed.add_field(name="🌦️ 今日の天気", value=f"{home_weather}\n{work_weather}", inline=False)
            await channel.send(embed=weather_embed)
            logging.info("天気予報を投稿しました。")
        except Exception as e:
            logging.error(f"天気予報の処理全体でエラーが発生しました: {e}", exc_info=True)
        
        since_time = datetime.now(JST) - timedelta(days=1)

        # --- マクロ経済ニュースを投稿 ---
        try:
            market_news = await self._fetch_macro_news(MACRO_NEWS_RSS_URLS, since_time)
            if market_news:
                macro_embed = discord.Embed(title="🌐 市場全体のニュース", color=discord.Color.dark_gold())
                news_text = ""
                # --- 変更点: 制限なし（すべて表示） ---
                for item in market_news:
                    summary = item['summary'][:250] + "..." if len(item['summary']) > 250 else item['summary']
                    news_text += f"**[{item['title']}]({item['link']})**\n```{summary}```\n"
                # -----------------------------------------------------------------
                if news_text:
                    macro_embed.description = news_text
                    await channel.send(embed=macro_embed)
                logging.info(f"{len(market_news)}件のマクロ経済ニュースを処理しました。")
            else:
                logging.info("新しいマクロ経済ニュースは見つかりませんでした。")
        except Exception as e:
            logging.error(f"マクロ経済ニュースの処理でエラーが発生しました: {e}", exc_info=True)

        # --- 保有銘柄ニュースを投稿 ---
        try:
            watchlist = await self._get_watchlist()
            if watchlist:
                logging.info(f"{len(watchlist)}件の保有銘柄ニュースをチェックします。")
                for company in watchlist:
                    company_news = await self._fetch_stock_news(company, TDNET_RSS_URL, since_time)
                    if company_news:
                        # すべての該当記事のうち最初の1件のみを埋め込む（元コードの仕様を保持）
                        item = company_news[0]
                        stock_embed = discord.Embed(title=f"📈 保有銘柄ニュース: {company}", color=discord.Color.green())
                        summary = item['summary'][:200] + "..." if len(item['summary']) > 200 else item['summary']
                        stock_embed.description = f"**[{item['title']}]({item['link']})**\n```{summary}```\n"
                        await channel.send(embed=stock_embed)
                    else:
                        # 該当記事がなければ明示的に通知
                        no_embed = discord.Embed(title=f"📈 保有銘柄ニュース: {company}", color=discord.Color.greyple())
                        no_embed.description = "該当する記事は見つかりませんでした。"
                        await channel.send(embed=no_embed)

                    await asyncio.sleep(2)
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