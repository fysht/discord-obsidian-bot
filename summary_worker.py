import logging
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
from collections import defaultdict
from datetime import datetime
import pytz
import os
import json
import time
import asyncio
from multiprocessing import Queue, Process

# JSTタイムゾーンの定義
JST = pytz.timezone('Asia/Tokyo')

def get_jst_now():
    """現在のJST時刻を取得する"""
    return datetime.now(JST)

def load_processed_memos():
    """処理済みのメモIDをファイルから読み込む"""
    if os.path.exists('processed_memos.json'):
        with open('processed_memos.json', 'r') as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()

def save_processed_memos(processed_ids):
    """処理済みのメモIDをファイルに保存する"""
    with open('processed_memos.json', 'w') as f:
        json.dump(list(processed_ids), f)

async def sync_memos_to_obsidian(memos: list, dbx: dropbox.Dropbox, vault_path: str):
    """
    メモをObsidianに同期する
    重複処理を防ぎ、詳細なデバッグログを出力するように修正
    """
    if not memos:
        logging.info("[sync_worker] 同期対象のメモはありませんでした。")
        return True, set()

    logging.info(f"[sync_worker] {len(memos)}件のメモの同期処理を開始します。")
    logging.info(f"[sync_worker] 受信したメモデータ: {memos}")

    processed_ids = set()
    memos_by_date = defaultdict(list)

    # メモを日付ごとにグループ化
    for memo in memos:
        try:
            # タイムゾーン情報が付与されたISOフォーマット文字列をdatetimeオブジェクトに変換
            created_at_utc = datetime.fromisoformat(memo['created_at'])
            # JSTに変換
            created_at_jst = created_at_utc.astimezone(JST)
            date_str = created_at_jst.strftime('%Y-%m-%d')
            memos_by_date[date_str].append(memo)
        except (KeyError, ValueError) as e:
            logging.error(f"[sync_worker] メモのタイムスタンプ処理中にエラーが発生しました: {e}。スキップします。 メモ: {memo}")
            if 'message_id' in memo:
                processed_ids.add(memo['message_id'])

    if not memos_by_date:
        logging.warning("[sync_worker] 有効な日付でグループ化できるメモがありませんでした。処理を中断します。")
        return False, processed_ids

    logging.info(f"[sync_worker] {len(memos_by_date)}日分のメモを処理します: {list(memos_by_date.keys())}")

    # 日付ごとにファイルに追記
    for date_str, date_memos in memos_by_date.items():
        daily_note_path = os.getenv("DAILY_NOTE_PATH", "Daily")
        file_path = f"{vault_path}/{daily_note_path}/{date_str}.md"
        
        logging.info(f"[sync_worker] ファイルパス '{file_path}' の処理を開始します。")

        try:
            # 既存のファイルをダウンロード
            _, res = dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            logging.info(f"[sync_worker] 既存ファイル '{file_path}' をダウンロードしました。")
        except ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                content = f"# {date_str}\n\n"
                logging.info(f"[sync_worker] ファイルが存在しないため、新規作成します: '{file_path}'")
            else:
                logging.error(f"[sync_worker] Dropboxファイルのダウンロード中に予期せぬAPIエラー: {e}")
                continue

        memo_contents = []
        for memo in date_memos:
            created_at_jst = datetime.fromisoformat(memo['created_at']).astimezone(JST)
            time_str = created_at_jst.strftime('%H:%M')
            memo_text = memo['content'].replace('\n', '\n- ')
            memo_contents.append(f"- {time_str} {memo_text}")
            processed_ids.add(memo['message_id'])

        new_content = content.strip() + "\n\n" + "\n".join(memo_contents) + "\n"

        try:
            dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
            logging.info(f"✅ [sync_worker] Obsidianに{len(date_memos)}件のメモを正常に同期しました: {file_path}")
        except Exception as e:
            logging.error(f"❌ [sync_worker] Obsidianへのアップロード中に致命的なエラーが発生: {e}", exc_info=True)
            for memo in date_memos:
                processed_ids.remove(memo['message_id'])
            return False, set()

    logging.info(f"[sync_worker] 同期処理が正常に完了しました。処理済みID: {processed_ids}")
    return True, processed_ids

def run_sync_worker(memo_queue: Queue, dropbox_refresh_token: str, dropbox_app_key: str, dropbox_app_secret: str, dropbox_vault_path: str):
    """同期処理を実行するワーカープロセス"""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=dropbox_refresh_token,
        app_key=dropbox_app_key,
        app_secret=dropbox_app_secret
    )
    
    logging.info("--- 同期ワーカー起動 ---")
    
    memos_to_sync = []
    while not memo_queue.empty():
        memos_to_sync.append(memo_queue.get())
        
    if not memos_to_sync:
        logging.info("--- 同期対象なし ---")
        return

    try:
        success, processed_ids = asyncio.run(sync_memos_to_obsidian(memos_to_sync, dbx, dropbox_vault_path))
        
        if success:
            # 既存の処理済みIDを読み込み、新しいIDを追加して保存
            existing_processed_ids = load_processed_memos()
            updated_ids = existing_processed_ids.union(processed_ids)
            save_processed_memos(updated_ids)
            logging.info("--- 同期ワーカー正常完了 ---")
        else:
            # 失敗した場合はキューに戻す（実装によっては無限ループのリスクあり）
            logging.error("--- 同期ワーカーでエラーが発生しました ---")
            for memo in memos_to_sync:
                 if memo['message_id'] not in processed_ids:
                    memo_queue.put(memo)
    except Exception as e:
        logging.error(f"同期ワーカーで致命的なエラー: {e}", exc_info=True)
        # エラーが発生した場合、メモをキューに戻す
        for memo in memos_to_sync:
            memo_queue.put(memo)

if __name__ == '__main__':
    # このスクリプトが直接実行された場合のテストコードなど
    # 通常の運用ではmain.pyから呼び出されるため、ここは空でも良い
    pass