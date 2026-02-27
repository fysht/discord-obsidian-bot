# ---------------------------------------------------------
# 1. インポート処理の整理
# 標準ライブラリ -> サードパーティ -> ローカル の順で整理します
# ---------------------------------------------------------
import os
import logging
import datetime
import xml.etree.ElementTree as ET

import discord
from discord.ext import commands, tasks
import aiohttp

# ---------------------------------------------------------
# ローカルモジュールのインポートと定数設定
# ---------------------------------------------------------
# 2. 定数ファイルの作成（config等から読み込む想定）
from config import JST

# ※本来は config/constants.py 等にまとめるのが理想ですが、
# 既存の動作を壊さないよう、当ファイル専用の定数として上部に定義しています。
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {"晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"}
YAHOO_NEWS_RSS_URL = "https://news.yahoo.co.jp/rss/topics/top-picks.xml"


class NewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        self.jma_area_name = os.getenv("JMA_AREA_NAME", "南部")
        
        # ---------------------------------------------------------
        # 3, 4. Google認証 / Geminiクライアントの統合
        # ※ NewsCog自体は直接Drive/Geminiを叩かずPartnerCogに委譲しますが、
        # カレンダー機能は統合された bot.calendar_service を参照するようにします。
        # ---------------------------------------------------------
        self.calendar_service = getattr(bot, 'calendar_service', None)

    @commands.Cog.listener()
    async def on_ready(self):
        target_time = datetime.time(hour=6, minute=0, tzinfo=JST)
        if not self.morning_data_collection.is_running():
            self.morning_data_collection.change_interval(time=target_time)
            self.morning_data_collection.start()

    def cog_unload(self):
        self.morning_data_collection.cancel()

    async def _get_news(self) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(YAHOO_NEWS_RSS_URL) as resp:
                    text = await resp.text()
                    root = ET.fromstring(text)
                    items = root.findall('.//item')[:5]
                    news_texts = []
                    for item in items:
                        title = item.find('title').text if item.find('title') is not None else "タイトルなし"
                        desc_elem = item.find('description')
                        desc = desc_elem.text if desc_elem is not None else ""
                        link_elem = item.find('link')
                        link = link_elem.text if link_elem is not None else ""
                        news_texts.append(f"- **{title}**\n  {desc}\n  {link}")
                    return "\n".join(news_texts)
        except Exception as e:
            logging.error(f"News fetch error: {e}")
            return "ニュースの取得に失敗しました。"

    async def _get_stocks(self) -> str:
        symbols = {"日経平均": "^N225", "S&P500": "^GSPC"}
        stock_texts = []
        try:
            async with aiohttp.ClientSession() as session:
                for name, symbol in symbols.items():
                    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                    async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'}) as resp:
                        data = await resp.json()
                        price = data['chart']['result'][0]['meta']['regularMarketPrice']
                        stock_texts.append(f"- {name}: {price:,.2f}")
            return "\n".join(stock_texts)
        except Exception as e:
            logging.error(f"Stock fetch error: {e}")
            return "株価データの取得に失敗しました。"

    async def _get_jma_weather_forecast(self) -> str:
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"
        weather_text = ""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    data = await response.json()
            area_weather = next((a for a in data[0]["timeSeries"][0]["areas"] if a["area"]["name"] == self.jma_area_name), None)
            if area_weather:
                summary = area_weather["weathers"][0].replace('\u3000', ' ')
                emoji = "❓"
                for key, e in WEATHER_EMOJI_MAP.items():
                    if key in summary: 
                        emoji = e
                        break
                weather_text += f"{emoji} {summary}\n"
            area_temps = next((a for a in data[0]["timeSeries"][2]["areas"] if a["area"]["name"] == self.location_name), None)
            if area_temps and "temps" in area_temps:
                valid_temps = [float(t) for t in area_temps["temps"] if t and t != "--"]
                if valid_temps:
                    weather_text += f"最高 {int(max(valid_temps))}℃ / 最低 {int(min(valid_temps))}℃"
            return weather_text
        except Exception as e:
            logging.error(f"Weather fetch error: {e}")
            return "天気情報の取得に失敗しました。"

    @tasks.loop()
    async def morning_data_collection(self):
        weather_text = await self._get_jma_weather_forecast()
        news_text = await self._get_news()
        stock_text = await self._get_stocks()
        
        partner_cog = self.bot.get_cog("PartnerCog")
        if partner_cog:
            # ---------------------------------------------------------
            # ★ エラー修正箇所: 統合カレンダーサービスから予定を取得
            # ---------------------------------------------------------
            today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            schedule_text = "カレンダーサービスに接続できませんでした。"
            if self.calendar_service:
                schedule_text = await self.calendar_service.list_events_for_date(today_str)

            # 今日のチャットログ（深夜0時以降）を取得して文脈に追加する
            memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
            channel = self.bot.get_channel(memo_channel_id)
            recent_log = ""
            if channel:
                recent_log = await partner_cog.fetch_todays_chat_log(channel)

            context_data = (
                f"【今日の予定】\n{schedule_text}\n\n"
                f"【今日の天気 ({self.location_name})】\n{weather_text}\n\n"
                f"【今日の主要ニュース(概要・URL付)】\n{news_text}\n\n"
                f"【昨晩の株価】\n{stock_text}\n\n"
                f"【最近の会話ログ】\n{recent_log}"
            )
            
            instruction = (
                "「おはよう！」から始まる朝のメッセージを作成して。以下の要素を自然なタメ口で織り交ぜてね。\n"
                "1. 今日の予定を教えてあげる（予定がない場合はその旨を伝える）。\n"
                "2. 気になるニュースを1〜2個ピックアップして、少し詳しく内容を教えてあげる。気になった記事のURLはそのまま出力してOK。\n"
                "3. 天気や株価にも軽く触れる。\n"
                "4. 今日も一日頑張れるようなポジティブな励ましの言葉で締める。\n"
                "※予定やニュースのURLを含めるため、多少長くなっても構いません。見やすく改行を使ってね。"
            )
            
            await partner_cog.generate_and_send_routine_message(context_data, instruction)

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))