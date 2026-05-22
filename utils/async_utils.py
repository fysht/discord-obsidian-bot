import asyncio
import logging
import traceback as _tb

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def safe_create_task(coro, name: str = "") -> asyncio.Task:
    """fire-and-forget な asyncio.Task を生成しつつ、例外をログ + DB error_log に残す。
    呼び出し側がタスクを保持しなくても GC されないように内部 set に保持し、
    完了時に discard する。"""
    task = asyncio.create_task(coro, name=name or getattr(coro, "__name__", "bg-task"))
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logging.error(
                f"[BG '{t.get_name()}'] バックグラウンドタスクが失敗しました: {exc}",
                exc_info=exc,
            )
            # DB error_log にも記録（後でアプリから一覧確認できるように）
            try:
                from api.database import record_error
                tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
                asyncio.create_task(
                    record_error(f"bg:{t.get_name()}", str(exc), tb_text)
                )
            except Exception:
                pass

    task.add_done_callback(_on_done)
    return task
