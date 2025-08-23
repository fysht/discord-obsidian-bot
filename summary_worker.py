import os
import sys
import datetime
import zoneinfo
from pathlib import Path
from dotenv import load_dotenv
import google.generativeai as genai
import dropbox
from dropbox.exceptions import ApiError

# 標準出力のエンコーディングをUTF-8に設定
sys.stdout.reconfigure(encoding='utf-8')

# --- 設定読み込み ---
load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

def generate_summary(date_str: str):
    """指定された日付のサマリーを生成し、結果を標準出力に返す"""
    if not GEMINI_API_KEY:
        print("ERROR: Gemini APIキーが設定されていません。")
        return
    if not DROPBOX_ACCESS_TOKEN:
        print("ERROR: Dropboxのアクセストークンが設定されていません。")
        return

    try:
        # Dropboxから今日のデイリーノートをダウンロード
        file_path = f"{DROPBOX_VAULT_PATH}/DailyNotes/{date_str}.md"
        notes = ""
        
        dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
        try:
            _, res = dbx.files_download(file_path)
            notes = res.content.decode('utf-8').strip()
        except ApiError as e:
            if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                print("NO_MEMO_TODAY") # ファイルが存在しない場合はメモなし
                return
            else:
                print(f"ERROR: Dropboxからのファイルダウンロードに失敗しました: {e}")
                return
        
        if not notes:
            print("NO_MEMO_TODAY")
            return

        # AIモデルの設定
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        prompt = f"""
        あなたは私の成長をサポートするコーチです。
        以下の断片的なメモ群から、今日一日の経験から学びや気づきを抽出し、自己理解を深めるためのサマリーを作成してください。
        私の思考や感情を読み取り、以下の観点で整理してください。
        - 今日のハイライト
        - 新しい学び・気づき
        - 感情の動き
        - 課題と改善点
        - 明日試してみたいこと

# 本日のメモ
{notes}
"""
        response = model.generate_content(prompt)
        summary_text = response.text.strip()

        # サマリーをDropboxに追記
        summary_to_append = f"\n\n## 本日のサマリー\n{summary_text}"
        new_content = notes + summary_to_append
        
        dbx.files_upload(
            new_content.encode('utf-8'),
            file_path,
            mode=dropbox.files.WriteMode('overwrite')
        )

        print(summary_text)

    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_date_str = sys.argv[1]
        generate_summary(target_date_str)
    else:
        jst = zoneinfo.ZoneInfo("Asia/Tokyo")
        today_str = datetime.datetime.now(jst).date().isoformat()
        generate_summary(today_str)