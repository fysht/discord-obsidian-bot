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
        """気象庁APIから詳細な時系列予報を取得し、現在以降のスロットのみ抽出"""
        url = self.weather_url
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        now = datetime.datetime.now(JST)
        today_date = now.date()
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        forecast = data[0]
                        area0 = forecast["timeSeries"][0]["areas"][0]
                        weathers = area0.get("weathers", ["不明"])
                        weather_times = forecast["timeSeries"][0].get("timeDefines", [])

                        pops = forecast["timeSeries"][1]["areas"][0].get("pops", [])
                        times_pop = forecast["timeSeries"][1].get("timeDefines", [])
                        temps = forecast["timeSeries"][2]["areas"][0].get("temps", [])

                        # 天気テキストを時間帯ごとにマッピング
                        weather_by_dt = []
                        for i, t in enumerate(weather_times):
                            if i < len(weathers):
                                weather_by_dt.append((datetime.datetime.fromisoformat(t), weathers[i]))

                        def get_weather_text_for(dt):
                            """指定時刻に最も近い天気テキストを返す"""
                            best = weathers[0]
                            for wdt, wtxt in weather_by_dt:
                                if dt >= wdt:
                                    best = wtxt
                            return best

                        # 現在以降の降水確率スロットのみ抽出
                        slots = []
                        for i in range(len(pops)):
                            if i >= len(times_pop):
                                break
                            dt = datetime.datetime.fromisoformat(times_pop[i])
                            # 次のスロット開始時刻（=このスロットの終了時刻）
                            if i + 1 < len(times_pop):
                                next_dt = datetime.datetime.fromisoformat(times_pop[i + 1])
                            else:
                                next_dt = dt + datetime.timedelta(hours=6)
                            # このスロットの終了時刻が現在より前なら過去 → スキップ
                            if next_dt <= now:
                                continue

                            slot_date = dt.date()
                            if slot_date == today_date:
                                day_label = "今日"
                            elif slot_date == today_date + datetime.timedelta(days=1):
                                day_label = "明日"
                            else:
                                day_label = f"{dt.month}/{dt.day}"

                            weather_text = get_weather_text_for(dt)
                            slots.append({
                                "time": dt.strftime("%H時"),
                                "day": day_label,
                                "icon": self._get_weather_icon_by_text(weather_text),
                                "pop": f"{pops[i]}%",
                                "temp": temps[i] if i < len(temps) else "--",
                                "weather": self._shorten_weather(weather_text),
                            })

                        # サマリー（今の天気）
                        summary = weathers[0]
                        max_t = temps[1] if len(temps) > 1 else "--"
                        min_t = temps[0] if len(temps) > 0 else "--"

                        return {
                            "summary": summary,
                            "slots": slots,
                            "max_temp": max_t,
                            "min_temp": min_t,
                        }
                    return {"summary": "取得失敗 (Server Error)"}
        except Exception as e:
            logging.error(f"Weather Fetch Error: {e}")
            return {"summary": "取得失敗 (JSON Error)"}

    def _get_weather_icon_by_text(self, text):
        """天気テキストからアイコンを判定（気象庁APIはひらがな「くもり」を使用）"""
        if "雨" in text or "あめ" in text: return "🌧️"
        if "雪" in text or "ゆき" in text: return "❄️"
        if "雷" in text or "かみなり" in text: return "⛈️"
        if "晴" in text or "はれ" in text:
            if "くもり" in text or "曇" in text:
                return "⛅"  # 晴れ時々くもり
            return "☀️"
        if "くもり" in text or "曇" in text: return "☁️"
        return "🌤️"  # デフォルトはくもり晴れ

    def _shorten_weather(self, text):
        """気象庁の長い天気文を短縮する"""
        text = text.replace("　", " ").strip()
        # 「くもり　時々　晴れ」→「くもり時々晴れ」
        text = text.replace(" 時々 ", "時々").replace(" 後 ", "後").replace(" 一時 ", "一時")
        # 20文字以上なら切る
        if len(text) > 16:
            text = text[:16] + "…"
        return text

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
