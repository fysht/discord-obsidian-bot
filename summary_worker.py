# summary_worker.py

import os
import json
from datetime import datetime
from dotenv import load_dotenv
import pytz
import google.generativeai as genai
import sys

# 標準出力のエンコーディングをUTF-8に設定
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()
VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

def generate_summary():
    if not GEMINI_API_KEY:
        print("エラー: Gemini APIキーが設定されていません。")
        return
    
    if not VAULT_PATH:
        print("エラー: Obsidianのパスが設定されていません。")
        return

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-pro')

        jst = pytz.timezone('Asia/Tokyo')
        today = datetime.now(jst).date()
        date_str = today.strftime('%Y-%m-%d')
        file_name = f"{date_str}.md"
        file_path = os.path.join(VAULT_PATH, file_name)

        notes = ""
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                notes = f.read()
        
        if not notes:
            print("NO_MEMO_TODAY")
            return

        prompt = f"あなたは優秀なアシスタントです。以下の断片的なメモ群を整理し、今日の活動を振り返るためのサマリーを作成してください。\n# 本日のメモ\n{notes}"
        response = model.generate_content(prompt)
        summary_text = response.text

        summary_to_append = f"\n\n## 🌙 本日のふりかえり\n{summary_text}"
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(summary_to_append)

        print(summary_text)

    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    generate_summary()