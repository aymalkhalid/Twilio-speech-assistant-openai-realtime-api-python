"""
Broadcast call-record changes to connected dashboard clients (SSE).

Used after Supabase writes so the UI can refetch without waiting for the poll interval.
On multi-instance hosts (for example Cloud Run with several instances), only clients
connected to the same instance as the writer receive push events; the dashboard still
uses polling as a fallback.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

_loop: Optional[asyncio.AbstractEventLoop] = None
_subscribers: list[asyncio.Queue] = []
_guard = threading.Lock()


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def register_subscriber(queue: asyncio.Queue) -> None:
    with _guard:
        _subscribers.append(queue)


def unregister_subscriber(queue: asyncio.Queue) -> None:
    with _guard:
        if queue in _subscribers:
            _subscribers.remove(queue)


def _broadcast_to_subscribers() -> None:
    with _guard:
        queues = list(_subscribers)
    for queue in queues:
        try:
            queue.put_nowait(None)
        except Exception:
            pass


async def notify_call_records_changed_async() -> None:
    """Call from async code on the main event loop after a call record changed."""
    _broadcast_to_subscribers()


def notify_call_records_changed_threadsafe() -> None:
    """Call from a worker thread or sync code after a call record changed."""
    loop = _loop
    if loop is None or not loop.is_running():
        return
    loop.call_soon_threadsafe(_broadcast_to_subscribers)
