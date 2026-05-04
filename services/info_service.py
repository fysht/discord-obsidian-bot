import aiohttp
import xml.etree.ElementTree as ET
import logging
import re
import os
import datetime
from config import JST

YAHOO_WEATHER_LOCATIONS = {
    "33/6710": "岡山（南部）",
    "33/6720": "岡山（北部）",
    "27/6200": "大阪",
    "13/4410": "東京",
    "14/4610": "横浜",
    "23/5110": "名古屋",
    "26/6100": "京都",
    "34/6710": "広島",
    "28/6500": "神戸",
    "40/6100": "福岡",
}

# 都道府県→地域の階層構造
YAHOO_WEATHER_BY_PREFECTURE = {
    "北海道": [
        {"code": "1/1400", "name": "札幌"},
        {"code": "1/1700", "name": "旭川"},
        {"code": "1/2400", "name": "函館"},
    ],
    "宮城県": [
        {"code": "4/3410", "name": "仙台"},
    ],
    "東京都": [
        {"code": "13/4410", "name": "東京"},
        {"code": "13/4420", "name": "伊豆諸島"},
    ],
    "神奈川県": [
        {"code": "14/4610", "name": "横浜"},
        {"code": "14/4620", "name": "小田原"},
    ],
    "千葉県": [
        {"code": "12/4510", "name": "千葉"},
    ],
    "埼玉県": [
        {"code": "11/4310", "name": "さいたま"},
    ],
    "愛知県": [
        {"code": "23/5110", "name": "名古屋"},
        {"code": "23/5120", "name": "豊橋"},
    ],
    "大阪府": [
        {"code": "27/6200", "name": "大阪"},
    ],
    "京都府": [
        {"code": "26/6100", "name": "京都（南部）"},
        {"code": "26/6120", "name": "京都（北部）"},
    ],
    "兵庫県": [
        {"code": "28/6300", "name": "神戸"},
        {"code": "28/6320", "name": "豊岡"},
    ],
    "岡山県": [
        {"code": "33/6710", "name": "岡山（南部）"},
        {"code": "33/6720", "name": "岡山（北部）"},
    ],
    "広島県": [
        {"code": "34/6710", "name": "広島（南部）"},
        {"code": "34/6720", "name": "広島（北部）"},
    ],
    "福岡県": [
        {"code": "40/6100", "name": "福岡"},
        {"code": "40/6110", "name": "北九州"},
    ],
    "沖縄県": [
        {"code": "47/9110", "name": "那覇"},
    ],
}

