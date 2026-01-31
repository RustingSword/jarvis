from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from jarvis.event_bus import Event
from jarvis.events import TELEGRAM_MESSAGE_RECEIVED


@dataclass(slots=True)
class PendingMessageBundle:
    chat_id: str
    user_id: str
    text_parts: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    last_message_id: int | None = None
    media_group_id: str | None = None
    reply_to_message_id: int | None = None
    flush_task: asyncio.Task | None = None

    def add_payload(self, payload: dict) -> None:
        text = (payload.get("text") or "").strip()
        if text:
            self.text_parts.append(text)
        attachments = payload.get("attachments") or []
        if attachments:
            self.attachments.extend(list(attachments))
        message_id = payload.get("message_id")
        if isinstance(message_id, int):
            self.last_message_id = message_id
        media_group_id = payload.get("media_group_id")
        if media_group_id:
            self.media_group_id = str(media_group_id)
        reply_to_message_id = payload.get("reply_to_message_id")
        if isinstance(reply_to_message_id, int):
            self.reply_to_message_id = reply_to_message_id

    def build_payload(self) -> dict:
        text = "\n".join(part for part in self.text_parts if part.strip())
        return {
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "text": text,
            "message_id": self.last_message_id,
            "media_group_id": self.media_group_id,
            "reply_to_message_id": self.reply_to_message_id,
            "attachments": list(self.attachments),
            "bundle_count": len(self.text_parts) + len(self.attachments),
        }


class MessageBundler:
    def __init__(
        self,
        wait_seconds: float,
        enqueue: Callable[[Event], Awaitable[None]],
    ) -> None:
        self._wait_seconds = max(0.0, float(wait_seconds))
        self._enqueue = enqueue
        self._pending: dict[str, PendingMessageBundle] = {}
        self._lock = asyncio.Lock()

    async def handle_event(self, event: Event) -> None:
        if self._wait_seconds <= 0:
            await self._enqueue(event)
            return
        await self._collect(event)

    async def flush_all(self) -> None:
        async with self._lock:
            keys = list(self._pending.keys())
        for key in keys:
            await self._flush(key)

    def _bundle_key(self, payload: dict) -> str:
        chat_id = payload.get("chat_id") or ""
        user_id = payload.get("user_id") or ""
        return f"{chat_id}:{user_id}"

    async def _collect(self, event: Event) -> None:
        payload = event.payload
        chat_id = payload.get("chat_id")
        if not chat_id:
            return
        key = self._bundle_key(payload)
        async with self._lock:
            bundle = self._pending.get(key)
            if not bundle:
                bundle = PendingMessageBundle(
                    chat_id=str(chat_id),
                    user_id=str(payload.get("user_id") or ""),
                )
                self._pending[key] = bundle
            bundle.add_payload(payload)
            if bundle.flush_task:
                bundle.flush_task.cancel()
            bundle.flush_task = asyncio.create_task(self._flush_after(key, self._wait_seconds))

    async def _flush_after(self, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._flush(key)

    async def _flush(self, key: str) -> None:
        async with self._lock:
            bundle = self._pending.pop(key, None)
        if not bundle:
            return
        if bundle.flush_task:
            bundle.flush_task.cancel()
        payload = bundle.build_payload()
        event = Event(
            type=TELEGRAM_MESSAGE_RECEIVED,
            payload=payload,
            created_at=datetime.now(timezone.utc),
        )
        await self._enqueue(event)
