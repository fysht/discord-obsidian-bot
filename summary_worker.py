import os
import sys
import datetime
import zoneinfo
from dotenv import load_dotenv
import google.generativeai as genai
import dropbox
from dropbox.exceptions import ApiError
import statistics

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
FITBIT_CLIENT_ID = os.getenv("FITBIT_CLIENT_ID")
FITBIT_CLIENT_SECRET = os.getenv("FITBIT_CLIENT_SECRET")
FITBIT_USER_ID = os.getenv("FITBIT_USER_ID", "-")

# --- FitbitClientのインポート ---
# 親ディレクトリをパスに追加
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fitbit_client import FitbitClient

def get_fitbit_data_for_period(dbx, start_date, end_date):
    """指定期間のFitbitデータを取得・集計する"""
    fitbit_client = FitbitClient(FITBIT_CLIENT_ID, FITBIT_CLIENT_SECRET, dbx, FITBIT_USER_ID)
    
    all_sleep_data = []
    all_activity_data = []
    
    current_date = start_date
    while current_date <= end_date:
        # この部分は非同期ではないので、async/awaitは使わない
        # FitbitClientのメソッドが非同期の場合は、run_in_executorなどで実行する必要がある
        # ここでは、簡単のため同期的に実行されると仮定する
        # sleep_data = fitbit_client.get_sleep_data(current_date)
        # activity_data = fitbit_client.get_activity_summary(current_date)
        # all_sleep_data.append(sleep_data)
        # all_activity_data.append(activity_data)
        current_date += datetime.timedelta(days=1)
        
    # ダミーデータを返す（実際のFitbit連携部分は未実装）
    return {
        "avg_sleep_score": 75,
        "total_steps": 50000,
    }


def generate_summary(period: str, date_str: str):
    """指定された期間のサマリーを生成し、結果を標準出力に返す"""
    if not GEMINI_API_KEY:
        print("ERROR: Gemini APIキーが設定されていません。")
        return
    if not all([DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN]):
        print("ERROR: Dropboxの認証情報が不足しています。")
        return

    try:
        target_date = datetime.datetime.fromisoformat(date_str).date()
        
        if period == "daily":
            start_date = end_date = target_date
            output_folder = "DailyNotes"
            output_filename = f"{date_str}.md"
        elif period == "weekly":
            start_date = target_date - datetime.timedelta(days=target_date.weekday())
            end_date = start_date + datetime.timedelta(days=6)
            output_folder = "WeeklyNotes"
            output_filename = f"{start_date.strftime('%Y-W%U')}.md"
        elif period == "monthly":
            start_date = target_date.replace(day=1)
            next_month = start_date.replace(day=28) + datetime.timedelta(days=4)
            end_date = next_month - datetime.timedelta(days=next_month.day)
            output_folder = "MonthlyNotes"
            output_filename = f"{start_date.strftime('%Y-%m')}.md"
        else:
            print("ERROR: Invalid period specified.")
            return

        with dropbox.Dropbox(
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET
        ) as dbx:
            dbx.check_user()
            
            # 1. 対象期間のDailyNotesの内容をすべて取得
            all_notes_content = []
            current_date = start_date
            while current_date <= end_date:
                file_path = f"{DROPBOX_VAULT_PATH}/DailyNotes/{current_date.strftime('%Y-%m-%d')}.md"
                try:
                    _, res = dbx.files_download(file_path)
                    all_notes_content.append(res.content.decode('utf-8').strip())
                except ApiError as e:
                    if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        pass # ファイルがなくてもエラーにしない
                    else:
                        raise
                current_date += datetime.timedelta(days=1)

            if not all_notes_content:
                print("NO_MEMO")
                return
            
            combined_notes = "\n\n---\n\n".join(all_notes_content)
            
            # 2. Fitbitデータの取得
            fitbit_summary = get_fitbit_data_for_period(dbx, start_date, end_date)

            # 3. AIによるサマリーとアドバイスの生成
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-2.5-pro')
            
            prompt = f"""
            あなたは私の成長をサポートするコーチです。
            以下は、私の一週間（または一ヶ月）の活動記録と健康データです。
            これらを分析し、自己理解を深めるためのサマリーと、次の一週間（または一ヶ月）に向けた具体的なアドバイスを作成してください。

            # 指示
            - 挨拶や前置きは一切含めず、本文のみを生成してください。
            - 以下の英語の見出しを使用してください。
            - **Summary**: 活動記録から、思考や行動の傾向を分析・要約します。
            - **AI Coach**: 健康データと活動記録を総合的に判断し、次へのアクションプランを提案します。

            # 活動記録
            {combined_notes}
            
            # 健康データサマリー
            {fitbit_summary}
            """
            
            response = model.generate_content(prompt)
            if not response.candidates:
                print("ERROR: AIからの応答がありませんでした（安全フィルタの可能性）。")
                return
            
            summary_text = ''.join(part.text for part in response.candidates[0].content.parts)

            # 4. 結果をObsidianに保存
            output_path = f"{DROPBOX_VAULT_PATH}/{output_folder}/{output_filename}"
            
            # 既存のファイル内容を取得（あれば）
            try:
                _, res = dbx.files_download(output_path)
                existing_content = res.content.decode('utf-8')
            except ApiError:
                existing_content = "" # 新規作成

            # update_sectionのような形で追記（ここでは簡易的に追記）
            if "## Summary and AI Coach" in existing_content:
                # 既存のセクションを更新するロジック（未実装）
                new_content = existing_content 
            else:
                new_content = existing_content + f"\n\n## Summary and AI Coach\n{summary_text}"

            dbx.files_upload(
                new_content.encode('utf-8'),
                output_path,
                mode=dropbox.files.WriteMode('overwrite')
            )

            print(summary_text)

    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 2:
        target_period = sys.argv[1] # "daily", "weekly", "monthly"
        target_date_str = sys.argv[2]
        generate_summary(target_period, target_date_str)
    else:
        # デフォルトはdaily
        jst = zoneinfo.ZoneInfo("Asia/Tokyo")
        today_str = datetime.datetime.now(jst).date().isoformat()
        generate_summary("daily", today_str)