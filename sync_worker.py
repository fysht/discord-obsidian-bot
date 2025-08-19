import os
import sys
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
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
GIT_USER_NAME = os.getenv("GIT_USER_NAME", "Discord Memo Bot")
GIT_USER_EMAIL = os.getenv("GIT_USER_EMAIL", "bot@example.com")

def run_git_command(args, cwd=VAULT_PATH):
    """指定されたディレクトリでGitコマンドを実行し、成功したかを返す"""
    try:
        # Gitコマンドの基本設定を追加
        base_cmd = ["git", "-c", f"user.name='{GIT_USER_NAME}'", "-c", f"user.email='{GIT_USER_EMAIL}'"]
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
                    formatted_content = f"- {content_lines[0]}"
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

        try:
            PENDING_MEMOS_FILE.unlink()
        except OSError as e:
            logging.error(f"[PROCESS] pending_memos.json の削除に失敗: {e}")
            pass
            
    return True, memos_processed

def main():
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