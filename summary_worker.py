import os
import sys
import datetime
import zoneinfo
from dotenv import load_dotenv
import google.generativeai as genai
import dropbox
from dropbox.exceptions import ApiError

# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
# 出力先を標準出力に
sys.stdout.reconfigure(encoding='utf-8')

# --- 基本設定 ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

def generate_summary(date_str: str):
    """指定された日付のサマリーを生成し、結果を標準出力に返す"""
    if not GEMINI_API_KEY:
        print("ERROR: Gemini APIキーが設定されていません。")
        return
    if not all([DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN]):
        print("ERROR: Dropboxの認証情報が不足しています。")
        return

    try:
        with dropbox.Dropbox(
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET
        ) as dbx:
            dbx.check_user()
            
            file_path = f"{DROPBOX_VAULT_PATH}/DailyNotes/{date_str}.md"
            notes = ""
            
            try:
                _, res = dbx.files_download(file_path)
                notes = res.content.decode('utf-8').strip()
            except ApiError as e:
                if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    print("NO_MEMO_TODAY")
                    return
                else:
                    print(f"ERROR: Dropboxからのファイルダウンロードに失敗しました: {e}")
                    return
            
            if not notes:
                print("NO_MEMO_TODAY")
                return

            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-2.5-pro')
            
            prompt = f"""
            あなたは私の成長をサポートするコーチです。
            以下の断片的なメモ群から、自己理解を深めるためのシンプルなサマリーを作成してください。

            # 指示
            - サマリーは簡潔に、最も重要なポイントに絞ってください。
            - 以下の英語の見出しを使用してください。
            - 挨拶や前置きは一切含めず、本文のみを生成してください。

            # 出力フォーマット
            - **Highlights**: 今日の最も重要な出来事や感情
            - **Key Learnings**: 最も重要な学びや気づき
            - **Action Items**: 今日の経験から明日試すべきこと一つ

            # 本日のメモ
            {notes}
            """
            
            response = model.generate_content(prompt)
            if not response.candidates:
                print("ERROR: AIからの応答がありませんでした（安全フィルタの可能性）。")
                return
            
            summary_text = ''.join(part.text for part in response.candidates[0].content.parts)

            # Daily Summaryの見出しを英語に変更
            summary_to_append = f"\n\n## Daily Summary\n{summary_text}"
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