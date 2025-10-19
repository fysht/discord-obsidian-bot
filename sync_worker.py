import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
import zoneinfo
from filelock import FileLock
from dotenv import load_dotenv
import dropbox
from dropbox.exceptions import ApiError, AuthError
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
                        if line.strip().lower() == section_header.lower():
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
                     return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"
            else:
                return f"{current_content.strip()}\n\n{section_header}\n{link_to_add}\n"

# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
# 標準エラー出力 (stderr) にログを出力するように変更
# Render は通常 stderr をログとして収集するため
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][sync_worker] %(message)s",
    stream=sys.stderr # 出力先を stderr に変更
)
# stdout/stderr のエンコーディング設定 (Render 環境によっては不要な場合あり)
# try:
#     sys.stdout.reconfigure(encoding='utf-8')
#     sys.stderr.reconfigure(encoding='utf-8')
# except AttributeError: # reconfigure がない古い Python バージョン向け
#     pass

# --- 基本設定 ---
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
LAST_PROCESSED_ID_FILE_PATH = f"{DROPBOX_VAULT_PATH}/.bot/last_processed_id.txt"

# タイムゾーン設定 (環境に合わせて pytz などに変更可能)
try:
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except Exception:
    logging.error("zoneinfo の初期化に失敗。タイムゾーン関連の処理でエラーが発生する可能性があります。")
    # フォールバックとして UTC を使うなどの処理が必要な場合
    from datetime import timezone as tz
    JST = tz.utc # 例: UTC フォールバック

