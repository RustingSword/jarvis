from __future__ import annotations

import logging

from jarvis.event_bus import Event
from jarvis.storage import Storage

logger = logging.getLogger(__name__)


class MessageSentHandler:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def handle(self, event: Event) -> None:
        payload = event.payload
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        session_id = payload.get("session_id")
        thread_id = payload.get("thread_id")
        if not chat_id or message_id is None or session_id is None or not thread_id:
            return
        try:
            await self._storage.save_message_session(
                str(chat_id), int(message_id), int(session_id), str(thread_id)
            )
        except Exception:
            logger.exception("Failed to save message session mapping")
