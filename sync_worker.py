# sync_worker.py

import os
import json
from datetime import datetime
from dotenv import load_dotenv
import pytz

load_dotenv()

PENDING_MEMOS_FILE = "pending_memos.json"
VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')

def sync_notes():
    print("[Worker] 同期処理を開始します。")

    try:
        with open(PENDING_MEMOS_FILE, "r", encoding='utf-8') as f:
            pending_memos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("[Worker] 同期するメモはありませんでした。")
        return

    if not pending_memos:
        print("[Worker] 同期するメモはありませんでした。")
        return

    if not VAULT_PATH:
        print("[Worker] エラー: .envにOBSIDIAN_VAULT_PATHが設定されていません。")
        return
    
    # メモを投稿日ごとにグループ分けするための辞書を作成
    memos_by_date = {}
    jst = pytz.timezone('Asia/Tokyo')

    for memo in pending_memos:
        timestamp_dt = datetime.fromisoformat(memo['created_at'])
        # 投稿日時をJSTに変換
        post_time_jst = timestamp_dt.astimezone(jst)
        # 投稿日から日付部分だけを取得 (例: '2025-08-16')
        post_date_str = post_time_jst.strftime('%Y-%m-%d')

        # 日付ごとにメモをグループ分け
        if post_date_str not in memos_by_date:
            memos_by_date[post_date_str] = []
        memos_by_date[post_date_str].append(memo)

    # 日付ごとにファイルへの書き込み処理を行う
    for post_date, memos_in_date in memos_by_date.items():
        try:
            file_name = f"{post_date}.md"
            file_path = os.path.join(VAULT_PATH, file_name)

            obsidian_content = []
            for memo in memos_in_date:
                timestamp_dt = datetime.fromisoformat(memo['created_at'])
                # 時刻部分だけを取得 (例: '15:30')
                time_str = timestamp_dt.astimezone(jst).strftime('%H:%M')
                
                # 改行をObsidianのインデント付き改行に変換
                content = memo['content'].replace('\n', '\n\t- ')
                
                # 新しいフォーマット：投稿者名を削除し、改行に対応
                formatted_memo = f"- {time_str}\n\t- {content}"
                obsidian_content.append(formatted_memo)
            
            full_content = "\n" + "\n".join(obsidian_content)

            with open(file_path, "a", encoding="utf-8") as f:
                f.write(full_content)
            
            print(f"[Worker] 成功: {post_date} のデイリーノートに {len(memos_in_date)}件のメモを同期しました。")

        except Exception as e:
            print(f"[Worker] 失敗: {post_date} のファイル書き込み中にエラーが発生しました。 {e}")

    # 処理が完了したので保留メモを空にする
    with open(PENDING_MEMOS_FILE, "w", encoding='utf-8') as f:
        json.dump([], f)

if __name__ == "__main__":
    sync_notes()