import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
import zoneinfo # zoneinfo が利用できない場合は pytz など代替が必要
from filelock import FileLock
from dotenv import load_dotenv
import dropbox
from dropbox.exceptions import ApiError, AuthError # AuthError もインポート
from dropbox.files import WriteMode, DownloadError

# utils.obsidian_utilsからupdate_sectionをインポート
# --- utils ディレクトリが Python の検索パスに含まれている必要がある ---
# 例: PYTHONPATH にプロジェクトルートを追加するか、
# sync_worker.py をプロジェクトルートから実行する場合
try:
    # sync_worker.py がプロジェクトルートにある場合
    from utils.obsidian_utils import update_section
except ImportError:
    # sync_worker.py が cogs/ などサブディレクトリにある場合
    # sys.path.append(str(Path(__file__).resolve().parent.parent)) # プロジェクトルートを追加
    try:
        from utils.obsidian_utils import update_section
    except ImportError:
        logging.error("[IMPORT ERROR] utils.obsidian_utilsが見つかりません。update_section を使用できません。", exc_info=True)
        # 簡易的なダミー関数 (元のコードと同様)
        def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
            if section_header in current_content:
                lines = current_content.split('\n')
                try:
                    header_index = -1
                    for i, line in enumerate(lines):
                        if line.strip().lstrip('#').strip().lower() == section_header.lstrip('#').strip().lower():
                            header_index = i
                            break
                    if header_index == -1: raise ValueError("Header not found")
                    insert_index = header_index + 1
                    while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                        insert_index += 1
                    if insert_index > header_index + 1 and lines[insert_index - 1].strip() != "":
                        lines.insert(insert_index, "")
                        insert_index += 1
                    lines.insert(insert_index, text_to_add)
                    return "\n".join(lines)
                except ValueError:
                     logging.warning(f"セクション '{section_header}' が見つからないか、挿入位置の特定に失敗したため、末尾に追加します。")
                     return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"
            else:
                 logging.info(f"セクション '{section_header}' が存在しないため、末尾に追加します。")
                 return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"


# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][sync_worker] %(message)s",
    stream=sys.stderr
)

# --- 基本設定 ---
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
LAST_PROCESSED_ID_FILE_PATH = f"{DROPBOX_VAULT_PATH}/.bot/last_processed_id.txt"

# タイムゾーン設定
try:
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except Exception as e:
    logging.error(f"zoneinfo の初期化に失敗: {e}。UTCを使用します。")
    from datetime import timezone as tz
    JST = tz.utc

