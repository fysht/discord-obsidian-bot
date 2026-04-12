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
        """気象庁APIから詳細な時系列予報を取得"""
        # 岡山県 (330000)
        url = "https://www.jma.go.jp/bosai/forecast/data/forecast/330000.json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        forecast = data[0]
                        
                        # 岡山南部 (areas[0])
                        # 天気コードとテキスト
                        ts0 = forecast["timeSeries"][0]
                        area0 = ts0["areas"][0]
                        codes = area0.get("weatherCodes", [])
                        weathers = area0.get("weathers", [])
                        
                        # 降水確率 (6時間ごと)
                        ts1 = forecast["timeSeries"][1]
                        pops = ts1["areas"][0].get("pops", [])
                        times_pop = ts1.get("timeDefines", [])
                        
                        # 気温
                        ts2 = forecast["timeSeries"][2]
                        temps = ts2["areas"][0].get("temps", [])
                        
                        # フロントエンドで使いやすいように整理
                        slots = []
                        # 今日・明日の情報を抽出
                        for i in range(min(len(pops), 6)):
                            t_str = times_pop[i] # ISO format
                            from datetime import datetime
                            dt = datetime.fromisoformat(t_str)
                            
                            # 天気アイコンのマッピング
                            code = codes[0] if i < 4 else codes[1] if len(codes)>1 else codes[0]
                            icon = self._get_weather_icon(code)
                            
                            slots.append({
                                "time": dt.strftime("%H:%M"),
                                "icon": icon,
                                "pop": f"{pops[i]}%",
                                "temp": temps[i] if i < len(temps) else "--"
                            })
                        
                        # 概要テキスト
                        summary = f"{weathers[0]} (現在の気温: {temps[0]}℃)" if temps else weathers[0]
                        
                        return {
                            "summary": summary,
                            "slots": slots,
                            "max_temp": temps[1] if len(temps) > 1 else "--",
                            "min_temp": temps[0] if len(temps) > 0 else "--"
                        }
                        
                    return {"summary": "取得失敗"}
        except Exception as e:
            logging.error(f"JMA Weather Fetch Error: {e}")
            return {"summary": "取得失敗"}

    def _get_weather_icon(self, code):
        """JMA天気コードを絵文字に変換"""
        code = str(code)
        if code.startswith('1'): return "☀️"
        if code.startswith('2'): return "☁️"
        if code.startswith('3'): return "☔"
        if code.startswith('4'): return "❄️"
        return "❓"

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
