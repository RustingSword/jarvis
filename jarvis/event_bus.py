from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any]
    created_at: datetime


EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: Dict[str, List[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        event = Event(type=event_type, payload=payload, created_at=datetime.now(timezone.utc))
        handlers = list(self._subscribers.get(event_type, []))
        if not handlers:
            logger.debug("No subscribers for event: %s", event_type)
            return

        tasks = [asyncio.create_task(self._safe_call(handler, event)) for handler in handlers]
        await asyncio.gather(*tasks)

    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception("Error handling event '%s' with %s", event.type, handler)
