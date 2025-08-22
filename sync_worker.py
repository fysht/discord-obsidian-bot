import sys
import os
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime
import zoneinfo
from filelock import FileLock
from dotenv import load_dotenv

# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.stdout.reconfigure(encoding='utf-8')

# --- 基本設定 ---
VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/var/data/vault"))
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
LAST_PROCESSED_TIMESTAMP_FILE = Path(os.getenv("LAST_PROCESSED_TIMESTAMP_FILE", "/var/data/last_processed_timestamp.txt"))
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
GIT_USER_NAME = os.getenv("GIT_USER_NAME", "Discord Memo Bot")
GIT_USER_EMAIL = os.getenv("GIT_USER_EMAIL", "bot@example.com")

def run_git_command(args, cwd=VAULT_PATH):
    """指定されたディレクトリでGitコマンドを実行し、成功したかを返す"""
    try:
        base_cmd = ["git", "-c", f"user.name={GIT_USER_NAME}", "-c", f"user.email={GIT_USER_EMAIL}"]
        cmd = base_cmd + args
        
        logging.info(f"[GIT] Running command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            cwd=cwd
        )
        logging.info(f"[GIT] {args[0]} successful:\n{result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"[GIT] Command failed (code: {e.returncode}): {' '.join(e.cmd)}\nSTDERR:\n{e.stderr}")
        return False
    except Exception as e:
        logging.error(f"[GIT] An unexpected error occurred: {e}", exc_info=True)
        return False

def initial_clone_if_needed():
    """保管庫の初期設定を行う。存在しない場合はクローンし、.gitがない場合は初期化して復元する"""
    GIT_REPO_URL = os.getenv("GIT_REPO_URL")
    if not GIT_REPO_URL:
        logging.critical("[GIT] GIT_REPO_URL environment variable is not set. Cannot proceed.")
        sys.exit(1)

    # Case 1: Vault directory doesn't exist. Clone it.
    if not VAULT_PATH.exists():
        logging.info(f"[GIT] Vault not found at {VAULT_PATH}. Cloning repository...")
        parent_dir = VAULT_PATH.parent
        parent_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["git", "clone", GIT_REPO_URL, str(VAULT_PATH)],
                check=True, capture_output=True, text=True, encoding='utf-8', cwd=parent_dir
            )
            logging.info("[GIT] Repository cloned successfully.")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"[GIT] Initial clone failed (code: {e.returncode}):\nSTDERR:\n{e.stderr}")
            sys.exit(1)

    # Case 2: Vault directory exists, but .git is missing (broken state). Initialize and restore.
    if not (VAULT_PATH / ".git").exists():
        logging.warning(f"[GIT] Vault exists at {VAULT_PATH}, but it's not a git repository. Attempting to restore...")
        
        if not run_git_command(["init"]): sys.exit(1)
        
        remotes_result = subprocess.run(["git", "remote"], capture_output=True, text=True, encoding='utf-8', cwd=VAULT_PATH)
        if 'origin' not in remotes_result.stdout:
            if not run_git_command(["remote", "add", "origin", GIT_REPO_URL]): sys.exit(1)
        
        if not run_git_command(["fetch", "origin"]): sys.exit(1)
        
        try:
            remote_info = subprocess.run(
                ["git", "remote", "show", "origin"],
                capture_output=True, text=True, check=True, encoding='utf-8', cwd=VAULT_PATH
            )
            default_branch = ""
            for line in remote_info.stdout.split('\n'):
                if "HEAD branch" in line:
                    default_branch = line.split(':')[1].strip()
                    break
            
            if not default_branch:
                logging.error("[GIT] Could not determine the default branch from remote 'origin'.")
                sys.exit(1)

            logging.info(f"[GIT] Default branch is '{default_branch}'. Resetting local state to match remote.")
            if not run_git_command(["reset", "--hard", f"origin/{default_branch}"]): sys.exit(1)
            
        except subprocess.CalledProcessError as e:
            logging.error(f"[GIT] Failed to determine default branch (code: {e.returncode}):\nSTDERR:\n{e.stderr}")
            sys.exit(1)

    logging.info(f"[GIT] Vault repository found at {VAULT_PATH}.")
    return True

def sync_with_git_repo():
    """Gitリポジトリと同期を行う (Pull)"""
    if not run_git_command(["pull", "--ff-only"]):
        logging.error("[GIT] Pull failed. Aborting sync.")
        return False
    return True
    
