import aiohttp
import xml.etree.ElementTree as ET
import logging

class InfoService:
    def __init__(self):
        # 岡山県の気象庁JSONコード
        self.weather_url = "https://www.jma.go.jp/bosai/forecast/data/forecast/330000.json"

    async def get_weather(self):
        """気象庁APIから岡山の天気を取得"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.weather_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        time_series = data[0]["timeSeries"]
                        weather_text = time_series[0]["areas"][0]["weathers"][0]
                        weather_text = weather_text.replace("　", " ") # 見やすく整形
                        
                        # 気温のリストを取得
                        weekly_areas = data[1]["timeSeries"][1]["areas"][0]
                        temps_max = weekly_areas.get("tempsMax", [])
                        temps_min = weekly_areas.get("tempsMin", [])
                        
                        # ★修正: 空っぽのデータ("")をスキップして、数字が入っているものだけを抽出する
                        valid_max = [t for t in temps_max if t.strip()]
                        valid_min = [t for t in temps_min if t.strip()]
                        
                        # 一番最初に見つかった有効な数字をセット（見つからなければN/A）
                        max_t = valid_max[0] if valid_max else "N/A"
                        min_t = valid_min[0] if valid_min else "N/A"
                        
                        # ObsidianのYAMLエラーを防ぐため、全体をダブルクォーテーションで囲む
                        weather_value = f'"{weather_text} (最高: {max_t}℃ / 最低: {min_t}℃)"'
                        
                        return weather_value, max_t, min_t
        except Exception as e:
            logging.error(f"Weather Fetch Error: {e}")
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
                        for item in root.findall('.//item')[:limit]:
                            title = item.find('title').text
                            link = item.find('link').text
                            # タイトルとURLをセットにしてAIに渡す
                            news_list.append(f"{title}\n{link}")
                        return news_list
        except Exception as e:
            logging.error(f"News Fetch Error: {e}")
        return []