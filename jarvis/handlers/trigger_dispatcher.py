from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol

from loguru import logger

from jarvis.event_bus import Event
from jarvis.formatting import normalize_verbosity

EventEnqueuer = Callable[[Event], Awaitable[None]]


class RssRunner(Protocol):
    async def run(self, chat_id: str) -> None: ...


class TriggerDispatcher:
    def __init__(
        self,
        enqueue_message: EventEnqueuer,
        *,
        rss_runner: RssRunner | None = None,
    ) -> None:
        self._enqueue_message = enqueue_message
        self._rss_runner = rss_runner

    async def handle(self, event: Event) -> None:
        payload = event.payload
        trigger_type = payload.get("type")
        if not trigger_type:
            logger.debug("Trigger missing type: {}", payload)
            return

        if trigger_type == "monitor":
            await self._handle_monitor(payload)
            return
        if trigger_type == "schedule":
            await self._handle_schedule(payload)
            return
        if trigger_type == "webhook":
            await self._handle_webhook(payload)
            return

        logger.debug("Unhandled trigger: {}", payload)

    async def _handle_monitor(self, payload: dict) -> None:
        chat_id = payload.get("chat_id")
        message = (
            f"监控告警: {payload.get('name')} "
            f"{payload.get('metric')}={payload.get('value')} "
            f"(阈值 {payload.get('threshold')})"
        )
        await self._dispatch_to_codex(chat_id, message)

    async def _handle_schedule(self, payload: dict) -> None:
        chat_id = payload.get("chat_id")
        action = payload.get("action")
        if action == "rss":
            if not chat_id:
                logger.warning("RSS schedule missing chat_id.")
                return
            if not self._rss_runner:
                logger.warning("RSS runner not configured; skipping schedule.")
                return
            await self._rss_runner.run(str(chat_id))
            return
        message = payload.get("message") or f"计划触发: {payload.get('name')}"
        verbosity = None
        raw_verbosity = payload.get("verbosity")
        if raw_verbosity:
            verbosity = normalize_verbosity(str(raw_verbosity))
        verbosity = verbosity or "result"
        await self._dispatch_to_codex(chat_id, str(message), verbosity=verbosity)

    async def _handle_webhook(self, payload: dict) -> None:
        webhook_payload = payload.get("payload")
        logger.info("Webhook fired: {}", webhook_payload)
        if isinstance(webhook_payload, dict):
            chat_id = webhook_payload.get("chat_id")
            message = webhook_payload.get("message") or webhook_payload.get("text")
            await self._dispatch_to_codex(chat_id, str(message) if message else "")

    async def _dispatch_to_codex(
        self, chat_id: str | None, message: str, *, verbosity: str | None = None
    ) -> None:
        if not chat_id:
            logger.warning("Trigger missing chat_id, skipping: {}", message)
            return
        if not message:
            logger.warning("Trigger missing message for chat_id={}", chat_id)
            return
        event = Event(
            type="trigger.message",
            payload={
                "chat_id": str(chat_id),
                "text": message,
                "source": "trigger",
                "verbosity": verbosity,
            },
            created_at=datetime.now(timezone.utc),
        )
        await self._enqueue_message(event)
