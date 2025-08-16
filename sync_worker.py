# sync_worker.py

import os
import json
from datetime import datetime
from dotenv import load_dotenv
import pytz
import subprocess
import sys

load_dotenv()

PENDING_MEMOS_FILE = "pending_memos.json"
VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')
RCLONE_CONFIG_PATH = os.getenv('RCLONE_CONFIG_PATH')

def run_command(command):
    result = subprocess.run(command, shell=True, capture_output=True, text=True, encoding='utf-8')
    if result.stdout:
        safe_stdout = result.stdout.strip().encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding)
        print(f"[rclone STDOUT]: {safe_stdout}")
    if result.stderr:
        safe_stderr = result.stderr.strip().encode(sys.stderr.encoding, errors='replace').decode(sys.stderr.encoding)
        print(f"[rclone STDERR]: {safe_stderr}")
    return result.returncode == 0

def sync_from_dropbox():
    print("[Worker] Dropboxから保管庫の同期を開始します...")
    config_option = f'--config="{RCLONE_CONFIG_PATH}"' if RCLONE_CONFIG_PATH else ""
    command = f'rclone sync Dropbox:Apps/remotely-save/MyKnowledgeBase "{VAULT_PATH}" {config_option} -v'
    return run_command(command)

def sync_to_dropbox():
    print("[Worker] Dropboxへ保管庫の同期を開始します...")
    config_option = f'--config="{RCLONE_CONFIG_PATH}"' if RCLONE_CONFIG_PATH else ""
    command = f'rclone sync "{VAULT_PATH}" Dropbox:Apps/remotely-save/MyKnowledgeBase {config_option} -v'
    return run_command(command)

def sync_notes():
    print("[Worker] 同期処理を開始します。")
    if not VAULT_PATH:
        print("[Worker] エラー: .envにOBSIDIAN_VAULT_PATHが設定されていません。")
        return
    os.makedirs(VAULT_PATH, exist_ok=True)
    if not sync_from_dropbox():
        print("[Worker] 失敗: Dropboxからの同期に失敗しました。")
        return
    try:
        with open(PENDING_MEMOS_FILE, "r", encoding='utf-8') as f: pending_memos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return
    if not pending_memos: return
    print(f"[Worker] {len(pending_memos)}件の保留メモを処理します。")
    memos_by_date = {}
    jst = pytz.timezone('Asia/Tokyo')
    for memo in pending_memos:
        timestamp_dt = datetime.fromisoformat(memo['created_at'])
        post_time_jst = timestamp_dt.astimezone(jst)
        post_date_str = post_time_jst.strftime('%Y-%m-%d')
        memos_by_date.setdefault(post_date_str, []).append(memo)
    for post_date, memos_in_date in memos_by_date.items():
        try:
            file_name = f"{post_date}.md"
            file_path = os.path.join(VAULT_PATH, file_name)
            print(f"[Worker] 書き込み先のフルパス: {os.path.abspath(file_path)}")
            obsidian_content = []
            for memo in memos_in_date:
                timestamp_dt = datetime.fromisoformat(memo['created_at'])
                time_str = timestamp_dt.astimezone(jst).strftime('%H:%M')
                content = memo['content'].replace('\n', '\n\t- ')
                formatted_memo = f"- {time_str}\n\t- {content}"
                obsidian_content.append(formatted_memo)
            full_content = "\n" + "\n".join(obsidian_content)
            with open(file_path, "a", encoding="utf-8") as f: f.write(full_content)
            print(f"[Worker] 成功: {post_date} のデイリーノートに {len(memos_in_date)}件のメモを同期しました。")
        except Exception as e: print(f"[Worker] 失敗: {post_date} のファイル書き込み中にエラーが発生しました。 {e}")
    if not sync_to_dropbox(): print("[Worker] 警告: Dropboxへのアップロードに失敗しました。")
    else: print("[Worker] Dropboxへのアップロードが完了しました。")
    try:
        with open(PENDING_MEMOS_FILE, "w", encoding='utf-8') as f: json.dump([], f)
        print("[Worker] 保留メモファイルをクリアしました。")
    except Exception as e: print(f"[Worker] エラー: 保留メモファイルのクリアに失敗しました。 {e}")

if __name__ == "__main__":
    sync_notes()