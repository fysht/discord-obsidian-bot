import os
import sys
import asyncio
import logging
import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import DownloadError # ★修正: ここからインポートする
import google.generativeai as genai
from dotenv import load_dotenv
import datetime
import zoneinfo

# --- 設定 ---
# 標準出力の文字コードをUTF-8に強制（Windows等での文字化け防止）
sys.stdout.reconfigure(encoding='utf-8')

# ログ設定 (標準エラー出力に出すことで、Bot側が受け取る標準出力(要約結果)と混ざらないようにする)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][summary_worker] %(message)s",
    stream=sys.stderr
)

load_dotenv()

# --- 定数 ---
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

try:
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except Exception:
    # tzdataがない環境へのフォールバック（基本はインストール推奨）
    JST = datetime.timezone(datetime.timedelta(hours=9))

async def generate_summary(period: str, target_date_str: str):
    """
    指定された期間・日付のデイリーノートを読み込み、AIで要約して標準出力する
    """
    
    # 1. クライアント初期化
    if not all([DROPBOX_REFRESH_TOKEN, GEMINI_API_KEY]):
        logging.error("環境変数が不足しています (DROPBOX_REFRESH_TOKEN, GEMINI_API_KEY)")
        print("ERROR: 環境変数が不足しています。")
        return

    try:
        dbx = dropbox.Dropbox(
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET
        )
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-3-pro-preview")
    except Exception as e:
        logging.error(f"クライアント初期化エラー: {e}")
        print(f"ERROR: 初期化エラー: {e}")
        return

    # 2. 対象期間のファイルを収集
    try:
        target_date = datetime.datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except ValueError:
        logging.error(f"日付形式エラー: {target_date_str}")
        print("ERROR: 日付形式が不正です。")
        return

    files_to_read = []

    if period == "daily":
        files_to_read.append(f"{DROPBOX_VAULT_PATH}/DailyNotes/{target_date_str}.md")
    
    elif period == "weekly":
        # target_date (日曜日) から過去7日分
        for i in range(7):
            d = target_date - datetime.timedelta(days=i)
            files_to_read.append(f"{DROPBOX_VAULT_PATH}/DailyNotes/{d.strftime('%Y-%m-%d')}.md")
            
    elif period == "monthly":
        # target_date (月末) の月の全ファイル
        # 簡易的に1日から月末まで回す
        first_day = target_date.replace(day=1)
        for i in range((target_date - first_day).days + 1):
            d = first_day + datetime.timedelta(days=i)
            files_to_read.append(f"{DROPBOX_VAULT_PATH}/DailyNotes/{d.strftime('%Y-%m-%d')}.md")

    # 3. コンテンツ読み込み
    full_text = ""
    
    for file_path in files_to_read:
        try:
            _, res = dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            
            full_text += f"\n--- Date: {file_path.split('/')[-1]} ---\n{content}\n"
            
        except ApiError as e:
            # ファイルがない日はスキップ
            # ★ 修正箇所: DownloadError は dropbox.files からインポートしたものを使用
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                continue
            else:
                logging.warning(f"ファイル読み込みエラー ({file_path}): {e}")

    if not full_text.strip():
        # メモが全くなかった場合
        print("NO_MEMO_TODAY") # Cog側でこれを検知してメッセージを変える
        return

    # 4. AI要約の実行
    logging.info(f"AI要約を開始します。文字数: {len(full_text)}")
    
    prompt = ""
    if period == "daily":
        prompt = f"""
        あなたは優秀な秘書です。以下の今日のメモやログを分析し、1日の活動を要約してください。
        
        # 指示
        - **やったこと**: 主要なタスクや活動を箇条書きで。
        - **インサイト**: メモから読み取れる気付きやアイデア、感情の変化などがあれば。
        - **ネクストアクション**: 明日やるべきことや、残っている課題があれば。
        - 全体をMarkdown形式で、簡潔かつ読みやすくまとめてください。
        
        # 今日のメモ
        {full_text[:20000]}
        """
    else:
        prompt = f"""
        あなたは優秀な秘書です。以下の期間（{period}）のログを分析し、活動のハイライトを要約してください。
        全体的な傾向、達成した大きな成果、積み残している課題を中心にまとめてください。
        
        # 期間中のログ
        {full_text[:30000]}
        """

    try:
        response = await model.generate_content_async(prompt)
        summary = response.text.strip()
        
        # 5. 結果を標準出力する (これがDiscordに投稿される)
        print(summary)
        logging.info("要約が完了し、出力しました。")
        
    except Exception as e:
        logging.error(f"AI生成エラー: {e}")
        print(f"ERROR: AI生成中にエラーが発生しました: {e}")

def main():
    if len(sys.argv) < 3:
        print("ERROR: 引数が不足しています (period, target_date)")
        return

    period = sys.argv[1] # daily, weekly, monthly
    target_date_str = sys.argv[2] # YYYY-MM-DD

    asyncio.run(generate_summary(period, target_date_str))

if __name__ == "__main__":
    main()