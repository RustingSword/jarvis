from __future__ import annotations

from loguru import logger

from jarvis.codex import CodexManager, CodexProcessError, CodexTimeoutError
from jarvis.messaging.messenger import Messenger
from jarvis.pipeline.prompt_builder import PromptBuilder
from jarvis.storage import Storage


class HeartbeatPipeline:
    def __init__(
        self,
        codex: CodexManager,
        storage: Storage,
        prompt_builder: PromptBuilder,
        messenger: Messenger,
    ) -> None:
        self._codex = codex
        self._storage = storage
        self._prompt_builder = prompt_builder
        self._messenger = messenger

    async def handle(self, chat_id: str, task_text: str) -> None:
        if not chat_id or not task_text:
            return

        try:
            prompt = await self._prompt_builder.build(str(task_text), [])
            result = await self._codex.run(prompt)
        except CodexTimeoutError:
            logger.warning("Codex heartbeat timed out")
            return
        except CodexProcessError as exc:
            logger.exception("Codex heartbeat failed: {}", exc)
            return

        response_text = result.response_text.strip() if result.response_text else ""
        if "HEARTBEAT_OK" in response_text:
            logger.debug("Heartbeat returned HEARTBEAT_OK; suppressing output")
            return

        session_record = None
        if result.thread_id:
            session_record = await self._storage.upsert_session(
                str(chat_id),
                result.thread_id,
                set_active=False,
            )

        session_id = session_record.session_id if session_record else None
        thread_id = session_record.thread_id if session_record else None

        if result.media:
            await self._messenger.send_media(
                str(chat_id),
                result.media,
                session_id=session_id,
                thread_id=thread_id,
            )

        response_text = response_text or "(无可用回复)"
        await self._messenger.send_markdown(
            str(chat_id),
            response_text,
            with_session_prefix=False,
            session_id=session_id,
            thread_id=thread_id,
            tts_hint=bool(result.tts_text),
            tts_text=result.tts_text,
        )
