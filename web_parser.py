import asyncio
import aiohttp
from bs4 import BeautifulSoup
import logging

async def parse_url(url: str) -> tuple[str | None, str | None]:
    """
    指定されたURLからページのタイトルと本文を非同期で取得する。
    aiohttpを使用してHTTPリクエストをノンブロッキングで実行する。
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status != 200:
                    logging.error(f"Failed to fetch URL {url} with status {response.status}")
                    return None, None
                
                html = await response.text()

        # BeautifulSoupを使ってHTMLを解析
        soup = BeautifulSoup(html, 'html.parser')

        # タイトルを取得
        title = soup.title.string if soup.title else "No Title"

        # 本文を取得（主要なコンテンツが含まれていそうなタグを試す）
        # 一般的な記事サイトを想定
        if soup.find('article'):
            content = soup.find('article').get_text('\n', strip=True)
        elif soup.find('main'):
            content = soup.find('main').get_text('\n', strip=True)
        else:
            # bodyから不要な要素（ヘッダー、フッター、ナビゲーション、スクリプト等）を削除
            for element in soup(['header', 'footer', 'nav', 'aside', 'script', 'style']):
                element.decompose()
            content = soup.body.get_text('\n', strip=True)
            
        return title, content

    except Exception as e:
        logging.error(f"Error parsing URL {url}: {e}", exc_info=True)
        return None, None