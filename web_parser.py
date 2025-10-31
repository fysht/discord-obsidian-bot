import requests
from readability import Document
from markdownify import markdownify as md
import logging

def parse_url_with_readability(url: str) -> tuple[str | None, str | None]:
    """
    requestsとreadability-lxmlを使い、ページのタイトルと本文(Markdown)を抽出する。
    """
    try:
        # 1. User-Agentを指定してウェブページを取得
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()  # HTTPエラーがあれば例外を発生

        # 2. readabilityで本文を抽出
        doc = Document(response.text)
        title = doc.title()
        content_html = doc.summary()

        # 3. HTMLをMarkdownに変換
        markdown_content = md(content_html, heading_style="ATX")

        return title, markdown_content

    except requests.exceptions.RequestException as e:
        logging.error(f"URLの取得に失敗しました {url}: {e}")
        return "No Title Found", f"（URLの取得に失敗しました: {e}）"
    except Exception as e:
        logging.error(f"URLの解析中に予期せぬエラーが発生しました {url}: {e}", exc_info=True)
        return "No Title Found", f"（ページの解析中にエラーが発生しました: {e}）"