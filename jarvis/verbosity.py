from __future__ import annotations

from jarvis.formatting import normalize_verbosity
from jarvis.storage import Storage


class VerbosityManager:
    def __init__(self, storage: Storage, default_verbosity: str) -> None:
        self._storage = storage
        self._default = normalize_verbosity(default_verbosity) or "full"
        self._by_chat: dict[str, str] = {}

    async def ensure(self, chat_id: str) -> None:
        if chat_id in self._by_chat:
            return
        stored = await self._storage.get_setting(chat_id, "verbosity")
        normalized = normalize_verbosity(stored) if stored else None
        self._by_chat[chat_id] = normalized or self._default

    def get(self, chat_id: str) -> str:
        return self._by_chat.get(chat_id, self._default)

    def show_tool_messages(self, chat_id: str) -> bool:
        return self.get(chat_id) not in {"compact", "minimal"}

    async def set(self, chat_id: str, value: str) -> str:
        normalized = normalize_verbosity(value)
        if not normalized:
            raise ValueError("invalid verbosity")
        self._by_chat[chat_id] = normalized
        await self._storage.set_setting(chat_id, "verbosity", normalized)
        return normalized

    async def reset(self, chat_id: str) -> None:
        await self._storage.delete_setting(chat_id, "verbosity")
        self._by_chat[chat_id] = self._default

    @property
    def default(self) -> str:
        return self._default
