import os
import aiohttp
import asyncio
from typing import List, Dict, Any
import logging

# --- 環境変数からAPI情報を読み込み ---
API_KEY = os.getenv("GOOGLE_API_KEY")
SEARCH_ENGINE_ID = os.getenv("GOOGLE_SEARCH_ENGINE_ID")

class SearchResult:
    def __init__(self, title: str, link: str, snippet: str):
        self.source_title = title
        self.url = link
        self.description = snippet

class SearchResults:
    def __init__(self, items: List[Dict[str, Any]]):
        self.results = [SearchResult(item.get('title'), item.get('link'), item.get('snippet')) for item in items]

async def _perform_search(session: aiohttp.ClientSession, query: str) -> Dict[str, Any]:
    """非同期でGoogle Custom Search APIを呼び出す"""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        'key': API_KEY,
        'cx': SEARCH_ENGINE_ID,
        'q': query,
        'num': 5 # 検索結果の数
    }
    try:
        async with session.get(url, params=params) as response:
            # ステータスコードが200番台でない場合もエラーとして扱う
            response.raise_for_status()
            return await response.json()
    except Exception as e:
        # エラー内容をログに出力する
        logging.error(f"Google Custom Search APIの呼び出し中にエラーが発生しました: {e}")
        return {"items": []}

async def search(queries: List[str]) -> List[SearchResults]:
    """非同期で検索を実行し、結果を返す"""
    if not all([API_KEY, SEARCH_ENGINE_ID]):
        logging.warning("警告: GOOGLE_API_KEYまたはGOOGLE_SEARCH_ENGINE_IDが設定されていません。")
        return []

    async with aiohttp.ClientSession() as session:
        tasks = [_perform_search(session, q) for q in queries]
        results = await asyncio.gather(*tasks)
        return [SearchResults(res.get("items", [])) for res in results]