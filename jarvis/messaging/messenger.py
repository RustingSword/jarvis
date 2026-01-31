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
        session_id: int | None = None,
        thread_id: str | None = None,
    ) -> None:
        final_text = text
        if with_session_prefix:
            final_text = await self._with_session_prefix(
                chat_id,
                text,
                with_separator=with_separator,
                session_id=session_id,
            )
        payload: dict[str, object] = {"chat_id": chat_id, "text": final_text}
        meta: dict[str, object] = {}
        if session_id is not None:
            meta["session_id"] = int(session_id)
        if thread_id:
            meta["thread_id"] = thread_id
        if meta:
            payload["meta"] = meta
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
        with_session_prefix: bool = True,
        session_id: int | None = None,
        thread_id: str | None = None,
    ) -> None:
        await self.send_message(
            chat_id,
            text,
            markdown=True,
            with_separator=with_separator,
            with_session_prefix=with_session_prefix,
            session_id=session_id,
            thread_id=thread_id,
        )

    async def send_media(
        self,
        chat_id: str,
        media: list[dict],
        *,
        text: str | None = None,
        markdown: bool = False,
        session_id: int | None = None,
        thread_id: str | None = None,
    ) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "media": media}
        if text:
            payload["text"] = text
        if markdown:
            payload["markdown"] = True
        meta: dict[str, object] = {}
        if session_id is not None:
            meta["session_id"] = int(session_id)
        if thread_id:
            meta["thread_id"] = thread_id
        if meta:
            payload["meta"] = meta
        await self._event_bus.publish(TELEGRAM_SEND, payload)

    async def _with_session_prefix(
        self,
        chat_id: str,
        text: str,
        *,
        with_separator: bool = True,
        session_id: int | None = None,
    ) -> str:
        active_session = await self._storage.get_session(chat_id)
        active_id = active_session.session_id if active_session else None
        if session_id is None:
            if not active_session:
                return text
            session_id = active_session.session_id
        is_active = active_id is not None and int(session_id) == int(active_id)
        suffix = "*" if is_active else ""
        bare_prefix = f"[{int(session_id)}{suffix}]"
        prefix = f"> Session {bare_prefix}"
        stripped = text.lstrip()
        if stripped.startswith(prefix) or stripped.startswith(bare_prefix):
            return text
        if with_separator:
            return f"{prefix}\n\n------\n\n{text}"
        return f"{prefix}\n\n{text}"
