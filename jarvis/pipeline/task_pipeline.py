from __future__ import annotations

from loguru import logger

from jarvis.codex import CodexManager, CodexProcessError, CodexTimeoutError
from jarvis.event_bus import Event
from jarvis.messaging.messenger import Messenger
from jarvis.pipeline.prompt_builder import PromptBuilder
from jarvis.storage import Storage


class TaskPipeline:
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

    async def handle(self, event: Event) -> None:
        payload = event.payload
        chat_id = payload.get("chat_id")
        task_text = payload.get("task")
        if not chat_id or not task_text:
            return

        session_record = None
        start_notified = False

        async def progress_callback(codex_event: dict) -> None:
            nonlocal session_record, start_notified
            if codex_event.get("type") != "thread.started":
                return
            thread_id = codex_event.get("thread_id")
            if not thread_id or session_record:
                return
            session_record = await self._storage.upsert_session(
                str(chat_id), str(thread_id), set_active=True
            )
            if start_notified:
                return
            start_notified = True
            await self._messenger.send_markdown(
                str(chat_id),
                f"会话 `{session_record.session_id}` 已创建，开始执行任务。",
                with_session_prefix=False,
                session_id=session_record.session_id,
                thread_id=session_record.thread_id,
            )

        try:
            prompt = await self._prompt_builder.build(str(task_text), [])
            result = await self._codex.run(prompt, progress_callback=progress_callback)
        except CodexTimeoutError:
            logger.warning("Codex task timed out")
            await self._messenger.send_markdown(
                str(chat_id),
                "任务执行超时，请稍后再试。",
                with_session_prefix=bool(session_record),
                session_id=session_record.session_id if session_record else None,
                thread_id=session_record.thread_id if session_record else None,
            )
            return
        except CodexProcessError as exc:
            logger.exception("Codex task failed")
            await self._messenger.send_markdown(
                str(chat_id),
                f"任务执行失败: {exc}",
                with_session_prefix=bool(session_record),
                session_id=session_record.session_id if session_record else None,
                thread_id=session_record.thread_id if session_record else None,
            )
            return

        if result.thread_id:
            session_record = await self._storage.upsert_session(
                str(chat_id),
                result.thread_id,
                set_active=True,
            )
            if not start_notified:
                await self._messenger.send_markdown(
                    str(chat_id),
                    f"会话 `{session_record.session_id}` 已创建，任务已完成。",
                    with_session_prefix=False,
                    session_id=session_record.session_id,
                    thread_id=session_record.thread_id,
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

        response_text = result.response_text.strip() if result.response_text else "(无可用回复)"
        await self._messenger.send_markdown(
            str(chat_id),
            response_text,
            session_id=session_id,
            thread_id=thread_id,
        )
