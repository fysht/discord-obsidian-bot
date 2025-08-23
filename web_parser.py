import asyncio
from requests_html import HTMLSession
from markdownify import markdownify as md
import logging
import nest_asyncio

# requests-htmlが非同期ループ内で動作するために必要
nest_asyncio.apply()

def parse_url_advanced(url: str) -> tuple[str | None, str | None]:
    """
    仮想ブラウザ(requests-html)を使い、JSをレンダリングした上で
    ページのタイトルと本文(Markdown)を抽出する。
    """
    session = None
    try:
        session = HTMLSession()
        r = session.get(url, timeout=30)
        
        # JSをレンダリング（SPAなどの現代的なサイトに対応）
        # スクロールダウンと待機時間を設けることで、動的に読み込まれるコンテンツも取得しやすくする
        r.html.render(scrolldown=1, sleep=3, keep_page=True, timeout=20)
        
        # ページタイトルを取得
        title = r.html.find('title', first=True)
        title_text = title.text if title else "No Title Found"

        # 本文が含まれていそうな主要な要素を探す（<article>や<main>を優先）
        article = r.html.find('article', first=True)
        if not article:
            article = r.html.find('main', first=True)
        
        # <article>や<main>が見つかればその中身を、なければページ全体を変換対象とする
        html_to_convert = article.html if article else r.html.html

        # HTMLをMarkdownに変換
        markdown_content = md(html_to_convert, heading_style="ATX")

        if not markdown_content:
            logging.warning(f"Markdownへの変換に失敗しました: {url}")
            return title_text, None

        return title_text, markdown_content

    except Exception as e:
        logging.error(f"高度なURL解析中にエラーが発生しました {url}: {e}", exc_info=True)
        # エラーが発生した場合は、タイトルと空の本文を返すこともできるが、
        # 今回はブックマークとして保存する логиックのためNoneを返す
        return None, None
    finally:
        if session:
            session.close()