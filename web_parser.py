import asyncio
import aiohttp
from bs4 import BeautifulSoup
from readability import Document
import logging

async def parse_url(url: str) -> tuple[str | None, str | None]:
    """
    指定されたURLからページのタイトルと本文を非同期で取得する。
    readability-lxmlを使用して、主要なコンテンツをより正確に抽出する。
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as response:
                if not response.ok:
                    logging.error(f"URLの取得に失敗しました {url} (ステータスコード: {response.status})")
                    return None, None
                
                html = await response.text()

        doc = Document(html)
        title = doc.title()
        content_html = doc.summary()
        
        soup = BeautifulSoup(content_html, 'lxml')
        content_text = soup.get_text('\n', strip=True)
            
        if not title or not content_text:
            logging.warning(f"タイトルまたは本文の抽出に失敗しました: {url}")
            return None, None

        return title, content_text

    except asyncio.TimeoutError:
        logging.error(f"URL取得がタイムアウトしました: {url}")
        return None, None
    except Exception as e:
        logging.error(f"URLの解析中にエラーが発生しました {url}: {e}", exc_info=True)
        return None, None