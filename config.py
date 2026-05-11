import os
import zoneinfo
from pathlib import Path

# タイムゾーン設定
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# Google API関連
TOKEN_FILE = "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/gmail.modify",
]

# フォルダ・ファイル設定
BOT_FOLDER = ".bot"
PENDING_MEMOS_FILE = Path("pending_memos.json")

# タイムアウト設定 (秒) — 全モジュールで共有
TIMEOUT_HTTP_SHORT = 8
TIMEOUT_HTTP_DEFAULT = 10
TIMEOUT_HTTP_LONG = 15
TIMEOUT_PLAYWRIGHT = 45
TIMEOUT_PLAYWRIGHT_LONG = 60
TIMEOUT_SUBPROCESS = 120


def require_env(name: str) -> str:
    """必須環境変数を取得。未設定なら起動を止める。"""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"必須環境変数 {name} が未設定です。.env もしくはホスティング環境の設定を確認してください。"
        )
    return val
