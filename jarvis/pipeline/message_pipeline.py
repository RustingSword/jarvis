from __future__ import annotations

import logging

from jarvis.audio.transcriber import TranscriptionService
from jarvis.codex import CodexManager, CodexProcessError, CodexTimeoutError
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
        transcriber: TranscriptionService | None = None,
    ) -> None:
        self._codex = codex
        self._storage = storage
        self._prompt_builder = prompt_builder
        self._progress = progress_handler
        self._messenger = messenger
        self._verbosity = verbosity
        self._transcriber = transcriber

    async def handle(self, event: Event) -> None:
        chat_id = event.payload.get("chat_id")
        text = event.payload.get("text", "")
        attachments = list(event.payload.get("attachments") or [])
        reply_to_message_id = event.payload.get("reply_to_message_id")
        is_trigger = event.payload.get("source") == "trigger"
        if not chat_id or (not text and not attachments):
            return

        if self._transcriber:
            text, attachments = await self._transcriber.process(text, attachments)
            if not text and not attachments:
                return

        await self._verbosity.ensure(chat_id)
        message_session = None
        if reply_to_message_id:
            message_session = await self._storage.get_message_session(chat_id, reply_to_message_id)
            if message_session:
                await self._storage.activate_session(chat_id, message_session.session_id)

        active_session = await self._storage.get_session(chat_id)
        thread_id = None
        if not is_trigger:
            if message_session:
                thread_id = message_session.thread_id
            elif active_session:
                thread_id = active_session.thread_id

        activate_on_complete = (
            (not is_trigger) and (message_session is None) and (active_session is None)
        )

        verbosity_override = None
        if is_trigger:
            raw_override = event.payload.get("verbosity")
            if raw_override:
                verbosity_override = str(raw_override)

        progress_state = {"session_id": None, "thread_id": None}
        if message_session:
            progress_state["session_id"] = message_session.session_id
            progress_state["thread_id"] = message_session.thread_id
        elif active_session:
            progress_state["session_id"] = active_session.session_id
            progress_state["thread_id"] = active_session.thread_id

        async def progress_callback(codex_event: dict) -> None:
            if codex_event.get("type") == "thread.started":
                thread_id = codex_event.get("thread_id")
                if thread_id:
                    record = await self._storage.upsert_session(
                        chat_id,
                        str(thread_id),
                        set_active=False,
                    )
                    progress_state["session_id"] = record.session_id
                    progress_state["thread_id"] = record.thread_id
            await self._progress.handle(
                chat_id,
                codex_event,
                session_id=progress_state.get("session_id"),
                verbosity_override=verbosity_override,
            )

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

        session_record = None
        if result.thread_id:
            session_record = await self._storage.upsert_session(
                chat_id,
                result.thread_id,
                set_active=activate_on_complete and not is_trigger,
            )

        session_id_for_response = None
        thread_id_for_response = None
        if session_record:
            session_id_for_response = session_record.session_id
            thread_id_for_response = session_record.thread_id
        elif message_session:
            session_id_for_response = message_session.session_id
            thread_id_for_response = message_session.thread_id
        elif active_session:
            session_id_for_response = active_session.session_id
            thread_id_for_response = active_session.thread_id

        if result.media:
            await self._messenger.send_media(
                chat_id,
                result.media,
                session_id=session_id_for_response,
                thread_id=thread_id_for_response,
            )

        response_text = result.response_text or "(无可用回复)"
        await self._messenger.send_markdown(
            chat_id,
            response_text,
            with_session_prefix=not is_trigger,
            session_id=session_id_for_response,
            thread_id=thread_id_for_response,
        )
