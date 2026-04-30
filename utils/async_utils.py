import asyncio
import logging

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def safe_create_task(coro, name: str = "") -> asyncio.Task:
    """fire-and-forget な asyncio.Task を生成しつつ、例外をログに残す。
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

    task.add_done_callback(_on_done)
    return task
