from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

app = FastAPI(title="Secretary AI API", docs_url=None, redoc_url=None)

# 静的ファイルの配信設定
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    """PWAのトップページを返す"""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(
            str(index_path),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return {"message": "Secretary AI API is running."}


@app.get("/sw.js")
async def service_worker():
    """Service Worker をルートスコープで配信する。
    SW ファイルは /static/js/sw.js に存在するが、ルート配信＋ Service-Worker-Allowed
    ヘッダーを付けることでアプリ全体（'/'）をスコープに含められる。
    これがないと Web Push 通知が登録できず、画面を閉じている間の通知が届かない。"""
    sw_path = static_dir / "js" / "sw.js"
    if not sw_path.exists():
        return {"error": "sw.js not found"}
    return FileResponse(
        str(sw_path),
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )
