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

# JSTタイムゾーンの定義
JST = pytz.timezone('Asia/Tokyo')

def get_jst_now():
    """現在のJST時刻を取得する"""
    return datetime.now(JST)

def load_processed_memos():
    """処理済みのメモIDをファイルから読み込む"""
    if os.path.exists('processed_memos.json'):
        with open('processed_memos.json', 'r') as f:
            return set(json.load(f))
    return set()

def save_processed_memos(processed_ids):
    """処理済みのメモIDをファイルに保存する"""
    with open('processed_memos.json', 'w') as f:
        json.dump(list(processed_ids), f)

async def sync_memos_to_obsidian(memos: list, dbx: dropbox.Dropbox, vault_path: str):
    """
    メモをObsidianに同期する。
    重複処理を防ぎ、詳細なデバッグログを出力するように修正。
    """
    if not memos:
        logging.info("[sync_worker] 同期対象のメモはありませんでした。")
        return True, set()

    # ★★★デバッグログ追加★★★
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
            # エラーが発生したメモも処理済みとして扱い、無限ループを防ぐ
            if 'message_id' in memo:
                processed_ids.add(memo['message_id'])


    # ★★★デバッグログ追加★★★
    if not memos_by_date:
        logging.warning("[sync_worker] 有効な日付でグループ化できるメモがありませんでした。処理を中断します。")
        return False, processed_ids


    logging.info(f"[sync_worker] {len(memos_by_date)}日分のメモを処理します: {list(memos_by_date.keys())}")


    # 日付ごとにファイルに追記
    for date_str, date_memos in memos_by_date.items():
        daily_note_path = os.getenv("DAILY_NOTE_PATH", "Daily")
        file_path = f"{vault_path}/{daily_note_path}/{date_str}.md"
        
        # ★★★デバッグログ追加★★★
        logging.info(f"[sync_worker] ファイルパス '{file_path}' の処理を開始します。")

        try:
            # 既存のファイルをダウンロード
            _, res = dbx.files_download(file_path)
            content = res.content.decode('utf-8')
            logging.info(f"[sync_worker] 既存ファイル '{file_path}' をダウンロードしました。")
        except ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                # ファイルが存在しない場合は新規作成
                content = f"# {date_str}\n\n"
                logging.info(f"[sync_worker] ファイルが存在しないため、新規作成します: '{file_path}'")
            else:
                logging.error(f"[sync_worker] Dropboxファイルのダウンロード中に予期せぬAPIエラー: {e}")
                continue # この日付の処理をスキップ

        # 追記するコンテンツを作成
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
            # ★★★ 重要な成功ログ ★★★
            logging.info(f"✅ [sync_worker] Obsidianに{len(date_memos)}件のメモを正常に同期しました: {file_path}")
        except Exception as e:
            logging.error(f"❌ [sync_worker] Obsidianへのアップロード中に致命的なエラーが発生: {e}", exc_info=True)
            # 失敗した場合は、処理済みIDから今回のIDを除外して再試行の機会を残す
            for memo in date_memos:
                processed_ids.remove(memo['message_id'])
            return False, set() # 処理失敗として終了

    logging.info(f"[sync_worker] 同期処理が正常に完了しました。処理済みID: {processed_ids}")
    return True, processed_ids

# (以降のsync_worker.pyの他の部分は変更なし)
# main関数やrun_sync_worker関数など...