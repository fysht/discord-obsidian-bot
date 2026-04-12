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
        """気象庁APIから岡山の詳細な天気を取得"""
        # 岡山県 (330000)
        url = "https://www.jma.go.jp/bosai/forecast/data/forecast/330000.json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # data[0] が直近予報
                        forecast_day0 = data[0]
                        
                        # 岡山南部 (areas[0])
                        ts0 = forecast_day0["timeSeries"][0]
                        weather = ts0["areas"][0]["weathers"][0].replace("　", " ")
                        
                        # 降水確率 (timeSeries[1] areas[0] pops)
                        ts1 = forecast_day0["timeSeries"][1]
                        pops = ts1["areas"][0]["pops"]
                        # 降水確率の時間枠ラベル (00-06, 06-12, 12-18, 18-24 など)
                        # popsの数によって今日か明日か変わるが、基本は今日の直近
                        pop_text = " / ".join([f"{p}%" for p in pops[:4]]) # 最大4つ表示
                        
                        # 気温 (timeSeries[2] areas[0] temps)
                        ts2 = forecast_day0["timeSeries"][2]
                        temps = ts2["areas"][0]["temps"] # 今日の最低、今日の最高、明日の最低、明日の最高
                        # 注: 発表時間によって配列の意味が変わるため、簡易的に取得
                        # 通常 index 0:今日最低(or欠落), 1:今日最高, 2:明日最低, 3:明日最高
                        max_t = temps[1] if len(temps) > 1 else "--"
                        min_t = temps[0] if len(temps) > 0 else "--"
                        
                        res_str = f'"{weather} (降水:{pop_text}) 気温:{max_t}℃/{min_t}℃"'
                        return res_str, max_t, min_t
                        
                    return "取得失敗", "N/A", "N/A"
        except Exception as e:
            logging.error(f"JMA Weather Fetch Error: {e}")
            return "取得失敗", "N/A", "N/A"

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
