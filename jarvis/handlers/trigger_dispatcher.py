from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from jarvis.event_bus import Event

logger = logging.getLogger(__name__)

EventEnqueuer = Callable[[Event], Awaitable[None]]


class TriggerDispatcher:
    def __init__(self, enqueue_message: EventEnqueuer) -> None:
        self._enqueue_message = enqueue_message

    async def handle(self, event: Event) -> None:
        payload = event.payload
        trigger_type = payload.get("type")
        if not trigger_type:
            logger.debug("Trigger missing type: %s", payload)
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

        logger.debug("Unhandled trigger: %s", payload)

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
        message = payload.get("message") or f"计划触发: {payload.get('name')}"
        await self._dispatch_to_codex(chat_id, str(message))

    async def _handle_webhook(self, payload: dict) -> None:
        webhook_payload = payload.get("payload")
        logger.info("Webhook fired: %s", webhook_payload)
        if isinstance(webhook_payload, dict):
            chat_id = webhook_payload.get("chat_id")
            message = webhook_payload.get("message") or webhook_payload.get("text")
            await self._dispatch_to_codex(chat_id, str(message) if message else "")

    async def _dispatch_to_codex(self, chat_id: str | None, message: str) -> None:
        if not chat_id:
            logger.warning("Trigger missing chat_id, skipping: %s", message)
            return
        if not message:
            logger.warning("Trigger missing message for chat_id=%s", chat_id)
            return
        event = Event(
            type="trigger.message",
            payload={"chat_id": str(chat_id), "text": message},
            created_at=datetime.now(timezone.utc),
        )
        await self._enqueue_message(event)
