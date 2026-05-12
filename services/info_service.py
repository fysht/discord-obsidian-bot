import aiohttp
import xml.etree.ElementTree as ET
import logging
import re
import os
import datetime
from config import JST

YAHOO_WEATHER_LOCATIONS = {
    "33/6610": "岡山（南部）",
    "33/6620": "岡山（北部）",
}

# 利用可能な天気地点の一覧（岡山 北部/南部のみ）
YAHOO_WEATHER_REGIONS = [
    {"code": "33/6610", "name": "岡山（南部）"},
    {"code": "33/6620", "name": "岡山（北部）"},
]

class InfoService:
    def __init__(self):
        env_loc = os.getenv("WEATHER_LOCATION", "33/6610")
        # 旧コード（6710/6720）が環境変数に残っていた場合は新コードへ自動置換
        if env_loc == "33/6710":
            env_loc = "33/6610"
        elif env_loc == "33/6720":
            env_loc = "33/6620"
        self.weather_location = env_loc

    async def get_weather(self, location=None):
        """Yahoo!天気から天気予報を取得（日別+時間別分離）"""
        if location is None:
            location = self.weather_location
        # 旧コードを新コードへ自動置換
        if location == "33/6710":
            location = "33/6610"
        elif location == "33/6720":
            location = "33/6620"

        result = None
        try:
            result = await self._fetch_yahoo_weather(location)
        except Exception as e:
            logging.warning(f"Yahoo Weather fetch failed: {e}")
            result = None

        # Yahoo の hourly が空なら JMA から補完する
        if result and result.get("summary") not in ("取得失敗", None):
            if not result.get("hourly") and location and location.startswith("33/"):
                try:
                    jma = await self._fetch_jma_weather()
                    if jma and jma.get("hourly"):
                        result["hourly"] = jma["hourly"]
                        result["slots"] = jma["hourly"]
                        logging.info("Yahoo hourly が空のため JMA から補完しました")
                except Exception as e:
                    logging.debug(f"JMA hourly 補完失敗: {e}")
            return result

        # JMAフォールバックは岡山専用
        if location and location.startswith("33/"):
            return await self._fetch_jma_weather()

        return {"summary": "取得失敗", "daily": [], "hourly": [], "slots": [], "max_temp": "--", "min_temp": "--"}

    async def _fetch_yahoo_weather(self, location: str):
        # Yahoo!天気の正しい URL は .html 付き形式
        # Desktop User-Agent で .forecastCity と #yjw_week が両方取れる
        url = f"https://weather.yahoo.co.jp/weather/jp/{location}.html"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
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
        """Yahoo!天気ページから日別予報を抽出。
        今日・明日は .forecastCity から、3日目以降は #yjw_week テーブルから取得。"""
        daily = []

        # アプローチ1: forecastCity の table の各 td が 1 日分（今日・明日）
        day_nodes = doc.cssselect('.forecastCity > table > tr > td')

        if day_nodes:
            for i, node in enumerate(day_nodes[:2]):
                text = node.text_content()
                weather_text = self._extract_weather_text_from_node(node) or "不明"
                max_t, min_t = self._extract_temps_from_node(node, text)
                # 降水確率（precip 要素から抽出）
                pop = "--"
                precip_el = node.cssselect('.precip')
                if precip_el:
                    pop_match = re.search(r'(\d+)', precip_el[0].text_content())
                    if pop_match:
                        pop = f"{pop_match.group(1)}%"
                day_label = "今日" if i == 0 else "明日"
                daily.append({
                    "day": day_label,
                    "date": (today_date + datetime.timedelta(days=i)).strftime("%Y-%m-%d"),
                    "weather": self._shorten_weather(weather_text),
                    "icon": self._get_weather_icon_by_text(weather_text),
                    "max_temp": max_t,
                    "min_temp": min_t,
                    "pop": pop,
                })

        # 週間予報テーブル #yjw_week から 3日目以降を補完
        week_table = doc.cssselect('#yjw_week')
        if week_table:
            week_rows = week_table[0].cssselect('tr')
            # 4行構造: [日付, 天気, 気温, 降水確率]
            if len(week_rows) >= 4:
                date_cells = week_rows[0].cssselect('td')[1:]      # ヘッダ td を除く
                weather_cells = week_rows[1].cssselect('td')[1:]
                temp_cells = week_rows[2].cssselect('td')[1:]
                pop_cells = week_rows[3].cssselect('td')[1:]
                # 既に取れている日数分を skip。最大 5 日分まで。
                start_offset = len(daily)
                for j, dcell in enumerate(date_cells[: 5 - start_offset]):
                    i = start_offset + j
                    d = today_date + datetime.timedelta(days=i)
                    weather = weather_cells[j].text_content().strip() if j < len(weather_cells) else "不明"
                    # 気温セル: "26 13" のように max min が空白区切り
                    temp_txt = temp_cells[j].text_content().strip() if j < len(temp_cells) else ""
                    nums = re.findall(r'-?\d+', temp_txt)
                    if len(nums) >= 2:
                        a, b = nums[0], nums[1]
                        try:
                            max_t = a if int(a) >= int(b) else b
                            min_t = b if int(a) >= int(b) else a
                        except ValueError:
                            max_t, min_t = a, b
                    elif len(nums) == 1:
                        max_t, min_t = nums[0], "--"
                    else:
                        max_t, min_t = "--", "--"
                    pop_txt = pop_cells[j].text_content().strip() if j < len(pop_cells) else ""
                    pop_match = re.search(r'\d+', pop_txt)
                    pop = f"{pop_match.group()}%" if pop_match else "--"
                    daily.append({
                        "day": f"{d.month}/{d.day}",
                        "date": d.strftime("%Y-%m-%d"),
                        "weather": self._shorten_weather(weather),
                        "icon": self._get_weather_icon_by_text(weather),
                        "max_temp": max_t,
                        "min_temp": min_t,
                        "pop": pop,
                    })

        if daily:
            return daily

        # 全アプローチ失敗時のフォールバック
        return self._extract_yahoo_daily_fallback(doc, today_date)

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
        """Yahoo!天気ページから時間別予報を抽出。
        【修正点】
        - 過去時刻の厳密フィルタを撤廃（現在時刻の手前 1 時間まで許容）。深夜アクセス時に空になる問題への対応。
        - CSS セレクタを拡充し、新しい DOM 構造（クラス名変更）にも追従。
        - 1 件も取れなかった場合は正規表現フォールバックを呼ぶ。
        """
        hourly = []
        cutoff = now - datetime.timedelta(hours=1)

        # 時間別予報テーブルを広めに探す（旧/新セレクタを併用）
        rows = doc.cssselect(
            'table.yjw_table tr, .hourlyForecast tr, .forecastHour tr, .yjSt_hour tr, '
            'table.forecast_table tr, .hourly-detail tr, .forecast-hourly tr, '
            'section[data-cy*="hourly"] tr, table tr'
        )

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
            # 24 時間未満の翌日扱い（深夜帯）
            if dt < cutoff and hour < 6:
                dt = dt + datetime.timedelta(days=1)
            # 完全に過去（前日）はスキップ。直近 1 時間以内は表示する
            if dt < cutoff:
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

        # 何も取れなかった場合は正規表現フォールバック
        if not hourly:
            hourly = self._extract_hourly_fallback(doc, today_date, now)

        return hourly

    def _extract_hourly_fallback(self, doc, today_date, now):
        """フォールバック: 時間別データを正規表現で抽出。
        Yahoo HTML 構造が変わっても全文から `N時` / `X℃` / `Y%` パターンを拾う。"""
        full_text = doc.text_content()
        cutoff = now - datetime.timedelta(hours=1)
        # "N時 ... X℃" パターン（広めに探す）
        matches = re.findall(r'(\d{1,2})時[^℃\n]{0,60}(-?\d+)℃[^%\n]{0,30}(?:(\d+)%)?', full_text)
        seen = set()
        hourly = []
        for hour_str, temp_str, pop_str in matches:
            hour = int(hour_str)
            if hour in seen:
                continue
            seen.add(hour)
            dt = datetime.datetime(today_date.year, today_date.month, today_date.day, hour, tzinfo=JST)
            if dt < cutoff and hour < 6:
                dt = dt + datetime.timedelta(days=1)
            if dt < cutoff:
                continue
            day_label = "今日" if dt.date() == today_date else "明日"
            hourly.append({
                "time": f"{hour}時",
                "day": day_label,
                "icon": "🌤️",
                "pop": f"{pop_str}%" if pop_str else "--",
                "temp": temp_str,
                "weather": "不明",
            })
            if len(hourly) >= 24:
                break
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
        """Yahoo!天気 HTML から最高/最低気温を抽出する。
        実 HTML 構造は <li class="high"><em>23</em>℃[+2] と <li class="low"><em>9</em>℃[+1]。
        各クラスの em 内の最初の数値を取るのが最も信頼できる。"""

        def _read_em_value(elements):
            for el in elements:
                em_list = el.cssselect("em")
                if em_list:
                    txt = em_list[0].text_content().strip()
                    m = re.search(r"-?\d+", txt)
                    if m:
                        return m.group()
                # フォールバック: 要素全体のテキストから最初の数値
                txt = el.text_content().strip()
                m = re.search(r"-?\d+", txt)
                if m:
                    return m.group()
            return None

        max_t = _read_em_value(node.cssselect(".high")) \
            or _read_em_value(node.cssselect(".temp-max")) \
            or _read_em_value(node.cssselect(".yjw_temp_max")) \
            or _read_em_value(node.cssselect(".hightemp"))
        min_t = _read_em_value(node.cssselect(".low")) \
            or _read_em_value(node.cssselect(".temp-min")) \
            or _read_em_value(node.cssselect(".yjw_temp_min")) \
            or _read_em_value(node.cssselect(".lowtemp"))

        if max_t and min_t:
            try:
                if int(max_t) < int(min_t):
                    max_t, min_t = min_t, max_t
            except ValueError:
                pass
            return max_t, min_t

        # CSS で取れなかった場合のテキスト解析フォールバック
        m = re.search(r"(-?\d+)\s*[℃°][^\d]{0,12}[\/／・~〜\-ー][^\d]{0,12}(-?\d+)\s*[℃°]", full_text)
        if m:
            a, b = m.group(1), m.group(2)
            try:
                if int(a) >= int(b):
                    return a, b
                return b, a
            except ValueError:
                return a, b

        return "--", "--"

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

            # 時間別: 過去スロットも次スロットがまだ将来なら表示（深夜帯対策）
            hourly = []
            cutoff_jma = now - datetime.timedelta(hours=1)
            for i in range(len(pops)):
                if i >= len(times_pop):
                    break
                dt = datetime.datetime.fromisoformat(times_pop[i])
                if i + 1 < len(times_pop):
                    next_dt = datetime.datetime.fromisoformat(times_pop[i + 1])
                else:
                    next_dt = dt + datetime.timedelta(hours=6)
                if next_dt < cutoff_jma:
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
                "location": "33/6610",
                "location_name": "岡山（南部）",
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

    async def get_news(self, limit=5):
        """Yahoo!ニュースのRSSからタイトルとURLを取得"""
        candidate_urls = [
            "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
            "https://news.yahoo.co.jp/pickup/rss.xml",
        ]
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        for url in candidate_urls:
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
                                if title and title != "無題":
                                    news_list.append({"title": title, "link": link})
                            if news_list:
                                return news_list
            except Exception as e:
                logging.warning(f"News Fetch Error ({url}): {e}")
        return []
