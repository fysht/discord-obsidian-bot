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
]

# フォルダ・ファイル設定
BOT_FOLDER = ".bot"
# Render上で環境変数が残っている問題を避けるため、強制的にローカルパスを指定します
PENDING_MEMOS_FILE = Path("pending_memos.json")
