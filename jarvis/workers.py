from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from jarvis.event_bus import Event

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]


class QueueWorker:
    def __init__(self, handler: EventHandler, *, name: str | None = None) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._name = name or "queue-worker"

    async def start(self) -> None:
        if self._task:
            return
        self._task = asyncio.create_task(self._run(), name=self._name)

    async def stop(self) -> None:
        if not self._task:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def enqueue(self, event: Event) -> None:
        await self._queue.put(event)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            try:
                await self._handler(event)
            except Exception:
                logger.exception("Worker failed to handle event")
            finally:
                self._queue.task_done()
