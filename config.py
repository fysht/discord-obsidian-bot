import os
import zoneinfo
from pathlib import Path

# タイムゾーン設定
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# Google API関連
TOKEN_FILE = 'token.json'
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/tasks'
]

# フォルダ・ファイル設定
BOT_FOLDER = ".bot"
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))