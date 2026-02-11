import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import json
from datetime import datetime, time
import zoneinfo
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from typing import Optional, List

try:
    from utils.obsidian_utils import update_frontmatter
except ImportError:
    logging.warning("NewsCog: utils.obsidian_utils not found. update_frontmatter disabled.")
    def update_frontmatter(content, updates): return content

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
JMA_AREA_CODE = "330000"
WEATHER_EMOJI_MAP = {"晴": "☀️", "曇": "☁️", "雨": "☔️", "雪": "❄️", "雷": "⚡️", "霧": "🌫️"}
BASE_PATH = os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')
CUSTOM_MESSAGES_PATH = f"{BASE_PATH}/.bot/custom_daily_messages.json"

class NewsCog(commands.Cog):
    """朝6時に天気・ニュース・株価を収集し、PartnerCogに通知を依頼するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.location_name = os.getenv("LOCATION_NAME", "岡山")
        self.jma_area_name = os.getenv("JMA_AREA_NAME", "南部")
        
        self.dbx = dropbox.Dropbox(
            oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
            app_key=os.getenv("DROPBOX_APP_KEY"),
            app_secret=os.getenv("DROPBOX_APP_SECRET")
        )
        self.briefing_lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_ready(self):
        target_time = time(hour=6, minute=0, tzinfo=JST)
        if not self.morning_data_collection.is_running():
            self.morning_data_collection.change_interval(time=target_time)
            self.morning_data_collection.start()
            logging.info(f"NewsCog: 朝のデータ収集タスクを {target_time} (JST) にスケジュールしました。")

    def cog_unload(self):
        self.morning_data_collection.cancel()

    async def _get_news(self) -> str:
        url = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    text = await resp.text()
                    root = ET.fromstring(text)
                    items = root.findall('.//item')[:3]
                    return "\n".join([f"- {item.find('title').text}" for item in items])
        except Exception as e:
            logging.error(f"ニュース取得エラー: {e}")
            return "ニュースの取得に失敗しました。"

    async def _get_stocks(self) -> str:
        symbols = {"日経平均": "^N225", "S&P500": "^GSPC"}
        stock_texts = []
        try:
            async with aiohttp.ClientSession() as session:
                for name, symbol in symbols.items():
                    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                    async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'}) as resp:
                        data = await resp.json()
                        price = data['chart']['result'][0]['meta']['regularMarketPrice']
                        stock_texts.append(f"- {name}: {price:,.2f}")
            return "\n".join(stock_texts)
        except Exception as e:
            logging.error(f"株価取得エラー: {e}")
            return "株価データの取得に失敗しました。"

    async def _get_jma_weather_forecast(self) -> tuple[str, dict]:
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{JMA_AREA_CODE}.json"
        property_updates = {}
        weather_text = ""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    data = await response.json()
                
                area_weather = next((a for a in data[0]["timeSeries"][0]["areas"] if a["area"]["name"] == self.jma_area_name), None)
                if area_weather:
                    summary = area_weather["weathers"][0].replace('\u3000', ' ')
                    emoji = "❓"
                    for key, e in WEATHER_EMOJI_MAP.items():
                        if key in summary: emoji = e; break
                    property_updates['weather'] = f"{emoji} {summary}"
                    weather_text += f"{emoji} {summary}\n"

                area_temps = next((a for a in data[0]["timeSeries"][2]["areas"] if a["area"]["name"] == self.location_name), None)
                if area_temps and "temps" in area_temps:
                    valid_temps = [float(t) for t in area_temps["temps"] if t and t != "--"]
                    if valid_temps:
                        max_temp, min_temp = max(valid_temps), min(valid_temps)
                        property_updates['max_temp'] = int(max_temp)
                        property_updates['min_temp'] = int(min_temp)
                        weather_text += f"最高 {int(max_temp)}℃ / 最低 {int(min_temp)}℃"
            
            return weather_text, property_updates
        except Exception as e:
            logging.error(f"天気取得エラー: {e}")
            return "天気情報の取得に失敗しました。", {}

    async def _save_weather_to_obsidian(self, updates: dict):
        if not updates: return
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        daily_note_path = f"{BASE_PATH}/DailyNotes/{today_str}.md"
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                content = res.content.decode('utf-8')
            except ApiError:
                content = f"# Daily Note {today_str}\n"

            new_content = update_frontmatter(content, updates)
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"Obsidianに天気情報を保存しました: {daily_note_path}")
        except Exception as e:
            logging.error(f"Obsidian天気保存エラー: {e}")

    async def _get_custom_messages(self) -> List[str]:
        try:
            _, res = self.dbx.files_download(CUSTOM_MESSAGES_PATH)
            return json.loads(res.content.decode('utf-8'))
        except:
            return []

    @tasks.loop()
    async def morning_data_collection(self):
        if self.briefing_lock.locked(): return

        async with self.briefing_lock:
            # 1. データの収集とObsidian保存
            weather_text, weather_updates = await self._get_jma_weather_forecast()
            news_text = await self._get_news()
            stock_text = await self._get_stocks()
            custom_messages = await self._get_custom_messages()
            custom_msg_text = "\n".join([f"- {m}" for m in custom_messages]) if custom_messages else "特になし"

            await self._save_weather_to_obsidian(weather_updates)

            # 2. PartnerCogに送信を依頼するデータセットを作成
            context_data = (
                f"【今日の天気 ({self.location_name})】\n{weather_text}\n\n"
                f"【今日の主要ニュース】\n{news_text}\n\n"
                f"【昨晩の株価】\n{stock_text}\n\n"
                f"【今日の予定・リマインド】\n{custom_msg_text}"
            )
            
            instruction = (
                "「おはようございます！」から始まる朝のメッセージを作成してください。"
                "ニュースや株価に対して簡単な一言コメントを添え、今日も一日頑張ろうと思えるようなポジティブな励ましを入れてください。"
            )

            # 3. PartnerCogを呼び出す
            partner_cog = self.bot.get_cog("PartnerCog")
            if partner_cog:
                await partner_cog.generate_and_send_routine_message(context_data, instruction)
            else:
                logging.error("NewsCog: PartnerCogが見つからないため、通知を送信できませんでした。")

async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))