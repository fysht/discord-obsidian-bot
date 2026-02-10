import logging
from playwright.async_api import async_playwright
from readability import Document
from markdownify import markdownify as md

async def parse_url_with_readability(url: str) -> tuple[str | None, str | None]:
    """
    Playwrightを使ってブラウザ経由でページを取得し、JavaScript実行後のHTMLから
    readabilityとmarkdownifyでタイトルと本文を抽出する。
    """
    try:
        async with async_playwright() as p:
            # ブラウザ起動 (Render等のサーバー環境では sandbox 無効化が必要な場合が多い)
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            
            # コンテキスト作成 (一般的なブラウザのUser-Agentを設定)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = await context.new_page()
            
            # ページ遷移 (タイムアウト30秒)
            # wait_until='domcontentloaded' でDOMの構築完了を待つ
            try:
                await page.goto(url, timeout=30000, wait_until='domcontentloaded')
            except Exception as e:
                logging.warning(f"ページ読み込みタイムアウト（処理は続行します）: {url} - {e}")

            # 追加の待機: ネットワーク通信が落ち着くまで待つ (SPAや遅延ロード対策)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except:
                pass # タイムアウトしても、取得できた時点のHTMLで解析を進める

            # HTMLとタイトルを取得
            content_html = await page.content()
            page_title = await page.title()
            
            await browser.close()

            # readabilityで本文抽出
            doc = Document(content_html)
            title = page_title if page_title else doc.title()
            summary_html = doc.summary()

            # HTMLをMarkdownに変換
            markdown_content = md(summary_html, heading_style="ATX")

            return title, markdown_content

    except Exception as e:
        logging.error(f"PlaywrightによるURL解析エラー: {url} -> {e}", exc_info=True)
        return "No Title Found", f"（ページの解析中にエラーが発生しました: {e}）"