import os
import discord
from discord.ext import commands, tasks
import logging
import datetime
import zoneinfo
import aiohttp
import xml.etree.ElementTree as ET

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {"晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"}

class NewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        self.jma_area_name = os.getenv("JMA_AREA_NAME", "南部")

    @commands.Cog.listener()
    async def on_ready(self):
        target_time = datetime.time(hour=6, minute=0, tzinfo=JST)
        if not self.morning_data_collection.is_running():
            self.morning_data_collection.change_interval(time=target_time)
            self.morning_data_collection.start()

    def cog_unload(self):
        self.morning_data_collection.cancel()

    async def _get_news(self) -> str:
        url = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    text = await resp.text()
                    root = ET.fromstring(text)
                    items = root.findall('.//item')[:3]
                    return "\n".join([f"- {item.find('title').text}" for item in items])
        except Exception: return "ニュースの取得に失敗しました。"

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
        except Exception: return "株価データの取得に失敗しました。"

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
                        if key in summary: emoji = e; break
                    weather_text += f"{emoji} {summary}\n"
                area_temps = next((a for a in data[0]["timeSeries"][2]["areas"] if a["area"]["name"] == self.location_name), None)
                if area_temps and "temps" in area_temps:
                    valid_temps = [float(t) for t in area_temps["temps"] if t and t != "--"]
                    if valid_temps:
                        weather_text += f"最高 {int(max(valid_temps))}℃ / 最低 {int(min(valid_temps))}℃"
            return weather_text
        except Exception: return "天気情報の取得に失敗しました。"

    @tasks.loop()
    async def morning_data_collection(self):
        weather_text = await self._get_jma_weather_forecast()
        news_text = await self._get_news()
        stock_text = await self._get_stocks()
        context_data = f"【今日の天気 ({self.location_name})】\n{weather_text}\n\n【今日の主要ニュース】\n{news_text}\n\n【昨晩の株価】\n{stock_text}"
        instruction = "「おはようございます！」から始まる朝のメッセージを作成してください。ニュースや株価に対して簡単な一言コメントを添え、今日も一日頑張ろうと思えるようなポジティブな励ましを入れてください。"
        
        partner_cog = self.bot.get_cog("PartnerCog")
        if partner_cog:
            await partner_cog.generate_and_send_routine_message(context_data, instruction)

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))