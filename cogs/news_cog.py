import os
import logging
import datetime
import xml.etree.ElementTree as ET
import asyncio

import discord
from discord.ext import commands, tasks
import aiohttp

# --- リファクタリング: 定数のクリーンなインポート ---
from config import JST

# ※既存の動作を壊さないよう、当ファイル専用の定数として上部に定義しています。
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {"晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"}
YAHOO_NEWS_RSS_URL = "https://news.yahoo.co.jp/rss/topics/top-picks.xml"


class NewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        self.jma_area_name = os.getenv("JMA_AREA_NAME", "南部")
        
        # --- ★ 修正: 正しい変数名でカレンダーとGeminiを受け取る ---
        self.calendar_service = getattr(bot, 'calendar_service', None)
        self.gemini_client = bot.gemini_client

    async def get_weather(self):
        """気象庁から今日の天気を取得"""
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return "天気情報取得失敗"
                    
                    data = await resp.json()
                    weather = data[0]["timeSeries"][0]["areas"][0]["weathers"][0].replace("\u3000", " ")
                    return weather
        except Exception as e:
            logging.error(f"Weather Fetch Error: {e}")
            return "取得エラー"

    async def get_news(self, limit=3):
        """YahooニュースRSSからヘッドラインとURLを取得"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(YAHOO_NEWS_RSS_URL) as resp:
                    if resp.status != 200:
                        return "ニュース取得失敗"
                    
                    xml_content = await resp.text()
                    root = ET.fromstring(xml_content)
                    
                    items = root.findall(".//item")
                    news_lines = []
                    for item in items[:limit]:
                        title = item.find("title").text
                        link = item.find("link").text
                        news_lines.append(f"・{title}\n  {link}")
                    
                    return "\n".join(news_lines)
        except Exception as e:
            logging.error(f"News Fetch Error: {e}")
            return "取得エラー"

    async def get_stock_info(self):
        """簡易的な株価情報等のプレースホルダー（必要に応じて拡張）"""
        return "（株価API未設定のため取得スキップ）"

    @tasks.loop(time=datetime.time(hour=6, minute=30, tzinfo=JST))
    async def morning_routine(self):
        """毎朝6:30に起動するモーニングルーティン"""
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            logging.error("NewsCog: PartnerCogが見つかりません。")
            return

        memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        channel = self.bot.get_channel(memo_channel_id)
        if not channel:
            return

        try:
            # 各種情報を非同期で並列取得
            weather_task = asyncio.create_task(self.get_weather())
            news_task = asyncio.create_task(self.get_news(limit=3))
            stock_task = asyncio.create_task(self.get_stock_info())

            weather_text = await weather_task
            news_text = await news_task
            stock_text = await stock_task

            # --- ★ 修正: 正しい変数名でカレンダーを呼び出す ---
            today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            schedule_text = "カレンダーサービスに接続できませんでした。"
            if self.calendar_service:
                schedule_text = await self.calendar_service.list_events_for_date(today_str)

            # 今日のチャットログ（深夜0時以降）を取得して文脈に追加する
            recent_log = ""
            if channel:
                recent_log = await partner_cog.fetch_todays_chat_log(channel)

            context_data = (
                f"【今日の予定】\n{schedule_text}\n\n"
                f"【今日の天気 ({self.location_name})】\n{weather_text}\n\n"
                f"【今日の主要ニュース(概要・URL付)】\n{news_text}\n\n"
                f"【昨晩の株価】\n{stock_text}\n\n"
                f"【最近の会話ログ】\n{recent_log}"
            )
            
            instruction = (
                "「おはよう！」から始まる朝のメッセージを作成して。以下の要素を自然なタメ口で織り交ぜてね。\n"
                "1. 今日の予定を教えてあげる（予定がない場合はその旨を伝える）。\n"
                "2. 天気について軽く触れる。\n"
                "3. ニュースはURLを含めて1〜2つほど紹介する。\n"
                "全体として長すぎず、LINEのような温かい雰囲気でお願いします。"
            )

            await partner_cog.generate_and_send_routine_message(context_data, instruction)

        except Exception as e:
            logging.error(f"Morning Routine Error: {e}")

    @morning_routine.before_loop
    async def before_morning_routine(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.morning_routine.is_running():
            self.morning_routine.start()

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))