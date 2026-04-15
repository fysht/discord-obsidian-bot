import aiohttp
import xml.etree.ElementTree as ET
import logging
import re
import datetime
from config import JST

class InfoService:
    def __init__(self):
        self.weather_url = "https://www.jma.go.jp/bosai/forecast/data/forecast/330000.json"

    async def get_weather(self):
        """気象庁APIから詳細な時系列予報を取得し、未来のデータのみ抽出"""
        url = self.weather_url
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        forecast = data[0]
                        # 岡山南部 (areas[0])
                        weathers = forecast["timeSeries"][0]["areas"][0].get("weathers", ["不明"])
                        pops = forecast["timeSeries"][1]["areas"][0].get("pops", [])
                        times_pop = forecast["timeSeries"][1].get("timeDefines", [])
                        temps = forecast["timeSeries"][2]["areas"][0].get("temps", [])
                        
                        # 降水確率がある分だけスロットを作成
                        slots = []
                        for i in range(min(len(pops), 6)):
                            dt = datetime.datetime.fromisoformat(times_pop[i])
                            slots.append({
                                "time": dt.strftime("%H:%M"),
                                "icon": self._get_weather_icon_by_text(weathers[0]),
                                "pop": f"{pops[i]}%",
                                "temp": temps[i] if i < len(temps) else "--"
                            })
                        
                        summary = weathers[0]
                        if temps: summary += f" ({temps[0]}℃)"
                        
                        return {
                            "summary": summary,
                            "slots": slots,
                            "max_temp": temps[1] if len(temps) > 1 else "--",
                            "min_temp": temps[0] if len(temps) > 0 else "--"
                        }
                    return {"summary": "取得失敗 (Server Error)"}
        except Exception as e:
            logging.error(f"Weather Fetch Error: {e}")
            return {"summary": "取得失敗 (JSON Error)"}

    def _get_weather_icon_by_text(self, text):
        if "晴" in text: return "☀️"
        if "雨" in text: return "☔"
        if "雪" in text: return "❄️"
        if "曇" in text: return "☁️"
        return "❓"

    async def get_news(self, limit=3):
        """Yahoo!ニュースのRSSからタイトルとURLを取得"""
        url = "https://news.yahoo.co.jp/rss/topics/top-picks.xml"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/91.0.4472.124"
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        xml_data = await response.text()
                        root = ET.fromstring(xml_data)
                        news_list = []
                        items = root.findall(".//item")
                        for item in items[:limit]:
                            title_el = item.find("title")
                            link_el = item.find("link")
                            title = title_el.text if title_el is not None else "無題"
                            link = link_el.text if link_el is not None else "#"
                            news_list.append(f"{title}\n{link}")
                        return news_list
        except Exception as e:
            logging.error(f"News Fetch Error: {e}")
        return []
