"""Obsidian DailyNote への共通追記ユーティリティ。
食事ログ・支出など、複数ルーターから同じパターンで使われる。"""

import logging

from utils.obsidian_utils import update_section


async def append_lifelog_line(date_str: str, line: str) -> bool:
    """指定日 (YYYY-MM-DD) の DailyNote の `## 🪟 Lifelog` セクションに 1 行追記する。
    成功時 True、未接続/失敗時 False。例外は内部でログのみ。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return False
    drive = chat_service.drive_service
    service = drive.get_service()
    if not service:
        return False
    try:
        folder_id = await drive.find_file(service, chat_service.drive_folder_id, "DailyNotes")
        if not folder_id:
            folder_id = await drive.create_folder(service, chat_service.drive_folder_id, "DailyNotes")
        filename = f"{date_str}.md"
        file_id = await drive.find_file(service, folder_id, filename)
        if file_id:
            content = await drive.read_text_file(service, file_id)
        else:
            content = f"---\ndate: {date_str}\n---\n\n# Daily Note {date_str}\n"
        new_content = update_section(content, line, "## 🪟 Lifelog")
        if file_id:
            await drive.update_text(service, file_id, new_content)
        else:
            await drive.upload_text(service, folder_id, filename, new_content)
        return True
    except Exception as e:
        logging.error(f"append_lifelog_line({date_str}) failed: {e}")
        return False
