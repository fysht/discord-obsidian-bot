import aiohttp
import xml.etree.ElementTree as ET
import logging
import re


class InfoService:
    def __init__(self):
        # 岡山県の気象庁JSONコード
        self.weather_url = (
            "https://www.jma.go.jp/bosai/forecast/data/forecast/330000.json"
        )

    async def get_weather(self):
        """Yahoo!天気から岡山の天気を取得（スクレイピング形式）"""
        url = "https://weather.yahoo.co.jp/weather/jp/33/6110.html" # 岡山南部
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        
                        # 天気
                        weather_match = re.search(r'<p class="pict">.*?alt="([^"]+)"', html, re.DOTALL)
                        weather_text = weather_match.group(1).strip() if weather_match else "取得失敗"
                        
                        # 気温
                        high_match = re.search(r'<li class="high">.*?em>(\d+)</em>', html, re.DOTALL)
                        low_match = re.search(r'<li class="low">.*?em>(\d+)</em>', html, re.DOTALL)
                        
                        max_t = high_match.group(1) if high_match else "N/A"
                        min_t = low_match.group(1) if low_match else "N/A"
                        
                        weather_value = f'"{weather_text} (最高: {max_t}℃ / 最低: {min_t}℃)"'
                        return weather_value, max_t, min_t
        except Exception as e:
            logging.error(f"Yahoo Weather Fetch Error: {e}")
        return '"天気情報の取得に失敗しました"', "N/A", "N/A"

    async def get_news(self, limit=3):
        """Yahoo!ニュースのRSSからタイトルと本物のURLを取得"""
        url = "https://news.yahoo.co.jp/rss/topics/top-picks.xml"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        xml_data = await response.text()
                        root = ET.fromstring(xml_data)
                        news_list = []
                        # 最新のニュースをlimit件取得
                        for item in root.findall(".//item")[:limit]:
                            title = item.find("title").text
                            link = item.find("link").text
                            # タイトルとURLをセットにしてAIに渡す
                            news_list.append(f"{title}\n{link}")
                        return news_list
        except Exception as e:
            logging.error(f"News Fetch Error: {e}")
        return []
