import logging
import os
import subprocess
import asyncio
from playwright.async_api import async_playwright
from readability import Document
from markdownify import markdownify as md
import aiohttp
from urllib.parse import unquote_plus
import re

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_BROWSER_DIR = os.path.join(PROJECT_ROOT, ".playwright_browsers")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = LOCAL_BROWSER_DIR


async def _ensure_browser_installed():
    """
    Playwrightのブラウザ(Chromium)がインストールされているか確認し、
    なければその場でインストールを実行する自己修復関数。
    """
    if not os.path.exists(LOCAL_BROWSER_DIR) or not os.listdir(LOCAL_BROWSER_DIR):
        logging.warning(
            f"Playwright browser not found in {LOCAL_BROWSER_DIR}. Installing Chromium... (This may take a moment)"
        )
        try:
            await asyncio.to_thread(
                lambda: subprocess.run(
                    ["playwright", "install", "chromium"], check=True
                )
            )
            logging.info("Chromium installation completed successfully.")
        except Exception as e:
            logging.error(f"Failed to install Chromium: {e}")
            pass


async def parse_url_with_readability(url: str) -> tuple[str | None, str | None]:
    """
    Playwrightを使ってブラウザ経由でページを取得し、JavaScript実行後のHTMLから
    readabilityとmarkdownifyでタイトルと本文を抽出する。
    """
    try:
        await _ensure_browser_installed()

        async with async_playwright() as p:
            # 【修正1】メモリ不足対策の引数 '--disable-dev-shm-usage' を追加
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            # 【修正2】try...finally で囲み、エラーが起きても確実にブラウザを閉じるようにする
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = await context.new_page()

                # 【修正3】ページ遷移のタイムアウトを45秒に延長
                await page.goto(url, timeout=45000, wait_until="domcontentloaded")

                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

                content_html = await page.content()
                page_title = await page.title()

            finally:
                # どんな状況（タイムアウトなど）でも確実にブラウザを閉じる
                await browser.close()

            # readabilityで本文抽出
            doc = Document(content_html)
            title = page_title if page_title else doc.title()
            summary_html = doc.summary()

            # HTMLをMarkdownに変換
            markdown_content = md(summary_html, heading_style="ATX")

            return title, markdown_content

    except Exception as e:
        logging.error(f"PlaywrightによるURL解析エラー: {url} -> {e}")
        return "No Title Found", f"（ページの解析中にエラーが発生しました: {e}）"


async def fetch_maps_info(url: str) -> tuple[str, str]:
    """Google MapsのURLを展開し、URLパスから場所名と住所をそのまま抽出する"""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            }
            # allow_redirects=True is default in aiohttp
            async with session.get(url, headers=headers, timeout=15) as response:
                final_url = str(response.url)

                # パターン: https://www.google.com/maps/place/○○○/...
                match = re.search(r"/place/([^/]+)", final_url)
                if match:
                    # URLエンコードされた文字列（例: 〒708-0032+岡山県...）をデコード
                    place_str = unquote_plus(match.group(1))

                    # 郵便番号と住所と場所名がくっついているので分割を試みる
                    # '〒708-0032 岡山県津山市伏見町２８−１ 炙家 ぼっけもん'
                    parts = place_str.split(" ")

                    if len(parts) >= 2 and parts[0].startswith("〒"):
                        address = parts[0] + " " + parts[1]
                        name = " ".join(parts[2:]) if len(parts) > 2 else parts[1]
                        return name, address
                    else:
                        return place_str, place_str

                # q= クエリパラメータの場合
                match_q = re.search(r"[?&]q=([^&]+)", final_url)
                if match_q:
                    q_str = unquote_plus(match_q.group(1))
                    return q_str, q_str

    except Exception as e:
        logging.error(f"Google Mapsの展開エラー: {url} -> {e}")

    return "Google Maps Location", ""
