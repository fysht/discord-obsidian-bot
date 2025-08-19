import os
import sys
import datetime
import zoneinfo
from pathlib import Path
from dotenv import load_dotenv
import google.generativeai as genai

# 標準出力のエンコーディングをUTF-8に設定
sys.stdout.reconfigure(encoding='utf-8')

# --- 設定読み込み ---
load_dotenv()
VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

def generate_summary(date_str: str):
    """指定された日付のサマリー生成とファイル追記を行い、結果を標準出力に返す"""
    if not GEMINI_API_KEY:
        print("ERROR: Gemini APIキーが.envファイルに設定されていません。")
        return
    if not VAULT_PATH:
        print("ERROR: Obsidianのパスが.envファイルに設定されていません。")
        return

    try:
        # 引数で渡された日付文字列からファイルパスを生成
        file_path = Path(VAULT_PATH) / "DailyNotes" / f"{date_str}.md"

        notes = ""
        if file_path.exists():
            notes = file_path.read_text(encoding="utf-8").strip()
        
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
        - 今日のハイライト: 最もポジティブだった出来事や、うまくいったことは何ですか？
        - 新しい学び・気づき: 新しく知ったこと、ハッとしたこと、考え方が変わったことは何ですか？
        - 感情の動き: どのような時に、喜び、悩み、不安などを感じましたか？
        - 課題と改善点: うまくいかなかったことや、次にもっと良くできそうなことは何ですか？
        - 明日試してみたいこと: 今日の学びを活かして、明日挑戦したい具体的なアクションを1つ提案してください。

# 本日のメモ
{notes}
"""
        response = model.generate_content(prompt)
        summary_text = response.text.strip()

        summary_to_append = f"\n\n## 本日のサマリー\n{summary_text}"
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(summary_to_append)

        print(summary_text)

    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_date_str = sys.argv[1]
        generate_summary(target_date_str)
    else:
        # 引数がない場合は、念のため今日の日付で実行
        jst = zoneinfo.ZoneInfo("Asia/Tokyo")
        today_str = datetime.datetime.now(jst).date().isoformat()
        generate_summary(today_str)