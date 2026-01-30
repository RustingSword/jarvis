from __future__ import annotations

from jarvis.event_bus import EventBus
from jarvis.events import TELEGRAM_SEND
from jarvis.storage import Storage


class Messenger:
    def __init__(self, event_bus: EventBus, storage: Storage) -> None:
        self._event_bus = event_bus
        self._storage = storage

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        with_separator: bool = True,
        markdown: bool = False,
        parse_mode: str | None = None,
        with_session_prefix: bool = True,
    ) -> None:
        final_text = text
        if with_session_prefix:
            final_text = await self._with_session_prefix(chat_id, text, with_separator=with_separator)
        payload: dict[str, object] = {"chat_id": chat_id, "text": final_text}
        if markdown:
            payload["markdown"] = True
        if parse_mode:
            payload["parse_mode"] = parse_mode
        await self._event_bus.publish(TELEGRAM_SEND, payload)

    async def send_markdown(
        self,
        chat_id: str,
        text: str,
        *,
        with_separator: bool = True,
    ) -> None:
        await self.send_message(chat_id, text, markdown=True, with_separator=with_separator)

    async def send_media(
        self,
        chat_id: str,
        media: list[dict],
        *,
        text: str | None = None,
        markdown: bool = False,
    ) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "media": media}
        if text:
            payload["text"] = text
        if markdown:
            payload["markdown"] = True
        await self._event_bus.publish(TELEGRAM_SEND, payload)

    async def _with_session_prefix(
        self,
        chat_id: str,
        text: str,
        *,
        with_separator: bool = True,
    ) -> str:
        session = await self._storage.get_session(chat_id)
        if not session:
            return text
        bare_prefix = f"[{session.session_id}]"
        prefix = f"> Session {bare_prefix}"
        stripped = text.lstrip()
        if stripped.startswith(prefix) or stripped.startswith(bare_prefix):
            return text
        if with_separator:
            return f"{prefix}\n\n------\n\n{text}"
        return f"{prefix}\n\n{text}"
