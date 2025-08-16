# q

import os
import json
from datetime import datetime
from dotenv import load_dotenv
import pytz
import subprocess # 外部コマンドを実行するために追加

# .envファイルから環境変数を読み込む
load_dotenv()

# --- 定数定義 ---
PENDING_MEMOS_FILE = "pending_memos.json"
VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH') # Renderの永続ディスクのマウントパスが入る

def run_command(command):
    """コマンドを実行し、出力を表示するヘルパー関数"""
    result = subprocess.run(command, shell=True, capture_output=True, text=True, encoding='utf-8')
    if result.stdout:
        print(f"[rclone STDOUT]: {result.stdout.strip()}")
    if result.stderr:
        print(f"[rclone STDERR]: {result.stderr.strip()}")
    return result.returncode == 0

def sync_from_dropbox():
    """DropboxからObsidianの保管庫をダウンロードする"""
    print("[Worker] Dropboxから保管庫の同期を開始します...")
    # rclone.confのパスを環境変数で指定 (RenderのSecret Fileのパス)
    rclone_config_path = "/etc/secrets/rclone.conf"
    command = f"rclone sync Dropbox:Apps/remotely-save '{VAULT_PATH}' --config='{rclone_config_path}' -v"
    return run_command(command)

def sync_to_dropbox():
    """Obsidianの保管庫をDropboxにアップロードする"""
    print("[Worker] Dropboxへ保管庫の同期を開始します...")
    rclone_config_path = "/etc/secrets/rclone.conf"
    command = f"rclone sync '{VAULT_PATH}' Dropbox:Apps/remotely-save --config='{rclone_config_path}' -v"
    return run_command(command)

def sync_notes():
    """保留中のメモをObsidianの日次メモに書き込み、Dropboxと同期するメイン関数"""
    print("[Worker] 同期処理を開始します。")

    # 1. まずDropboxから最新のファイルを取得
    if not sync_from_dropbox():
        print("[Worker] 失敗: Dropboxからの同期に失敗しました。処理を中断します。")
        return

    # 2. 保留メモの読み込み
    try:
        with open(PENDING_MEMOS_FILE, "r", encoding='utf-8') as f:
            pending_memos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # ファイルがない、または空の場合は処理を終了
        print("[Worker] 同期する保留メモはありませんでした。")
        return

    if not pending_memos:
        print("[Worker] 同期する保留メモはありませんでした。")
        return
        
    if not VAULT_PATH:
        print("[Worker] エラー: .envにOBSIDIAN_VAULT_PATHが設定されていません。")
        return
        
    print(f"[Worker] {len(pending_memos)}件の保留メモを処理します。")

    # 3. メモを投稿日ごとにグループ分け
    memos_by_date = {}
    jst = pytz.timezone('Asia/Tokyo')

    for memo in pending_memos:
        timestamp_dt = datetime.fromisoformat(memo['created_at'])
        post_time_jst = timestamp_dt.astimezone(jst)
        post_date_str = post_time_jst.strftime('%Y-%m-%d')

        if post_date_str not in memos_by_date:
            memos_by_date[post_date_str] = []
        memos_by_date[post_date_str].append(memo)

    # 4. 日付ごとにファイルへ書き込み
    for post_date, memos_in_date in memos_by_date.items():
        try:
            file_name = f"{post_date}.md"
            file_path = os.path.join(VAULT_PATH, file_name)

            obsidian_content = []
            for memo in memos_in_date:
                timestamp_dt = datetime.fromisoformat(memo['created_at'])
                time_str = timestamp_dt.astimezone(jst).strftime('%H:%M')
                
                # 改行をObsidianのインデント付き改行に変換
                content = memo['content'].replace('\n', '\n\t- ')
                
                # フォーマット：時刻を見出しとし、その下にインデントされた内容を記載
                formatted_memo = f"- {time_str}\n\t- {content}"
                obsidian_content.append(formatted_memo)
            
            # 書き込む全コンテンツの先頭に改行を追加して、前の内容との間隔を確保
            full_content = "\n" + "\n".join(obsidian_content)

            with open(file_path, "a", encoding="utf-8") as f:
                f.write(full_content)
            
            print(f"[Worker] 成功: {post_date} のデイリーノートに {len(memos_in_date)}件のメモを同期しました。")

        except Exception as e:
            print(f"[Worker] 失敗: {post_date} のファイル書き込み中にエラーが発生しました。 {e}")

    # 5. 最後に変更をDropboxにアップロード
    if not sync_to_dropbox():
        print("[Worker] 警告: Dropboxへのアップロードに失敗しました。")
    else:
        print("[Worker] Dropboxへのアップロードが完了しました。")
    
    # 6. 処理が完了したので保留メモファイルを空にする
    try:
        with open(PENDING_MEMOS_FILE, "w", encoding='utf-8') as f:
            json.dump([], f)
        print("[Worker] 保留メモファイルをクリアしました。")
    except Exception as e:
        print(f"[Worker] エラー: 保留メモファイルのクリアに失敗しました。 {e}")

if __name__ == "__main__":
    sync_notes()