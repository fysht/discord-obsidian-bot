import logging
import os
import subprocess
import asyncio
from playwright.async_api import async_playwright
from readability import Document
from markdownify import markdownify as md

# --- Render対策: ブラウザのインストール先をプロジェクト内に強制する ---
# プロジェクトフォルダ内の .playwright_browsers というフォルダに保存するように設定します。
# これにより、システムのキャッシュ削除の影響を受けにくくし、
# もし削除されていても自動で再インストールできる権限を確保します。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_BROWSER_DIR = os.path.join(PROJECT_ROOT, ".playwright_browsers")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = LOCAL_BROWSER_DIR

async def _ensure_browser_installed():
    """
    Playwrightのブラウザ(Chromium)がインストールされているか確認し、
    なければその場でインストールを実行する自己修復関数。
    """
    # ディレクトリが存在しない、または中身が空の場合
    if not os.path.exists(LOCAL_BROWSER_DIR) or not os.listdir(LOCAL_BROWSER_DIR):
        logging.warning(f"Playwright browser not found in {LOCAL_BROWSER_DIR}. Installing Chromium... (This may take a moment)")
        try:
            # タイムアウトを防ぐため、別スレッドでインストールコマンドを実行
            await asyncio.to_thread(
                lambda: subprocess.run(["playwright", "install", "chromium"], check=True)
            )
            logging.info("Chromium installation completed successfully.")
        except Exception as e:
            logging.error(f"Failed to install Chromium: {e}")
            # ここで失敗しても、次の処理で詳細なエラーが出るので一旦パス
            pass

async def parse_url_with_readability(url: str) -> tuple[str | None, str | None]:
    """
    Playwrightを使ってブラウザ経由でページを取得し、JavaScript実行後のHTMLから
    readabilityとmarkdownifyでタイトルと本文を抽出する。
    """
    try:
        # 1. ブラウザの存在確認と自動インストール（初回のみ時間がかかります）
        await _ensure_browser_installed()
        
        async with async_playwright() as p:
            # ブラウザ起動
            # args指定はRender等のコンテナ環境でのクラッシュを防ぐために必要
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            
            # コンテキスト作成
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = await context.new_page()
            
            # ページ遷移 (タイムアウト30秒)
            try:
                await page.goto(url, timeout=30000, wait_until='domcontentloaded')
            except Exception as e:
                logging.warning(f"ページ読み込みタイムアウト（処理は続行します）: {url} - {e}")

            # 追加の待機: ネットワーク通信が落ち着くまで
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except:
                pass 

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