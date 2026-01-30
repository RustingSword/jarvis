from __future__ import annotations

import logging

from jarvis.codex import CodexProcessError, CodexTimeoutError, CodexManager
from jarvis.event_bus import Event
from jarvis.handlers.progress import CodexProgressHandler
from jarvis.messaging.messenger import Messenger
from jarvis.pipeline.prompt_builder import PromptBuilder
from jarvis.storage import Storage
from jarvis.verbosity import VerbosityManager

logger = logging.getLogger(__name__)


class MessagePipeline:
    def __init__(
        self,
        codex: CodexManager,
        storage: Storage,
        prompt_builder: PromptBuilder,
        progress_handler: CodexProgressHandler,
        messenger: Messenger,
        verbosity: VerbosityManager,
    ) -> None:
        self._codex = codex
        self._storage = storage
        self._prompt_builder = prompt_builder
        self._progress = progress_handler
        self._messenger = messenger
        self._verbosity = verbosity

    async def handle(self, event: Event) -> None:
        chat_id = event.payload.get("chat_id")
        text = event.payload.get("text", "")
        attachments = list(event.payload.get("attachments") or [])
        if not chat_id or (not text and not attachments):
            return

        await self._verbosity.ensure(chat_id)
        session = await self._storage.get_session(chat_id)
        thread_id = session.thread_id if session else None

        async def progress_callback(codex_event: dict) -> None:
            await self._progress.handle(chat_id, codex_event)

        try:
            prompt = await self._prompt_builder.build(text, attachments)
            result = await self._codex.run(
                prompt,
                session_id=thread_id,
                progress_callback=progress_callback,
            )
        except CodexTimeoutError:
            logger.warning("Codex timed out")
            await self._messenger.send_message(chat_id, "Codex 调用超时，请稍后再试。")
            return
        except CodexProcessError as exc:
            logger.exception("Codex run failed")
            await self._messenger.send_message(chat_id, f"Codex 调用失败: {exc}")
            return

        if result.thread_id:
            await self._storage.upsert_session(chat_id, result.thread_id)

        if result.media:
            await self._messenger.send_media(chat_id, result.media)

        response_text = result.response_text or "(无可用回复)"
        await self._messenger.send_markdown(chat_id, response_text)
