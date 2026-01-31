from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from jarvis.event_bus import Event

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]


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

    async def start(self) -> None:
        if self._tasks:
            return
        for idx in range(self._concurrency):
            task_name = f"{self._name}-{idx+1}"
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
