from __future__ import annotations

import json
from datetime import datetime, timedelta

from loguru import logger

from jarvis.codex import CodexError, CodexManager, CodexProcessError, CodexTimeoutError
from jarvis.event_bus import Event
from jarvis.memory import MemoryManager
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
        memory: MemoryManager,
    ) -> None:
        self._codex = codex
        self._storage = storage
        self._prompt_builder = prompt_builder
        self._messenger = messenger
        self._memory = memory

    async def handle(self, event: Event) -> None:
        if event.type == "command.compact":
            await self._handle_compact(event)
            return
        if event.type != "command.task":
            return
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
            tts_hint=bool(result.tts_text),
            tts_text=result.tts_text,
        )

    async def _handle_compact(self, event: Event) -> None:
        payload = event.payload or {}
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")
        session_id = payload.get("session_id")
        if not chat_id or not thread_id:
            return
        try:
            summary_result = await self._codex.run(
                "请总结到目前为止的对话内容，包含关键上下文、决策与待办事项，"
                "用简洁的要点列出，控制在 200 字以内。",
                session_id=str(thread_id),
            )
        except CodexTimeoutError:
            await self._messenger.send_markdown(
                str(chat_id),
                "会话压缩超时，请稍后再试。",
                with_session_prefix=False,
            )
            await self._maybe_restore_session(str(chat_id), str(thread_id))
            return
        except CodexProcessError as exc:
            error_msg = str(exc)
            if "UTF-8" in error_msg:
                error_msg = f"会话文件可能已损坏。建议使用 `/new` 创建新会话。\n技术详情: {exc}"
            await self._messenger.send_markdown(
                str(chat_id),
                f"会话压缩失败: {error_msg}",
                with_session_prefix=False,
            )
            await self._maybe_restore_session(str(chat_id), str(thread_id))
            return

        summary = summary_result.response_text.strip()
        if not summary:
            await self._messenger.send_markdown(
                str(chat_id),
                "未获取到摘要内容，压缩失败。",
                with_session_prefix=False,
            )
            await self._maybe_restore_session(str(chat_id), str(thread_id))
            return

        try:
            title = "compact"
            if session_id is not None:
                title = f"compact session_id={session_id}"
            await self._memory.append_daily_block(summary, title=title)
            await self._memory.sync()
        except Exception:
            logger.exception("Failed to write compact summary to memory")

        await self._storage.save_summary(str(chat_id), summary)

        active_session = await self._storage.get_session(str(chat_id))
        skip_seed = False
        if active_session and active_session.thread_id != str(thread_id):
            skip_seed = True
        else:
            await self._storage.clear_session(str(chat_id))
            active_session = None

        seed_result = None
        if not skip_seed:
            seed_prompt = "以下是之前对话的摘要，请基于这些内容继续后续对话：\n" + summary
            try:
                seed_result = await self._codex.run(seed_prompt)
            except CodexError:
                seed_result = None

        if seed_result and seed_result.thread_id:
            await self._storage.upsert_session(str(chat_id), seed_result.thread_id)

        if skip_seed:
            await self._messenger.send_markdown(
                str(chat_id),
                "会话已压缩。检测到新会话已开始，未自动重置。",
                with_session_prefix=False,
            )
        else:
            await self._messenger.send_markdown(
                str(chat_id),
                "会话已压缩并重置。",
                with_session_prefix=False,
            )
        try:
            await self._maybe_consolidate_yesterday_memory()
        except Exception:
            logger.exception("Failed to consolidate yesterday memory")

    async def _maybe_restore_session(self, chat_id: str, thread_id: str) -> None:
        active_session = await self._storage.get_session(chat_id)
        if active_session:
            return
        await self._storage.upsert_session(chat_id, thread_id, set_active=True)

    async def _maybe_consolidate_yesterday_memory(self) -> None:
        if not self._memory.enabled:
            return
        workspace = self._memory.workspace_dir
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        state_path = memory_dir / ".state.json"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8")) or {}
            except Exception:
                state = {}
        yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
        if state.get("last_consolidated") == yesterday:
            return
        yesterday_path = memory_dir / f"{yesterday}.md"
        if not yesterday_path.exists():
            return
        raw = yesterday_path.read_text(encoding="utf-8").strip()
        if not raw:
            state["last_consolidated"] = yesterday
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            return
        content = _truncate_text(raw, 4000)
        prompt = (
            "你是 Jarvis 的记忆整理器。请从下面的“昨日记忆”中提炼适合长期记忆的要点，"
            "输出 3-8 条精炼的项目符号（每条不超过 30 字）。"
            "如果没有值得长期保留的内容，输出 NO_UPDATE。\n\n"
            f"昨日记忆（{yesterday}）:\n{content}\n"
        )
        result = await self._codex.run(prompt)
        response = (result.response_text or "").strip()
        if not response or response.upper().startswith("NO_UPDATE"):
            state["last_consolidated"] = yesterday
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            return
        await self._memory.append_global_block(response, title=f"{yesterday} consolidate")
        await self._memory.sync()
        state["last_consolidated"] = yesterday
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _truncate_text(text: str, max_chars: int) -> str:
    limit = max(50, max_chars)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(truncated)"