class InfoService:
    def __init__(self):
        self.weather_location = os.getenv("WEATHER_LOCATION", "33/6710")

    async def get_weather(self, location=None):
        """Yahoo!天気から天気予報を取得（日別+時間別分離）"""
        if location is None:
            location = self.weather_location

        try:
            result = await self._fetch_yahoo_weather(location)
            if result and result.get("summary") not in ("取得失敗", None):
                return result
        except Exception as e:
            logging.warning(f"Yahoo Weather fetch failed: {e}")

        # JMAフォールバックは岡山専用（33/6710 or 33/6720）
        if location and location.startswith("33/"):
            return await self._fetch_jma_weather()

        return {"summary": "取得失敗", "daily": [], "hourly": [], "slots": [], "max_temp": "--", "min_temp": "--"}

    async def _fetch_yahoo_weather(self, location: str):
        url = f"https://weather.yahoo.co.jp/weather/jp/{location}/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; Pixel 3) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
            "Referer": "https://weather.yahoo.co.jp/",
        }

        now = datetime.datetime.now(JST)
        today_date = now.date()

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logging.warning(f"Yahoo Weather HTTP {resp.status} for {url}")
                    return None
                html = await resp.text()

        try:
            import lxml.html
            doc = lxml.html.fromstring(html)
        except Exception as e:
            logging.warning(f"lxml parse failed: {e}")
            return None

        daily = self._extract_yahoo_daily(doc, today_date)
        hourly = self._extract_yahoo_hourly(doc, today_date, now)

        if not daily:
            # 完全にデータが取れなかった場合
            return None

        location_name = YAHOO_WEATHER_LOCATIONS.get(location, location)
        summary = daily[0]["weather"] if daily else "不明"
        max_t = daily[0].get("max_temp", "--")
        min_t = daily[0].get("min_temp", "--")

        return {
            "summary": summary,
            "daily": daily,
            "hourly": hourly,
            "slots": hourly,  # 後方互換
            "max_temp": max_t,
            "min_temp": min_t,
            "location": location,
            "location_name": location_name,
        }

    def _extract_yahoo_daily(self, doc, today_date):
        """Yahoo!天気ページから日別予報を抽出"""
        daily = []

        # アプローチ1: yjSt / forecastWrap 系のセレクタ
        day_nodes = doc.cssselect('.forecastWrap > div, .forecastCity_forecastItems > li, ul.forecast > li, .days > li, .yjSt_forecast > li')
        if not day_nodes:
            # アプローチ2: table ベース
            day_nodes = doc.cssselect('table.yjw_table2 tr')

        # アプローチ3: 天気テキストとtemperatureを別途抽出（フォールバック）
        if not day_nodes:
            return self._extract_yahoo_daily_fallback(doc, today_date)

        for i, node in enumerate(day_nodes[:3]):
            text = node.text_content()
            weather_text = self._extract_weather_text_from_node(node) or "不明"
            max_t, min_t = self._extract_temps_from_node(node, text)

            # 降水確率を抽出（ページ内 N% パターンから）
            pop = "--"
            pop_match = re.search(r'(\d+)\s*%', text)
            if pop_match:
                pop = f"{pop_match.group(1)}%"

            if i == 0:
                day_label = "今日"
            elif i == 1:
                day_label = "明日"
            else:
                d = today_date + datetime.timedelta(days=i)
                day_label = f"{d.month}/{d.day}"

            daily.append({
                "day": day_label,
                "date": (today_date + datetime.timedelta(days=i)).strftime("%Y-%m-%d"),
                "weather": weather_text,
                "icon": self._get_weather_icon_by_text(weather_text),
                "max_temp": max_t,
                "min_temp": min_t,
                "pop": pop,
            })

        return daily if daily else None

    def _extract_yahoo_daily_fallback(self, doc, today_date):
        """フォールバック: 全ページテキストから日別予報を正規表現で抽出"""
        full_text = doc.text_content()

        # 最高気温と最低気温のペアを探す
        temp_pairs = re.findall(r'(\d+)℃\s*/\s*(\d+)℃', full_text)
        weather_texts = []
        for img in doc.cssselect('img[alt]'):
            alt = img.get('alt', '')
            if alt and len(alt) < 30 and any(k in alt for k in ['晴', '曇', 'くもり', '雨', '雪', '雷', 'はれ']):
                weather_texts.append(alt)

        daily = []
        for i in range(min(3, max(len(temp_pairs), 1))):
            if i == 0:
                day_label = "今日"
            elif i == 1:
                day_label = "明日"
            else:
                d = today_date + datetime.timedelta(days=i)
                day_label = f"{d.month}/{d.day}"

            max_t = temp_pairs[i][0] if i < len(temp_pairs) else "--"
            min_t = temp_pairs[i][1] if i < len(temp_pairs) else "--"
            weather = weather_texts[i] if i < len(weather_texts) else "不明"

            daily.append({
                "day": day_label,
                "date": (today_date + datetime.timedelta(days=i)).strftime("%Y-%m-%d"),
                "weather": weather,
                "icon": self._get_weather_icon_by_text(weather),
                "max_temp": max_t,
                "min_temp": min_t,
            })

        return daily if daily else None

    def _extract_yahoo_hourly(self, doc, today_date, now):
        """Yahoo!天気ページから時間別予報を抽出"""
        hourly = []

        # 時間別予報テーブルを探す
        rows = doc.cssselect('table.yjw_table tr, .hourlyForecast tr, .forecastHour tr, .yjSt_hour tr')
        if not rows:
            # フォールバック: 時間+気温パターンを全文から探す
            return self._extract_hourly_fallback(doc, today_date, now)

        for row in rows:
            cells = row.cssselect('td, th')
            if len(cells) < 2:
                continue
            texts = [c.text_content().strip() for c in cells]

            time_match = re.search(r'(\d{1,2})時', texts[0]) if texts else None
            if not time_match:
                continue

            hour = int(time_match.group(1))
            dt = datetime.datetime(today_date.year, today_date.month, today_date.day, hour, tzinfo=JST)
            if dt <= now:
                continue

            temp = "--"
            for t in texts[1:]:
                m = re.search(r'(-?\d+)', t)
                if m:
                    temp = m.group(1)
                    break

            pop = "--"
            for t in texts:
                m = re.search(r'(\d+)%', t)
                if m:
                    pop = f"{m.group(1)}%"
                    break

            weather_text = "不明"
            for cell in cells:
                for img in cell.cssselect('img[alt]'):
                    alt = img.get('alt', '')
                    if alt and len(alt) < 30:
                        weather_text = alt
                        break

            day_label = "今日" if dt.date() == today_date else "明日"
            hourly.append({
                "time": f"{hour}時",
                "day": day_label,
                "icon": self._get_weather_icon_by_text(weather_text),
                "pop": pop,
                "temp": temp,
                "weather": self._shorten_weather(weather_text),
            })

        return hourly

    def _extract_hourly_fallback(self, doc, today_date, now):
        """フォールバック: 時間別データを正規表現で抽出"""
        full_text = doc.text_content()
        # "N時 X℃" パターンを探す
        matches = re.findall(r'(\d{1,2})時\D{0,20}(-?\d+)℃', full_text)
        hourly = []
        for hour_str, temp_str in matches[:12]:
            hour = int(hour_str)
            dt = datetime.datetime(today_date.year, today_date.month, today_date.day, hour, tzinfo=JST)
            if dt <= now:
                continue
            hourly.append({
                "time": f"{hour}時",
                "day": "今日",
                "icon": "🌤️",
                "pop": "--",
                "temp": temp_str,
                "weather": "不明",
            })
        return hourly

    def _extract_weather_text_from_node(self, node):
        for img in node.cssselect('img[alt]'):
            alt = img.get('alt', '')
            if alt and len(alt) < 30 and any(k in alt for k in ['晴', '曇', 'くもり', '雨', '雪', '雷', 'はれ']):
                return alt
        for el in node.cssselect('p, span, em, div'):
            t = el.text_content().strip()
            if t and len(t) < 30 and any(k in t for k in ['晴', '曇', 'くもり', '雨', '雪', '雷', 'はれ']):
                return t
        return None

    def _extract_temps_from_node(self, node, full_text):
        max_t, min_t = "--", "--"
        # パターン1: "最高 X℃ / 最低 Y℃"
        m = re.search(r'(\d+)℃\D{0,5}/\D{0,5}(\d+)℃', full_text)
        if m:
            return m.group(1), m.group(2)
        # パターン2: class名ベース
        for cls in ['.temp-max', '.yjw_temp_max', '.high', '.hightemp']:
            el = node.cssselect(cls)
            if el:
                t = re.search(r'-?\d+', el[0].text_content())
                if t:
                    max_t = t.group()
                    break
        for cls in ['.temp-min', '.yjw_temp_min', '.low', '.lowtemp']:
            el = node.cssselect(cls)
            if el:
                t = re.search(r'-?\d+', el[0].text_content())
                if t:
                    min_t = t.group()
                    break
        # パターン3: 全テキストから数値2つ取り出す
        if max_t == "--" or min_t == "--":
            nums = re.findall(r'-?\d+', re.sub(r'\d+%', '', full_text))
            temp_nums = [n for n in nums if -30 <= int(n) <= 50]
            if len(temp_nums) >= 2:
                max_t = temp_nums[0]
                min_t = temp_nums[1]
        return max_t, min_t

    async def _fetch_jma_weather(self):
        """気象庁API（フォールバック）から天気予報を取得"""
        url = "https://www.jma.go.jp/bosai/forecast/data/forecast/330000.json"
        headers = {"User-Agent": "Mozilla/5.0"}
        now = datetime.datetime.now(JST)
        today_date = now.date()

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return {"summary": "取得失敗 (Server Error)"}
                    data = await resp.json()

            forecast = data[0]
            area0 = forecast["timeSeries"][0]["areas"][0]
            weathers = area0.get("weathers", ["不明"])
            weather_times = forecast["timeSeries"][0].get("timeDefines", [])

            pops = forecast["timeSeries"][1]["areas"][0].get("pops", [])
            times_pop = forecast["timeSeries"][1].get("timeDefines", [])
            temps_raw = forecast["timeSeries"][2]["areas"][0].get("temps", [])

            # 気象庁APIの気温構造: temps[0]=今日最低, temps[1]=今日最高, temps[2]=明日最低, temps[3]=明日最高
            def get_temp(idx):
                v = temps_raw[idx] if idx < len(temps_raw) else ""
                return v if v else "--"

            weather_by_dt = []
            for i, t in enumerate(weather_times):
                if i < len(weathers):
                    weather_by_dt.append((datetime.datetime.fromisoformat(t), weathers[i]))

            def get_weather_text_for(dt):
                best = weathers[0]
                for wdt, wtxt in weather_by_dt:
                    if dt >= wdt:
                        best = wtxt
                return best

            # 日別サマリー（今日、明日、明後日）
            daily = []
            for i in range(min(len(weathers), 3)):
                d = today_date + datetime.timedelta(days=i)
                if i == 0:
                    day_label = "今日"
                elif i == 1:
                    day_label = "明日"
                else:
                    day_label = f"{d.month}/{d.day}"
                w = weathers[i] if i < len(weathers) else weathers[-1]
                daily.append({
                    "day": day_label,
                    "date": d.strftime("%Y-%m-%d"),
                    "weather": self._shorten_weather(w),
                    "icon": self._get_weather_icon_by_text(w),
                    "max_temp": get_temp(i * 2 + 1),
                    "min_temp": get_temp(i * 2),
                })

            # 時間別
            hourly = []
            for i in range(len(pops)):
                if i >= len(times_pop):
                    break
                dt = datetime.datetime.fromisoformat(times_pop[i])
                if i + 1 < len(times_pop):
                    next_dt = datetime.datetime.fromisoformat(times_pop[i + 1])
                else:
                    next_dt = dt + datetime.timedelta(hours=6)
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
                temp_idx = i
                hourly.append({
                    "time": dt.strftime("%H時"),
                    "day": day_label,
                    "icon": self._get_weather_icon_by_text(weather_text),
                    "pop": f"{pops[i]}%",
                    "temp": temps_raw[temp_idx] if temp_idx < len(temps_raw) and temps_raw[temp_idx] else "--",
                    "weather": self._shorten_weather(weather_text),
                })

            summary = weathers[0]
            return {
                "summary": summary,
                "daily": daily,
                "hourly": hourly,
                "slots": hourly,
                "max_temp": get_temp(1),
                "min_temp": get_temp(0),
                "location": "33/6710",
                "location_name": "岡山（南部）※気象庁",
            }
        except Exception as e:
            logging.error(f"JMA Weather Error: {e}")
            return {"summary": "取得失敗 (JSON Error)"}

    def _get_weather_icon_by_text(self, text):
        if not text:
            return "🌤️"
        if "雨" in text or "あめ" in text:
            return "🌧️"
        if "雪" in text or "ゆき" in text:
            return "❄️"
        if "雷" in text or "かみなり" in text:
            return "⛈️"
        if "晴" in text or "はれ" in text:
            if "くもり" in text or "曇" in text:
                return "⛅"
            return "☀️"
        if "くもり" in text or "曇" in text:
            return "☁️"
        return "🌤️"

    def _shorten_weather(self, text):
        if not text:
            return "不明"
        text = text.replace("　", " ").strip()
        text = text.replace(" 時々 ", "時々").replace(" 後 ", "後").replace(" 一時 ", "一時")
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
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
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
                            news_list.append({"title": title, "link": link})
                        return news_list
        except Exception as e:
            logging.error(f"News Fetch Error: {e}")
        return []
