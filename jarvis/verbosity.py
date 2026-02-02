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

    def show_tool_messages(self, chat_id: str, override: str | None = None) -> bool:
        return self._resolve(chat_id, override) == "full"

    def show_reasoning_messages(self, chat_id: str, override: str | None = None) -> bool:
        return self._resolve(chat_id, override) in {"full", "compact"}

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

    def _resolve(self, chat_id: str, override: str | None) -> str:
        if override:
            normalized = normalize_verbosity(override)
            if normalized:
                return normalized
        return self.get(chat_id)
