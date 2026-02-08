from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from jarvis.event_bus import Event

EventHandler = Callable[[Event], Awaitable[None]]


@dataclass(slots=True)
class ActiveTaskSnapshot:
    event_type: str
    started_at: datetime
    summary: str
    session_id: str | None
    chat_id: str | None


class QueueWorker:
    def __init__(
        self,
        handler: EventHandler,
        *,
        name: str | None = None,
        concurrency: int = 1,
    ) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._name = name or "queue-worker"
        self._concurrency = max(1, int(concurrency))
        self._active: dict[str, ActiveTaskSnapshot] = {}
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        if self._tasks:
            return
        for idx in range(self._concurrency):
            task_name = f"{self._name}-{idx + 1}"
            self._tasks.append(asyncio.create_task(self._run(), name=task_name))

    async def stop(self) -> None:
        if not self._tasks:
            return
        for _ in self._tasks:
            await self._queue.put(None)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def enqueue(self, event: Event) -> None:
        await self._queue.put(event)

    def pending_count(self) -> int:
        return int(self._queue.qsize())

    async def snapshot(self) -> list[ActiveTaskSnapshot]:
        async with self._lock:
            return list(self._active.values())

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            task_id = asyncio.current_task()
            task_name = task_id.get_name() if task_id else None
            snapshot = ActiveTaskSnapshot(
                event_type=str(event.type),
                started_at=datetime.now(timezone.utc),
                summary=_summarize_event(event),
                session_id=_extract_session_id(event),
                chat_id=_extract_chat_id(event),
            )
            async with self._lock:
                if task_name:
                    self._active[task_name] = snapshot
            try:
                await self._handler(event)
            except Exception:
                logger.exception("Worker failed to handle event")
            finally:
                async with self._lock:
                    if task_name:
                        self._active.pop(task_name, None)
                self._queue.task_done()


def _summarize_event(event: Event) -> str:
    payload: dict[str, Any] = event.payload or {}
    if event.type == "command.task":
        return _truncate(str(payload.get("task") or ""))
    if event.type == "command.compact":
        session_id = payload.get("session_id")
        if session_id is not None:
            return _truncate(f"compact session_id={session_id}")
        return "compact"
    if event.type == "telegram.command":
        raw = payload.get("raw_text")
        if raw:
            return _truncate(str(raw))
        command = payload.get("command")
        args = payload.get("args") or []
        if command:
            return _truncate("/" + str(command) + (" " + " ".join(args) if args else ""))
    if event.type in {"telegram.message_received", "trigger.message", "command.task"}:
        text = payload.get("text")
        if text:
            return _truncate(str(text))
    return _truncate(str(payload.get("name") or payload.get("action") or event.type))


def _extract_session_id(event: Event) -> str | None:
    payload: dict[str, Any] = event.payload or {}
    session_id = payload.get("session_id")
    if session_id is None:
        return None
    return str(session_id)


def _extract_chat_id(event: Event) -> str | None:
    payload: dict[str, Any] = event.payload or {}
    chat_id = payload.get("chat_id")
    if chat_id is None:
        return None
    return str(chat_id)


def _truncate(value: str, limit: int = 120) -> str:
    cleaned = " ".join(value.strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"
