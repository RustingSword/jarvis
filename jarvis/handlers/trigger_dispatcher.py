from __future__ import annotations

import logging

from jarvis.event_bus import Event
from jarvis.messaging.messenger import Messenger

logger = logging.getLogger(__name__)


class TriggerDispatcher:
    def __init__(self, messenger: Messenger) -> None:
        self._messenger = messenger

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
        if chat_id:
            await self._messenger.send_message(str(chat_id), message)

    async def _handle_schedule(self, payload: dict) -> None:
        chat_id = payload.get("chat_id")
        message = payload.get("message") or f"计划触发: {payload.get('name')}"
        if chat_id:
            await self._messenger.send_message(str(chat_id), str(message))

    async def _handle_webhook(self, payload: dict) -> None:
        webhook_payload = payload.get("payload")
        logger.info("Webhook fired: %s", webhook_payload)
        if isinstance(webhook_payload, dict):
            chat_id = webhook_payload.get("chat_id")
            message = webhook_payload.get("message")
            if chat_id and message:
                await self._messenger.send_message(str(chat_id), str(message))
