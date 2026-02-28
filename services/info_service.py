import aiohttp
import xml.etree.ElementTree as ET
import logging
import datetime

# --- リファクタリング: 定数のクリーンなインポート ---
from config import JST

# 気象庁エリアコード: 岡山県 = 330000
# (参考: 東京=130000, 大阪=270000)
WEATHER_AREA_CODE = "330000" 
WEATHER_URL = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{WEATHER_AREA_CODE}.json"
NEWS_RSS_URL = "https://news.yahoo.co.jp/rss/topics/top-picks.xml"

class InfoService:
    def __init__(self):
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def get_weather(self):
        """気象庁から今日の天気（岡山）を取得"""
        try:
            session = await self._get_session()
            async with session.get(WEATHER_URL) as resp:
                if resp.status != 200:
                    return "天気情報取得失敗", "N/A", "N/A"
                
                data = await resp.json()
                weather_text = data[0]["timeSeries"][0]["areas"][0]["weathers"][0]
                weather_text = weather_text.replace("\u3000", " ") 
                
                temps = data[0]["timeSeries"][2]["areas"][0].get("temps", [])
                
                if len(temps) >= 2:
                    return weather_text, temps[1], temps[0]
                elif len(temps) == 1:
                    return weather_text, temps[0], "N/A"
                else:
                    return weather_text, "N/A", "N/A"

        except Exception as e:
            logging.error(f"Weather Fetch Error: {e}")
            return "取得エラー", "N/A", "N/A"

    async def get_news(self, limit=3):
        """YahooニュースRSSからヘッドラインを取得"""
        try:
            session = await self._get_session()
            async with session.get(NEWS_RSS_URL) as resp:
                if resp.status != 200:
                    return []
                
                xml_content = await resp.text()
                root = ET.fromstring(xml_content)
                
                items = root.findall(".//item")
                headlines = []
                for item in items[:limit]:
                    title = item.find("title").text
                    headlines.append(f"{title}")
                
                return headlines
        except Exception as e:
            logging.error(f"News Fetch Error: {e}")
            return []
    
    async def get_info_summary(self):
        """天気とニュースをまとめた文字列を返す"""
        w_text, t1, t2 = await self.get_weather()
        news_list = await self.get_news()
        
        weather_str = f"☁️ 天気: {w_text} (最高: {t1}℃ / 最低: {t2}℃)\n"
        news_str = "📰 今日のニュース:\n" + "\n".join([f"- {n}" for n in news_list])
        
        return weather_str + "\n" + news_str