def process_pending_memos():
    """保留メモをDropbox上のDailyNoteに追加する"""
    logging.info(f"保留メモファイルを確認: {PENDING_MEMOS_FILE}")
    if not PENDING_MEMOS_FILE.exists():
        logging.info("保留メモファイルが存在しません。処理をスキップします。")
        return True

    lock_path = str(PENDING_MEMOS_FILE) + ".lock"
    logging.info(f"ロックファイルを取得試行: {lock_path}")
    lock = FileLock(lock_path, timeout=10)

    memos = []
    try:
        with lock:
            logging.info("ロック取得成功。保留メモファイルを読み込みます。")
            try:
                if PENDING_MEMOS_FILE.stat().st_size == 0:
                     logging.info("保留メモファイルは空です。")
                     return True

                with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                    memos = json.load(f)
                logging.info(f"{len(memos)} 件のメモをファイルから読み込みました。")

            except FileNotFoundError:
                logging.info("保留メモファイルが存在しませんでした（ロック中に削除された可能性）。")
                return True
            except (json.JSONDecodeError, ValueError) as e:
                logging.error(f"保留メモファイルのJSON解析に失敗: {e}", exc_info=True)
                try:
                    # 壊れたファイルをクリア
                    with open(PENDING_MEMOS_FILE, "w") as f: json.dump([], f)
                except Exception as write_e: logging.error(f"壊れたJSONファイルのクリアに失敗: {write_e}")
                return False
            except Exception as e:
                logging.error(f"保留メモファイルの読み込み中に予期せぬエラー: {e}", exc_info=True)
                return False

            if not memos:
                logging.info("ファイル内に処理対象のメモはありませんでした。")
                return True

            logging.info(f"[PROCESS] {len(memos)} 件のメモをDropboxに保存します...")

            # --- Dropbox 処理 ---
            dbx = None
            try:
                logging.info("Dropboxクライアントを初期化しています...")
                if not all([DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET]):
                    logging.error("Dropboxの認証情報（Refresh Token, App Key, App Secret）が不足しています。")
                    return False

                dbx = dropbox.Dropbox(
                    oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
                    app_key=DROPBOX_APP_KEY,
                    app_secret=DROPBOX_APP_SECRET,
                    timeout=60
                )
                dbx.users_get_current_account() # 接続テスト
                logging.info("[DROPBOX] Dropboxへの接続に成功しました。")

                # --- メモの処理 ---
                try:
                    sorted_memos = sorted(
                        [m for m in memos if m.get('id') is not None],
                        key=lambda m: int(m['id'])
                    )
                    memos_without_id = [m for m in memos if m.get('id') is None]
                    if memos_without_id:
                         logging.warning(f"{len(memos_without_id)}件のメモにIDがありません。これらは処理されません。")
                except (ValueError, TypeError) as e:
                    logging.error(f"メモのソート中にエラー（IDが数値でない可能性）: {e}", exc_info=True)
                    sorted_memos = [m for m in memos if m.get('id') is not None]

                memos_by_date = {}
                processed_ids_in_batch = set()
                failed_ids_in_batch = set()  # Dropbox操作で実際に失敗したID
                skipped_ids_in_batch = set() # 内容が空/無効でスキップされたID

                for memo in sorted_memos:
                    memo_id = memo.get('id')
                    try:
                        ts_str = memo.get('created_at')
                        if not ts_str: raise ValueError("created_at がありません")
                        timestamp_utc = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                        timestamp_jst = timestamp_utc.astimezone(JST)
                        date_str = timestamp_jst.strftime('%Y-%m-%d')
                        memos_by_date.setdefault(date_str, []).append(memo)
                        # ここではまだ processed_ids_in_batch に追加しない
                    except (KeyError, ValueError, TypeError) as e:
                        logging.error(f"メモの日付/ID処理中にエラー (ID: {memo_id}): {e}", exc_info=True)
                        if memo_id: failed_ids_in_batch.add(memo_id) # 日付処理エラーも失敗扱い
                        continue

                latest_processed_id_this_batch = None

                logging.info(f"{len(memos_by_date)} 日分のデイリーノートを更新します。")
                for date_str, memos_in_date in memos_by_date.items():
                    file_path = f"{DROPBOX_VAULT_PATH}/DailyNotes/{date_str}.md"
                    current_content = ""
                    date_process_success = True # この日付の処理が成功したかどうかのフラグ
                    ids_attempted_for_this_date = set() # この日付で処理を試みたID

                    logging.info(f"[DROPBOX] デイリーノート処理開始: {file_path}")

                    # 1. Download daily note
                    try:
                        logging.info(f"[DROPBOX] ダウンロード試行: {file_path}")
                        _, res = dbx.files_download(file_path)
                        current_content = res.content.decode('utf-8')
                        logging.info(f"[DROPBOX] ダウンロード成功: {file_path}")
                    except ApiError as e:
                        if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                            current_content = "" # ★ 修正: 初期値を空文字に変更
                            logging.info(f"[DROPBOX] 新規作成します: {file_path}")
                        else:
                            logging.error(f"[DROPBOX] ダウンロード失敗: {file_path}, Error: {e}", exc_info=True)
                            date_process_success = False # ダウンロード失敗
                            for memo in memos_in_date: # この日付のメモは全て失敗扱い
                                if memo.get('id'): failed_ids_in_batch.add(memo.get('id'))
                            continue # 次の日付へ
                    except Exception as e:
                        logging.error(f"[DROPBOX] ダウンロード中に予期せぬエラー: {file_path}, Error: {e}", exc_info=True)
                        date_process_success = False # ダウンロード失敗
                        for memo in memos_in_date:
                            if memo.get('id'): failed_ids_in_batch.add(memo.get('id'))
                        continue

                    # 2. Prepare content to add
                    content_to_add = []
                    ids_valid_for_this_date = set() # この日付で実際に追記されるID

                    for memo in memos_in_date:
                        memo_id = memo.get('id')
                        ids_attempted_for_this_date.add(memo_id) # 処理を試みたIDとして追加

                        # 既に他のステップで失敗としてマークされている場合はスキップ
                        if memo_id in failed_ids_in_batch:
                            continue

                        try:
                            # --- 内容の空チェックをここで行う ---
                            memo_content = memo.get('content', '').strip()
                            if not memo_content:
                                logging.warning(f"内容が空または無効なメモをスキップ (ID: {memo_id})")
                                skipped_ids_in_batch.add(memo_id) # スキップIDに追加
                                continue # 次のメモへ
                            # --- ここまで ---

                            timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                            time_str = timestamp_utc.astimezone(JST).strftime('%H:%M')
                            content_lines = memo_content.split('\n') # strip()済みの内容を使用

                            formatted_memo = f"- {time_str}\n\t- " + "\n\t- ".join(content_lines)
                            content_to_add.append(formatted_memo)
                            ids_valid_for_this_date.add(memo_id) # 追記されるIDとして追加

                        except (KeyError, ValueError, TypeError) as e:
                            logging.error(f"メモのフォーマット中にエラー (ID: {memo_id}): {e}", exc_info=True)
                            failed_ids_in_batch.add(memo_id) # フォーマットエラーは失敗扱い
                            date_process_success = False # この日付の処理は一部失敗

                    if not content_to_add:
                        logging.warning(f"{date_str} に追記する有効なメモがありませんでした（スキップされたメモのみ）。")
                        # スキップのみの場合は date_process_success は True のまま
                        # processed_ids_in_batch にスキップされたIDを追加
                        processed_ids_in_batch.update(skipped_ids_in_batch.intersection(ids_attempted_for_this_date))
                        continue # 次の日付へ

                    # 3. Use update_section
                    try:
                        memos_as_text = "\n".join(content_to_add)
                        new_content = update_section(current_content, memos_as_text, "## Memo")
                    except Exception as e:
                         logging.error(f"update_section 処理中にエラー ({file_path}): {e}", exc_info=True)
                         date_process_success = False # update_section 失敗
                         for memo_id in ids_valid_for_this_date: failed_ids_in_batch.add(memo_id)
                         continue

                    # 4. Upload daily note
                    try:
                        logging.info(f"[DROPBOX] アップロード試行: {file_path} ({len(ids_valid_for_this_date)}件)")
                        dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
                        logging.info(f"[DROPBOX] アップロード成功: {file_path}")

                        # 成功したIDを processed_ids_in_batch に追加
                        processed_ids_in_batch.update(ids_valid_for_this_date)
                        # スキップされたIDも処理済みとして追加
                        processed_ids_in_batch.update(skipped_ids_in_batch.intersection(ids_attempted_for_this_date))


                        # 最終処理IDを更新
                        if ids_valid_for_this_date:
                             try:
                                 valid_ids_int = [int(id_val) for id_val in ids_valid_for_this_date if id_val is not None]
                                 if valid_ids_int:
                                     last_id_this_date = max(valid_ids_int)
                                     if latest_processed_id_this_batch is None or last_id_this_date > latest_processed_id_this_batch:
                                         latest_processed_id_this_batch = last_id_this_date
                             except (ValueError, TypeError) as e:
                                 logging.error(f"最終処理IDの更新中にエラー (数値変換失敗): {e}")

                    except ApiError as e:
                        logging.error(f"[DROPBOX] アップロード失敗: {file_path}, Error: {e}", exc_info=True)
                        date_process_success = False # アップロード失敗
                        for memo_id in ids_valid_for_this_date: failed_ids_in_batch.add(memo_id)
                    except Exception as e:
                        logging.error(f"[DROPBOX] アップロード中に予期せぬエラー: {file_path}, Error: {e}", exc_info=True)
                        date_process_success = False # アップロード失敗
                        for memo_id in ids_valid_for_this_date: failed_ids_in_batch.add(memo_id)

                # --- Batch processing result and cleanup ---
                # failed_ids_in_batch が空かどうかで全体の成否を判断
                if not failed_ids_in_batch:
                    logging.info("今回のバッチ処理でDropbox操作の失敗はありませんでした。")
                    if latest_processed_id_this_batch is not None:
                        try:
                            logging.info(f"[DROPBOX] 最終処理IDを保存: {latest_processed_id_this_batch}")
                            dbx.files_upload(str(latest_processed_id_this_batch).encode('utf-8'), LAST_PROCESSED_ID_FILE_PATH, mode=WriteMode('overwrite'))
                            logging.info(f"[PROCESS] 最終処理IDをDropboxに保存成功: {latest_processed_id_this_batch}")
                        except Exception as e:
                            logging.error(f"[DROPBOX] 最終処理IDの保存に失敗: {e}", exc_info=True)
                            # 最終処理IDの保存失敗はワーカー全体のエラーとはしない

                    # Clear pending memos file ONLY IF no actual failures occurred
                    logging.info("処理が成功（またはスキップのみ）したため、保留メモファイルをクリアします。")
                    with open(PENDING_MEMOS_FILE, "w") as f:
                        json.dump([], f)
                    return True # 成功
                else:
                    # Dropbox操作で何らかの失敗があった場合
                    logging.error(f"今回のバッチ処理でエラーが発生しました。失敗ID: {failed_ids_in_batch}")
                    # Keep only failed memos
                    remaining_memos = [
                        memo for memo in memos
                        if memo.get('id') in failed_ids_in_batch
                    ]
                    logging.info(f"{len(remaining_memos)} 件の失敗メモを残します。")
                    try:
                        with open(PENDING_MEMOS_FILE, "w", encoding="utf-8") as f:
                            json.dump(remaining_memos, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logging.error(f"失敗したメモのファイル更新中にエラー: {e}", exc_info=True)
                    return False # 失敗

            except AuthError as e:
                logging.error(f"[DROPBOX] Dropbox認証エラー: {e}", exc_info=True)
                return False
            except ApiError as e:
                logging.error(f"[DROPBOX] Dropbox APIエラー（接続以外）: {e}", exc_info=True)
                return False
            except Exception as e:
                logging.error(f"[PROCESS] Dropbox処理中に予期せぬエラー: {e}", exc_info=True)
                return False

    except TimeoutError:
        logging.error(f"ロックファイルの取得にタイムアウトしました: {lock_path}")
        return False
    except Exception as e:
        logging.error(f"ロック処理またはファイル読み込み前の段階で予期せぬエラー: {e}", exc_info=True)
        return False


def main():
    """ワーカープロセスのメイン関数"""
    logging.info("--- 同期ワーカー起動 ---")
    success = False
    try:
        success = process_pending_memos()
    except Exception as e:
        logging.error(f"同期ワーカーのメイン処理で致命的なエラー: {e}", exc_info=True)
        success = False

    if success:
        logging.info("--- 同期ワーカー正常完了 ---")
        sys.exit(0) # 成功時は 0 で終了
    else:
        logging.error("--- 同期ワーカーでエラーが発生しました ---")
        sys.exit(1) # 失敗時は 1 で終了

if __name__ == "__main__":
    main()