def push_to_git_repo():
    """変更をGitリポジトリにPushする"""
    if not run_git_command(["add", "."]):
        return False
    
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, encoding='utf-8', cwd=VAULT_PATH
    )
    if not status_result.stdout.strip():
        logging.info("[GIT] No changes to commit.")
        return True

    commit_message = f"docs: Add new memos from Discord ({datetime.now(JST).strftime('%Y-%m-%d %H:%M')})"
    if not run_git_command(["commit", "-m", commit_message]):
        return False
        
    if not run_git_command(["push"]):
        return False
        
    return True

def process_pending_memos():
    """保留メモを日付ごとの DailyNote に追加する"""
    if not PENDING_MEMOS_FILE.exists():
        return True, False 

    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    memos_processed = False
    last_processed_timestamp = None
    with lock:
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                memos = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            memos = []
            
        if not memos:
            return True, False

        logging.info(f"[PROCESS] {len(memos)} 件のメモを処理...")
        memos_by_date = {}
        for memo in memos:
            try:
                timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                date_str = timestamp_utc.astimezone(JST).strftime('%Y-%m-%d')
                memos_by_date.setdefault(date_str, []).append(memo)
            except (KeyError, ValueError):
                pass

        daily_notes_path = VAULT_PATH / "DailyNotes"
        daily_notes_path.mkdir(parents=True, exist_ok=True)

        for date_str, memos_in_date in memos_by_date.items():
            try:
                file_path = daily_notes_path / f"{date_str}.md"
                lines_to_append = []
                for memo in sorted(memos_in_date, key=lambda m: m['created_at']):
                    time_str = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00')).astimezone(JST).strftime('%H:%M')
                    content_lines = memo['content'].strip().split('\n')
                    formatted_content = f"- ({memo.get('author','')}) [{memo.get('message_id','?')}] {content_lines[0]}"
                    if len(content_lines) > 1:
                        formatted_content += "\n" + "\n".join([f"\t- {line}" for line in content_lines[1:]])
                    lines_to_append.append(f"- {time_str}\n\t{formatted_content}")
                    
                with open(file_path, "a", encoding="utf-8") as f:
                    if f.tell() > 0:
                        f.write("\n")
                    f.write("\n".join(lines_to_append))
                memos_processed = True
            except Exception as e:
                logging.error(f"[PROCESS] ファイル書き込みエラー: {e}", exc_info=True)
                return False, False
        
        if memos:
            last_processed_timestamp = memos[-1]['created_at']
        
        if last_processed_timestamp:
            try:
                LAST_PROCESSED_TIMESTAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
                new_ts_obj = datetime.fromisoformat(last_processed_timestamp.replace('Z', '+00:00'))
                
                should_write = True
                if LAST_PROCESSED_TIMESTAMP_FILE.exists():
                    try:
                        existing_ts_str = LAST_PROCESSED_TIMESTAMP_FILE.read_text().strip()
                        if existing_ts_str:
                            existing_ts_obj = datetime.fromisoformat(existing_ts_str.replace('Z', '+00:00'))
                            if new_ts_obj <= existing_ts_obj:
                                should_write = False
                    except Exception:
                        pass
                
                if should_write:
                    with open(LAST_PROCESSED_TIMESTAMP_FILE, "w", encoding="utf-8") as f:
                        f.write(last_processed_timestamp)
                    logging.info(f"[PROCESS] 最終処理タイムスタンプを更新しました: {last_processed_timestamp}")
                else:
                    logging.info("[PROCESS] 既存のタイムスタンプが新しいため、更新をスキップしました。")

            except Exception as e:
                logging.error(f"[PROCESS] 最終処理タイムスタンプの保存に失敗: {e}")

        try:
            with open(PENDING_MEMOS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
        except OSError as e:
            logging.error(f"[PROCESS] pending_memos.json のクリアに失敗: {e}")
            pass
            
    return True, memos_processed

def main():
    if not initial_clone_if_needed():
        sys.exit(1)
    
    if not sync_with_git_repo():
        sys.exit(1)
        
    success, has_changes = process_pending_memos()
    if not success:
        sys.exit(1)
        
    if has_changes:
        if not push_to_git_repo():
            sys.exit(1)
    else:
        logging.info("--- メモの変更がなかったため、Pushは行いません ---")

    logging.info("--- 同期ワーカー正常完了 ---")

if __name__ == "__main__":
    main()