def process_pending_memos():
    """保留メモをDropbox上のDailyNoteに追加する"""
    logging.info(f"保留メモファイルを確認: {PENDING_MEMOS_FILE}")
    if not PENDING_MEMOS_FILE.exists():
        logging.info("保留メモファイルが存在しません。処理をスキップします。")
        return True # 処理対象がないので成功扱い

    lock_path = str(PENDING_MEMOS_FILE) + ".lock"
    logging.info(f"ロックファイルを取得試行: {lock_path}")
    lock = FileLock(lock_path, timeout=10) # タイムアウトを設定

    memos = []
    try:
        with lock:
            logging.info("ロック取得成功。保留メモファイルを読み込みます。")
            try:
                # ファイルサイズが0の場合も考慮
                if PENDING_MEMOS_FILE.stat().st_size == 0:
                     logging.info("保留メモファイルは空です。")
                     return True # 空なので成功扱い

                with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                    memos = json.load(f)
                logging.info(f"{len(memos)} 件のメモをファイルから読み込みました。")

            except FileNotFoundError:
                logging.info("保留メモファイルが存在しませんでした（ロック中に削除された可能性）。")
                return True # ファイルがないので成功扱い
            except (json.JSONDecodeError, ValueError) as e:
                logging.error(f"保留メモファイルのJSON解析に失敗: {e}", exc_info=True)
                # ファイルが壊れている可能性があるので、空にしてエラーとする
                try:
                    with open(PENDING_MEMOS_FILE, "w") as f: json.dump([], f)
                except Exception as write_e:
                    logging.error(f"壊れたJSONファイルのクリアに失敗: {write_e}")
                return False # JSONエラーは失敗扱い
            except Exception as e:
                logging.error(f"保留メモファイルの読み込み中に予期せぬエラー: {e}", exc_info=True)
                return False # その他の読み込みエラーも失敗扱い

            if not memos:
                logging.info("ファイル内に処理対象のメモはありませんでした。")
                return True # メモがないので成功扱い

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
                    timeout=60 # タイムアウトを少し長めに設定
                )
                dbx.users_get_current_account() # 接続テスト
                logging.info("[DROPBOX] Dropboxへの接続に成功しました。")

                # --- メモの処理 ---
                # IDでソートして処理順を保証 (IDが文字列の場合があるのでintに変換)
                try:
                    sorted_memos = sorted(memos, key=lambda m: int(m['id']))
                except (KeyError, ValueError, TypeError) as e:
                    logging.error(f"メモのソート中にエラー（ID不正の可能性）: {e}", exc_info=True)
                    # ソートできない場合は元の順序で試みる
                    sorted_memos = memos

                memos_by_date = {}
                processed_ids_in_batch = set() # このバッチで処理したIDを追跡
                failed_ids_in_batch = set() # このバッチで失敗したIDを追跡

                for memo in sorted_memos:
                    memo_id = memo.get('id')
                    try:
                        # タイムスタンプのパースをより堅牢に
                        ts_str = memo.get('created_at')
                        if not ts_str: raise ValueError("created_at がありません")
                        # ISOフォーマット (+00:00 や Z に対応)
                        timestamp_utc = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                        # JSTに変換
                        timestamp_jst = timestamp_utc.astimezone(JST)
                        date_str = timestamp_jst.strftime('%Y-%m-%d')
                        memos_by_date.setdefault(date_str, []).append(memo)
                        processed_ids_in_batch.add(memo_id) # 処理対象としてマーク
                    except (KeyError, ValueError, TypeError) as e:
                        logging.error(f"メモの日付/ID処理中にエラー (ID: {memo_id}): {e}", exc_info=True)
                        if memo_id: failed_ids_in_batch.add(memo_id) # 失敗としてマーク
                        continue # エラーのあるメモはスキップ

                all_success_this_batch = True
                latest_processed_id_this_batch = None

                logging.info(f"{len(memos_by_date)} 日分のデイリーノートを更新します。")
                for date_str, memos_in_date in memos_by_date.items():
                    file_path = f"{DROPBOX_VAULT_PATH}/DailyNotes/{date_str}.md"
                    current_content = ""
                    logging.info(f"[DROPBOX] デイリーノート処理開始: {file_path}")

                    # 1. デイリーノートのダウンロード
                    try:
                        logging.info(f"[DROPBOX] ダウンロード試行: {file_path}")
                        _, res = dbx.files_download(file_path)
                        current_content = res.content.decode('utf-8')
                        logging.info(f"[DROPBOX] ダウンロード成功: {file_path}")
                    except ApiError as e:
                        if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                            current_content = f"# {date_str}\n" # 新規作成
                            logging.info(f"[DROPBOX] 新規作成します: {file_path}")
                        else:
                            logging.error(f"[DROPBOX] ダウンロード失敗: {file_path}, Error: {e}", exc_info=True)
                            all_success_this_batch = False
                            # この日付のメモIDを失敗リストに追加
                            for memo in memos_in_date:
                                if memo.get('id'): failed_ids_in_batch.add(memo.get('id'))
                            continue # この日付の処理を中断
                    except Exception as e:
                        logging.error(f"[DROPBOX] ダウンロード中に予期せぬエラー: {file_path}, Error: {e}", exc_info=True)
                        all_success_this_batch = False
                        for memo in memos_in_date:
                            if memo.get('id'): failed_ids_in_batch.add(memo.get('id'))
                        continue

                    # 2. メモ内容の追記準備
                    content_to_add = []
                    ids_for_this_date = set()
                    for memo in memos_in_date:
                        memo_id = memo.get('id')
                        # この日付の処理で以前に失敗マークが付いていなければ処理
                        if memo_id not in failed_ids_in_batch:
                            try:
                                timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                                time_str = timestamp_utc.astimezone(JST).strftime('%H:%M')
                                content_lines = memo.get('content', '').strip().split('\n')

                                # 箇条書き整形
                                if content_lines:
                                    formatted_memo = f"- {time_str}\n\t- {content_lines[0]}"
                                    if len(content_lines) > 1:
                                        formatted_memo += "\n" + "\n".join([f"\t- {line}" for line in content_lines[1:]])
                                    content_to_add.append(formatted_memo)
                                    ids_for_this_date.add(memo_id) # この日付で成功したID
                                else:
                                    logging.warning(f"内容が空のメモをスキップ (ID: {memo_id})")
                                    failed_ids_in_batch.add(memo_id) # 内容がないので失敗扱い

                            except (KeyError, ValueError, TypeError) as e:
                                logging.error(f"メモのフォーマット中にエラー (ID: {memo_id}): {e}", exc_info=True)
                                failed_ids_in_batch.add(memo_id) # フォーマット失敗も失敗扱い

                    if not content_to_add:
                        logging.warning(f"{date_str} に追記する有効なメモがありませんでした。")
                        continue # 次の日付へ

                    # 3. update_section を使って追記
                    try:
                        memos_as_text = "\n".join(content_to_add)
                        new_content = update_section(current_content, memos_as_text, "## Memo")
                    except Exception as e:
                         logging.error(f"update_section 処理中にエラー ({file_path}): {e}", exc_info=True)
                         all_success_this_batch = False
                         for memo_id in ids_for_this_date: failed_ids_in_batch.add(memo_id)
                         continue # この日付のアップロードを中断

                    # 4. デイリーノートのアップロード
                    try:
                        logging.info(f"[DROPBOX] アップロード試行: {file_path} ({len(memos_in_date)}件)")
                        dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
                        logging.info(f"[DROPBOX] アップロード成功: {file_path}")
                        # この日付で最後に成功したIDを更新
                        if ids_for_this_date:
                             last_id_this_date = max(map(int, ids_for_this_date)) # IDを数値として比較
                             if latest_processed_id_this_batch is None or last_id_this_date > latest_processed_id_this_batch:
                                 latest_processed_id_this_batch = last_id_this_date
                    except ApiError as e:
                        logging.error(f"[DROPBOX] アップロード失敗: {file_path}, Error: {e}", exc_info=True)
                        all_success_this_batch = False
                        for memo_id in ids_for_this_date: failed_ids_in_batch.add(memo_id) # アップロード失敗したIDをマーク
                    except Exception as e:
                        logging.error(f"[DROPBOX] アップロード中に予期せぬエラー: {file_path}, Error: {e}", exc_info=True)
                        all_success_this_batch = False
                        for memo_id in ids_for_this_date: failed_ids_in_batch.add(memo_id)

                # --- バッチ処理結果の判定と後処理 ---
                if all_success_this_batch:
                    logging.info("今回のバッチ処理はすべて成功しました。")
                    # 最後に処理したIDをDropboxに保存
                    if latest_processed_id_this_batch is not None:
                        try:
                            logging.info(f"[DROPBOX] 最終処理IDを保存: {latest_processed_id_this_batch}")
                            dbx.files_upload(str(latest_processed_id_this_batch).encode('utf-8'), LAST_PROCESSED_ID_FILE_PATH, mode=WriteMode('overwrite'))
                            logging.info(f"[PROCESS] 最終処理IDをDropboxに保存成功: {latest_processed_id_this_batch}")
                        except Exception as e:
                            logging.error(f"[DROPBOX] 最終処理IDの保存に失敗: {e}", exc_info=True)
                            # ID保存失敗は致命的ではないので続行するが、ログには残す
                    # 保留メモファイルを空にする
                    logging.info("処理が成功したため、保留メモファイルをクリアします。")
                    with open(PENDING_MEMOS_FILE, "w") as f:
                        json.dump([], f)
                    return True
                else:
                    logging.error("今回のバッチ処理で一部エラーが発生しました。")
                    # 失敗したメモだけを残してファイルを更新
                    remaining_memos = [memo for memo in memos if memo.get('id') in failed_ids_in_batch or memo.get('id') not in processed_ids_in_batch]
                    logging.info(f"{len(remaining_memos)} 件の未処理/失敗メモを残します。")
                    try:
                        with open(PENDING_MEMOS_FILE, "w", encoding="utf-8") as f:
                            json.dump(remaining_memos, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logging.error(f"失敗したメモのファイル更新中にエラー: {e}", exc_info=True)
                    return False # バッチの一部が失敗したので False を返す

            except AuthError as e:
                logging.error(f"[DROPBOX] Dropbox認証エラー: {e}", exc_info=True)
                return False # 認証エラーはリトライしても無駄な可能性が高いので失敗とする
            except ApiError as e:
                logging.error(f"[DROPBOX] Dropbox APIエラー（接続以外）: {e}", exc_info=True)
                return False # APIエラーも失敗とする
            except Exception as e:
                logging.error(f"[PROCESS] Dropbox処理中に予期せぬエラー: {e}", exc_info=True)
                return False # 予期せぬエラーも失敗とする
            finally:
                # Dropboxクライアントを閉じる (withを使わない場合)
                # if dbx: dbx.close()
                pass

    except TimeoutError:
        logging.error(f"ロックファイルの取得にタイムアウトしました: {lock_path}")
        return False # ロック失敗はリトライすべきなので False
    except Exception as e:
        logging.error(f"ロック処理またはファイル読み込み前の段階で予期せぬエラー: {e}", exc_info=True)
        return False # 予期せぬエラー


def main():
    """ワーカープロセスのメイン関数"""
    logging.info("--- 同期ワーカー起動 ---")
    success = False
    try:
        success = process_pending_memos()
    except Exception as e:
        # process_pending_memos 内で捕捉されなかった最上位のエラー
        logging.error(f"同期ワーカーのメイン処理で致命的なエラー: {e}", exc_info=True)
        success = False # 念のため False に

    if success:
        logging.info("--- 同期ワーカー正常完了 ---")
        sys.exit(0) # 正常終了
    else:
        logging.error("--- 同期ワーカーでエラーが発生または一部失敗しました ---")
        sys.exit(1) # 異常終了

if __name__ == "__main__":
    main()