# obsidian_handler.py

import os
from datetime import datetime
import asyncio

# 書き込み先をデスクトップのテストフォルダに固定する
def _get_vault_path() -> str | None:
    # あなたのユーザー名に合わせてパスを調整してください
    return 'C:\\Users\\fyshx\\Desktop\\bot_test_output'

# --- 以降のコードは前回と同じ ---

async def _write_to_file_async(path: str, content: str):
    def write_operation():
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    await asyncio.to_thread(write_operation)

async def _read_from_file_async(path: str) -> str:
    def read_operation():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return await asyncio.to_thread(read_operation)

async def append_to_daily_note(content: str) -> bool:
    vault_path = _get_vault_path()
    if not vault_path:
        print("エラー: テスト用のパスが設定されていません。")
        return False

    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        file_name = f"{today_str}.md"
        file_path = os.path.join(vault_path, file_name)
        
        post_content = f"\n{content}"
        await _write_to_file_async(file_path, post_content)
        
        print("テストフォルダへの書き込み成功")
        return True
    except Exception as e:
        print(f"テストフォルダへのファイル書き込みに失敗しました: {e}")
        return False

async def read_daily_note() -> str | None:
    vault_path = _get_vault_path()
    if not vault_path:
        return None

    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        file_name = f"{today_str}.md"
        file_path = os.path.join(vault_path, file_name)
        
        if not os.path.exists(file_path):
            return ""

        content = await _read_from_file_async(file_path)
        return content
    except Exception as e:
        print(f"テストフォルダからのファイル読み込みに失敗しました: {e}")
        return None