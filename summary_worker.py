import os
import sys
import datetime
import zoneinfo # 標準ライブラリ (Python 3.9+)
from pathlib import Path
from dotenv import load_dotenv
import google.generativeai as genai

# 標準出力のエンコーディングをUTF-8に設定（Cogとの連携に必須）
sys.stdout.reconfigure(encoding='utf-8')

# --- 設定読み込み ---
load_dotenv()
VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

def generate_summary():
    """AIによるサマリー生成とファイル追記を行い、結果を標準出力に返す"""
    # APIキーやパスの存在チェック
    if not GEMINI_API_KEY:
        print("ERROR: Gemini APIキーが.envファイルに設定されていません。")
        return
    if not VAULT_PATH:
        print("ERROR: Obsidianのパスが.envファイルに設定されていません。")
        return

    try:
        # 今日の日付に合わせたデイリーノートのパスを生成
        jst = zoneinfo.ZoneInfo("Asia/Tokyo")
        today = datetime.datetime.now(jst).date()
        date_str = today.strftime('%Y-%m-%d')
        file_path = Path(VAULT_PATH) / f"{date_str}.md"

        # 今日のデイリーノートが存在しない、または中身が空の場合
        notes = ""
        if file_path.exists():
            notes = file_path.read_text(encoding="utf-8").strip()
        
        if not notes:
            # メモがない場合は特別なメッセージを出力して終了
            print("NO_MEMO_TODAY")
            return

        # AIモデルの設定
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-pro') 
        # AIへの指示（プロンプト）
        prompt = f"""
        あなたは私の成長をサポートするコーチです。
        以下の断片的なメモ群から、今日一日の経験から学びや気づきを抽出し、自己理解を深めるためのサマリーを作成してください。
        私の思考や感情を読み取り、以下の観点で整理してください。
        今日のハイライト:最もポジティブだった出来事や、うまくいったことは何ですか？
        新しい学び・気づき:新しく知ったこと、ハッとしたこと、考え方が変わったことは何ですか？
        感情の動き:どのような時に、喜び、悩み、不安などを感じましたか？
        課題と改善点:うまくいかなかったことや、次にもっと良くできそうなことは何ですか？
        明日試してみたいこと:今日の学びを活かして、明日挑戦したい具体的なアクションを1つ提案してください。

# 本日のメモ
{notes}
"""
        response = model.generate_content(prompt)
        summary_text = response.text.strip()

        # 生成したサマリーをデイリーノートの末尾に追記
        # ※もしDiscordへの投稿だけでよければ、以下の3行は削除またはコメントアウトしてください
        summary_to_append = f"\n\n## 本日のサマリー\n{summary_text}"
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(summary_to_append)

        # 成功したサマリーのテキストを標準出力に出力（マネージャーへの報告）
        print(summary_text)

    except Exception as e:
        # 何かエラーが起きた場合は、エラーメッセージを出力
        print(f"ERROR: {e}")

if __name__ == "__main__":
    generate_summary()