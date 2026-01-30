from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from jarvis.codex import CodexError, CodexProcessError, CodexTimeoutError, CodexManager
from jarvis.config import AppConfig, SkillSourceConfig, persist_skill_source
from jarvis.event_bus import Event, EventBus
from jarvis.memory import MemoryManager
from jarvis.skills import SkillError, install_skill, list_installed_skills, list_remote_skills
from jarvis.storage import ReminderRecord, Storage, TaskRecord
from jarvis.telegram import TelegramBot
from jarvis.triggers import TriggerManager

logger = logging.getLogger(__name__)

EVENT_TELEGRAM_MESSAGE = "telegram.message_received"
EVENT_TELEGRAM_COMMAND = "telegram.command"
EVENT_TELEGRAM_SEND = "telegram.send_message"
EVENT_TRIGGER_FIRED = "trigger.fired"

_TOOL_CALL_NAME_MAP = {
    "shell_command": "执行命令",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "list_directory": "列出目录",
    "web_search": "网络搜索",
    "browser_action": "浏览器操作",
}

_TOOL_USE_NAME_MAP = {
    "bash": "执行命令",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "list_files": "列出文件",
    "web_search": "网络搜索",
}

CommandHandler = Callable[[str, list[str]], Awaitable[None]]
TriggerHandler = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class PendingMessageBundle:
    chat_id: str
    user_id: str
    text_parts: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    last_message_id: int | None = None
    media_group_id: str | None = None
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

    def build_payload(self) -> dict:
        text = "\n".join(part for part in self.text_parts if part.strip())
        return {
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "text": text,
            "message_id": self.last_message_id,
            "media_group_id": self.media_group_id,
            "attachments": list(self.attachments),
            "bundle_count": len(self.text_parts) + len(self.attachments),
        }

class JarvisApp:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._event_bus = EventBus()
        self._storage = Storage(config.storage)
        self._codex = CodexManager(config.codex)
        self._memory = MemoryManager(config.memory)
        self._telegram = TelegramBot(config.telegram, self._event_bus)
        self._triggers = TriggerManager(self._event_bus, self._storage, config.triggers)
        self._default_verbosity = (config.output.verbosity or "full").lower()
        self._verbosity_by_chat: dict[str, str] = {}
        self._message_queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._command_queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._message_worker_task: asyncio.Task | None = None
        self._command_worker_task: asyncio.Task | None = None
        self._pending_bundles: dict[str, PendingMessageBundle] = {}
        self._bundle_lock = asyncio.Lock()
        self._bundle_wait_seconds = max(0.0, float(config.telegram.bundle_wait_seconds))

        self._command_handlers: dict[str, CommandHandler] = {
            "start": self._cmd_start,
            "help": self._cmd_help,
            "reset": self._cmd_reset,
            "compact": self._cmd_compact,
            "resume": self._cmd_resume,
            "verbosity": self._cmd_verbosity,
            "task": self._cmd_task,
            "remind": self._cmd_remind,
            "skills": self._cmd_skills,
            "memory": self._cmd_memory,
        }
        self._trigger_handlers: dict[str, TriggerHandler] = {
            "reminder": self._handle_reminder_trigger,
            "monitor": self._handle_monitor_trigger,
            "schedule": self._handle_schedule_trigger,
            "webhook": self._handle_webhook_trigger,
        }

        self._event_bus.subscribe(EVENT_TELEGRAM_MESSAGE, self._enqueue_message)
        self._event_bus.subscribe(EVENT_TELEGRAM_COMMAND, self._enqueue_command)
        self._event_bus.subscribe(EVENT_TRIGGER_FIRED, self._on_trigger)

    async def start(self) -> None:
        await self._storage.connect()
        await self._memory.connect()
        await self._triggers.start()
        await self._telegram.start()
        await self._send_startup_message()
        self._message_worker_task = asyncio.create_task(self._message_worker(), name="message-worker")
        self._command_worker_task = asyncio.create_task(self._command_worker(), name="command-worker")
        await self._idle()

    async def stop(self) -> None:
        await self._telegram.stop()
        await self._triggers.stop()
        await self._stop_workers()
        await self._memory.close()
        await self._storage.close()

    async def _idle(self) -> None:
        logger.info("Jarvis app running")
        stop_event = asyncio.Event()
        await stop_event.wait()

    async def _send_startup_message(self) -> None:
        message = (self._config.telegram.startup_message or "").strip()
        chat_id_raw = (self._config.telegram.startup_chat_id or "").strip()
        if not message or not chat_id_raw:
            logger.info("Startup message skipped (missing chat_id or message)")
            return
        chat_ids = [item.strip() for item in chat_id_raw.split(",") if item.strip()]
        for chat_id in chat_ids:
            await self._send_message(
                chat_id,
                message,
                with_separator=False,
                with_session_prefix=False,
            )

    async def _enqueue_message(self, event: Event) -> None:
        if self._bundle_wait_seconds <= 0:
            await self._message_queue.put(event)
            return
        await self._collect_message_bundle(event)

    async def _enqueue_command(self, event: Event) -> None:
        await self._command_queue.put(event)

    def _bundle_key(self, payload: dict) -> str:
        chat_id = payload.get("chat_id") or ""
        user_id = payload.get("user_id") or ""
        return f"{chat_id}:{user_id}"

    async def _collect_message_bundle(self, event: Event) -> None:
        payload = event.payload
        chat_id = payload.get("chat_id")
        if not chat_id:
            return
        key = self._bundle_key(payload)
        async with self._bundle_lock:
            bundle = self._pending_bundles.get(key)
            if not bundle:
                bundle = PendingMessageBundle(
                    chat_id=str(chat_id),
                    user_id=str(payload.get("user_id") or ""),
                )
                self._pending_bundles[key] = bundle
            bundle.add_payload(payload)
            if bundle.flush_task:
                bundle.flush_task.cancel()
            bundle.flush_task = asyncio.create_task(self._flush_bundle_after(key, self._bundle_wait_seconds))

    async def _flush_bundle_after(self, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._flush_bundle(key)

    async def _flush_bundle(self, key: str) -> None:
        async with self._bundle_lock:
            bundle = self._pending_bundles.pop(key, None)
        if not bundle:
            return
        if bundle.flush_task:
            bundle.flush_task.cancel()
        payload = bundle.build_payload()
        event = Event(type=EVENT_TELEGRAM_MESSAGE, payload=payload, created_at=datetime.now(timezone.utc))
        await self._message_queue.put(event)

    async def _message_worker(self) -> None:
        while True:
            event = await self._message_queue.get()
            if event is None:
                self._message_queue.task_done()
                break
            try:
                await self._handle_message(event)
            except Exception:
                logger.exception("Message handling failed")
            finally:
                self._message_queue.task_done()

    async def _command_worker(self) -> None:
        while True:
            event = await self._command_queue.get()
            if event is None:
                self._command_queue.task_done()
                break
            try:
                await self._handle_command(event)
            except Exception:
                logger.exception("Command handling failed")
            finally:
                self._command_queue.task_done()

    async def _stop_workers(self) -> None:
        await self._flush_all_bundles()
        if self._message_worker_task:
            await self._message_queue.put(None)
        if self._command_worker_task:
            await self._command_queue.put(None)

        tasks = [task for task in (self._message_worker_task, self._command_worker_task) if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._message_worker_task = None
        self._command_worker_task = None

    async def _flush_all_bundles(self) -> None:
        async with self._bundle_lock:
            keys = list(self._pending_bundles.keys())
        for key in keys:
            await self._flush_bundle(key)

    async def _handle_message(self, event: Event) -> None:
        chat_id = event.payload.get("chat_id")
        text = event.payload.get("text", "")
        attachments = list(event.payload.get("attachments") or [])
        if not chat_id or (not text and not attachments):
            return

        await self._ensure_verbosity(chat_id)
        session = await self._storage.get_session(chat_id)
        thread_id = session.thread_id if session else None

        # 创建进度回调函数
        async def progress_callback(codex_event: dict) -> None:
            await self._handle_codex_progress(chat_id, codex_event)

        try:
            prompt = await self._build_prompt(text, attachments)
            result = await self._codex.run(prompt, session_id=thread_id, progress_callback=progress_callback)
        except CodexTimeoutError:
            logger.warning("Codex timed out")
            await self._send_message(chat_id, "Codex 调用超时，请稍后再试。")
            return
        except CodexProcessError as exc:
            logger.exception("Codex run failed")
            await self._send_message(chat_id, f"Codex 调用失败: {exc}")
            return

        if result.thread_id:
            await self._storage.upsert_session(chat_id, result.thread_id)

        response_text = result.response_text or "(无可用回复)"
        # 直接发送 Codex 返回的 markdown 内容
        await self._send_markdown(chat_id, response_text)

    async def _augment_with_memory(self, text: str) -> str:
        if not self._memory.enabled:
            return text
        try:
            results = await self._memory.search(text)
        except Exception:
            logger.exception("Memory search failed")
            return text
        if not results:
            return text
        lines = ["以下是可能相关的记忆片段（仅供参考）："]
        for idx, item in enumerate(results, start=1):
            lines.append(
                f"{idx}. {item.path}#L{item.start_line}-L{item.end_line}: {item.snippet}"
            )
        lines.append("")
        lines.append("用户消息：")
        lines.append(text)
        return "\n".join(lines)

    async def _build_prompt(self, text: str, attachments: list[dict]) -> str:
        base_text = text or ""
        prompt = await self._augment_with_memory(base_text) if base_text else ""
        if attachments:
            attachments_text = self._format_attachments_prompt(attachments)
            if prompt:
                prompt = f"{prompt}\n\n{attachments_text}"
            else:
                prompt = f"用户未提供文本，仅提供了附件。\n\n{attachments_text}"
        return prompt or base_text

    @staticmethod
    def _format_attachments_prompt(attachments: list[dict]) -> str:
        lines = ["用户附件（请直接读取以下文件路径）："]
        for idx, item in enumerate(attachments, start=1):
            path = item.get("path") or item.get("file") or ""
            if not path:
                continue
            meta_parts = []
            item_type = item.get("type")
            if item_type:
                meta_parts.append(str(item_type))
            file_name = item.get("file_name")
            if file_name:
                meta_parts.append(str(file_name))
            mime_type = item.get("mime_type")
            if mime_type:
                meta_parts.append(str(mime_type))
            meta = f" ({' / '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"{idx}. {path}{meta}")
        return "\n".join(lines)

    async def _handle_codex_progress(self, chat_id: str, event: dict) -> None:
        """处理 Codex 进度事件，发送有价值的信息到 Telegram"""
        event_type = event.get("type")

        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                await self._storage.upsert_session(chat_id, str(thread_id))
            return

        if event_type == "event_msg":
            await self._handle_event_msg(chat_id, event.get("payload", {}))
            return

        if event_type == "response_item":
            await self._handle_response_item(chat_id, event.get("payload", {}))
            return

        if event_type == "item.completed":
            await self._handle_item_completed(chat_id, event.get("item", {}))
            return

    def _summarize_reasoning(self, text: str) -> str:
        """简化思考过程文本，提取关键信息"""
        # 保留原始markdown格式，不做处理
        return text

    async def _ensure_verbosity(self, chat_id: str) -> None:
        if chat_id in self._verbosity_by_chat:
            return
        stored = await self._storage.get_setting(chat_id, "verbosity")
        normalized = self._normalize_verbosity(stored) if stored else None
        self._verbosity_by_chat[chat_id] = normalized or self._default_verbosity

    def _get_chat_verbosity(self, chat_id: str) -> str:
        return self._verbosity_by_chat.get(chat_id, self._default_verbosity)

    def _show_tool_messages(self, chat_id: str) -> bool:
        return self._get_chat_verbosity(chat_id) not in {"compact", "minimal"}

    @staticmethod
    def _normalize_verbosity(value: str | None) -> str | None:
        if not value:
            return None
        raw = value.strip().lower()
        aliases = {
            "full": "full",
            "verbose": "full",
            "normal": "full",
            "detail": "full",
            "详细": "full",
            "完整": "full",
            "compact": "compact",
            "minimal": "compact",
            "lite": "compact",
            "quiet": "compact",
            "精简": "compact",
            "简洁": "compact",
            "简短": "compact",
            "安静": "compact",
        }
        return aliases.get(raw)

    @staticmethod
    def _as_blockquote(text: str) -> str:
        lines = text.splitlines() or [text]
        return "\n".join(f"> {line}" if line else ">" for line in lines)

    def _format_tool_call(self, tool_name: str, arguments: str) -> str:
        """格式化工具调用信息"""
        tool_display = _TOOL_CALL_NAME_MAP.get(tool_name, tool_name)

        # 尝试解析参数以提取关键信息
        try:
            args = json.loads(arguments)

            # 对于 shell_command，显示命令内容
            if tool_name == "shell_command" and "command" in args:
                cmd = args["command"]
                if isinstance(cmd, list):
                    cmd_str = " ".join(cmd)
                else:
                    cmd_str = str(cmd)

                return _format_code_block(tool_display, cmd_str)

            # 对于文件操作，显示文件路径
            elif "path" in args:
                path = str(args["path"])
                return _format_tool_path(tool_display, path)
            elif "file" in args:
                file_path = str(args["file"])
                return _format_tool_path(tool_display, file_path)

        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        return tool_display

    def _format_tool_use(self, tool_name: str, tool_input: dict) -> str:
        """格式化工具使用信息（用于 item.completed 格式）"""
        tool_display = _TOOL_USE_NAME_MAP.get(tool_name, tool_name)

        # 尝试提取关键信息
        if tool_name == "bash" and "command" in tool_input:
            cmd = tool_input["command"]
            return _format_code_block(tool_display, cmd)
        elif "path" in tool_input:
            path = str(tool_input["path"])
            return _format_tool_path(tool_display, path)
        elif "query" in tool_input:
            query = str(tool_input["query"])
            return _format_tool_path(tool_display, query)

        return tool_display

    async def _handle_command(self, event: Event) -> None:
        chat_id = event.payload.get("chat_id")
        command = event.payload.get("command")
        args = event.payload.get("args", [])
        if not chat_id or not command:
            return

        await self._ensure_verbosity(chat_id)
        handler = self._command_handlers.get(command)
        if not handler:
            await self._send_markdown(chat_id, f"未知命令: `{command}`")
            return
        await handler(chat_id, args)

    async def _cmd_start(self, chat_id: str, args: list[str]) -> None:
        await self._send_markdown(chat_id, "Jarvis 已启动。输入消息即可对话。")

    async def _cmd_help(self, chat_id: str, args: list[str]) -> None:
        await self._send_markdown(
            chat_id,
            "\n".join(
                [
                    "**可用命令**",
                    "- `/start` - 启动对话",
                    "- `/help` - 显示帮助",
                    "- `/reset` - 重置当前对话上下文",
                    "- `/compact` - 压缩对话历史并重置",
                    "- `/resume <id>` - 恢复历史会话（不带 id 会列出最近会话）",
                    "- `/verbosity <full|compact|reset>` - 控制输出详细程度",
                    "- `/task add <描述>` | `/task list` | `/task done <id>` - 任务管理",
                    "- `/remind <YYYY-MM-DD HH:MM> <内容>` | `/remind list` | `/remind cancel <id>` - 提醒",
                    "- `/skills sources` | `/skills list [source]` | `/skills installed` | "
                    "`/skills install <source> <name>` | `/skills add-source <name> <repo> <path> [ref] [token_env]` - skills 管理",
                    "- `/memory search <关键词>` | `/memory add <内容>` | `/memory get <path> [from] [lines]` | "
                    "`/memory index` | `/memory status` - 记忆功能",
                    "",
                    "提示：每条消息前会显示会话标识，如 `> [12]`。",
                ]
            ),
        )

    async def _cmd_reset(self, chat_id: str, args: list[str]) -> None:
        await self._storage.clear_session(chat_id)
        await self._send_markdown(chat_id, "会话已重置。")

    async def _cmd_compact(self, chat_id: str, args: list[str]) -> None:
        await self._handle_compact(chat_id)

    async def _cmd_resume(self, chat_id: str, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            sessions = await self._storage.list_sessions(chat_id, limit=5)
            if not sessions:
                await self._send_markdown(chat_id, "暂无可恢复的会话。")
                return
            lines = ["**用法**: `/resume <id>`", "**最近会话**:"]
            for session in sessions:
                ts = session.last_active.isoformat(sep=" ", timespec="minutes")
                lines.append(f"- {session.session_id} (最后活动: {ts})")
            await self._send_markdown(chat_id, "\n".join(lines))
            return

        session_id = int(args[0])
        record = await self._storage.activate_session(chat_id, session_id)
        if not record:
            await self._send_markdown(chat_id, f"未找到会话 ID: `{session_id}`")
            return
        await self._send_markdown(chat_id, "已恢复会话。")

    async def _cmd_verbosity(self, chat_id: str, args: list[str]) -> None:
        if not args:
            current = self._get_chat_verbosity(chat_id)
            await self._send_markdown(
                chat_id,
                f"**当前 verbosity**: `{current}`\n**用法**: `/verbosity full|compact|reset`",
            )
            return

        action = args[0].strip().lower()
        if action in {"reset", "default"}:
            await self._storage.delete_setting(chat_id, "verbosity")
            self._verbosity_by_chat[chat_id] = self._default_verbosity
            await self._send_markdown(
                chat_id,
                f"verbosity 已重置为默认值: `{self._default_verbosity}`",
            )
            return

        normalized = self._normalize_verbosity(args[0])
        if not normalized:
            await self._send_markdown(chat_id, "**用法**: `/verbosity full|compact|reset`")
            return

        self._verbosity_by_chat[chat_id] = normalized
        await self._storage.set_setting(chat_id, "verbosity", normalized)
        await self._send_markdown(
            chat_id,
            f"verbosity 已设置为: `{normalized}`",
        )

    async def _handle_compact(self, chat_id: str) -> None:
        session = await self._storage.get_session(chat_id)
        if not session:
            await self._send_markdown(chat_id, "当前没有可压缩的会话。")
            return
        try:
            summary_result = await self._codex.run(
                "请总结到目前为止的对话内容，包含关键上下文、决策与待办事项，"
                "用简洁的要点列出，控制在 200 字以内。",
                session_id=session.thread_id,
            )
        except CodexTimeoutError:
            await self._send_markdown(chat_id, "会话压缩超时，请稍后再试。")
            return
        except CodexProcessError as exc:
            error_msg = str(exc)
            # 如果是 UTF-8 错误，提供更有用的提示
            if "UTF-8" in error_msg:
                error_msg = (
                    "会话文件可能已损坏。建议使用 `/reset` 重置会话。\n"
                    f"技术详情: {exc}"
                )
            await self._send_markdown(chat_id, f"会话压缩失败: {error_msg}")
            return

        summary = summary_result.response_text.strip()
        if not summary:
            await self._send_markdown(chat_id, "未获取到摘要内容，压缩失败。")
            return

        try:
            await self._memory.append_daily_block(summary, title="compact")
            await self._memory.sync()
        except Exception:
            logger.exception("Failed to write compact summary to memory")

        await self._storage.save_summary(chat_id, summary)
        await self._storage.clear_session(chat_id)

        seed_prompt = "以下是之前对话的摘要，请基于这些内容继续后续对话：\n" + summary
        try:
            seed_result = await self._codex.run(seed_prompt)
        except CodexError:
            seed_result = None

        if seed_result and seed_result.thread_id:
            await self._storage.upsert_session(chat_id, seed_result.thread_id)

        await self._send_markdown(chat_id, "会话已压缩并重置。")
        try:
            await self._maybe_consolidate_yesterday_memory()
        except Exception:
            logger.exception("Failed to consolidate yesterday memory")

    async def _cmd_task(self, chat_id: str, args: list[str]) -> None:
        if not args:
            await self._send_markdown(
                chat_id,
                "**用法**: `/task add <描述>` | `/task list` | `/task done <id>`",
            )
            return
        action = args[0]
        if action == "add":
            description = " ".join(args[1:]).strip()
            if not description:
                await self._send_markdown(chat_id, "请提供任务描述。")
                return
            task_id = await self._storage.add_task(chat_id, description, due_at=None)
            await self._send_markdown(chat_id, f"任务已添加，ID: `{task_id}`")
            return
        if action == "list":
            tasks = await self._storage.list_tasks(chat_id)
            message = _format_tasks(tasks)
            await self._send_markdown(chat_id, message)
            return
        if action == "done":
            if len(args) < 2 or not args[1].isdigit():
                await self._send_markdown(chat_id, "**用法**: `/task done <id>`")
                return
            task_id = int(args[1])
            ok = await self._storage.complete_task(chat_id, task_id)
            await self._send_markdown(chat_id, "任务已完成。" if ok else "未找到该任务。")
            return

        await self._send_markdown(chat_id, "未知 task 子命令。")

    async def _cmd_remind(self, chat_id: str, args: list[str]) -> None:
        if not args:
            await self._send_markdown(
                chat_id,
                "**用法**: `/remind <YYYY-MM-DD HH:MM> <内容>` | `/remind list` | `/remind cancel <id>`",
            )
            return
        action = args[0]
        if action == "list":
            reminders = await self._storage.list_reminders(chat_id)
            message = _format_reminders(reminders)
            await self._send_markdown(chat_id, message)
            return
        if action == "cancel":
            if len(args) < 2 or not args[1].isdigit():
                await self._send_markdown(chat_id, "**用法**: `/remind cancel <id>`")
                return
            reminder_id = int(args[1])
            ok = await self._storage.delete_reminder(chat_id, reminder_id)
            await self._send_markdown(chat_id, "提醒已取消。" if ok else "未找到该提醒。")
            return

        dt, message = _parse_remind_args(args)
        if not dt or not message:
            await self._send_markdown(chat_id, "**用法**: `/remind <YYYY-MM-DD HH:MM> <内容>`")
            return
        reminder_id = await self._storage.add_reminder(chat_id, message, dt, None)
        reminder = ReminderRecord(
            id=reminder_id,
            chat_id=chat_id,
            message=message,
            trigger_time=dt,
            repeat_interval_seconds=None,
        )
        await self._triggers.schedule_reminder(reminder)
        await self._send_markdown(chat_id, f"提醒已设置，ID: `{reminder_id}`")

    async def _cmd_skills(self, chat_id: str, args: list[str]) -> None:
        if not args:
            await self._send_markdown(chat_id, _format_skills_usage())
            return

        action = args[0]
        if action == "installed":
            installed = list_installed_skills()
            if not installed:
                await self._send_markdown(chat_id, "暂无已安装技能。")
                return
            lines = ["**已安装技能**"]
            for entry in installed:
                desc = f" - {entry.description}" if entry.description else ""
                lines.append(f"- `{entry.name}`{desc}")
            await self._send_markdown(chat_id, "\n".join(lines))
            return

        if action == "sources":
            sources = self._config.skills.sources
            if not sources:
                await self._send_markdown(chat_id, "未配置 skills sources。")
                return
            lines = ["**已配置 sources**"]
            for src in sources:
                ref = f"@{src.ref}" if src.ref else ""
                target = f"{src.repo}/{src.path}{ref}"
                lines.append(f"- `{src.name}`: {src.type} `{target}`")
            await self._send_markdown(chat_id, "\n".join(lines))
            return

        if action == "list":
            sources = self._config.skills.sources
            if not sources:
                await self._send_markdown(chat_id, "未配置 skills sources。")
                return
            source_name = args[1] if len(args) > 1 else None
            try:
                remote = await list_remote_skills(sources, source_name=source_name)
            except SkillError as exc:
                await self._send_markdown(chat_id, f"skills 列表获取失败: {exc}")
                return
            if not remote:
                await self._send_markdown(chat_id, "未找到可用技能。")
                return
            installed_names = {entry.name for entry in list_installed_skills()}
            grouped: dict[str, list[str]] = {}
            for entry in remote:
                label = entry.source
                name = entry.name
                if name in installed_names:
                    name = f"{name} (已安装)"
                grouped.setdefault(label, []).append(name)
            lines = ["**可用技能**"]
            for label, items in grouped.items():
                lines.append(f"**{label}**")
                for idx, item in enumerate(items, start=1):
                    lines.append(f"{idx}. `{item}`")
            await self._send_markdown(chat_id, "\n".join(lines))
            return

        if action == "install":
            if len(args) < 3:
                await self._send_markdown(chat_id, "**用法**: `/skills install <source> <name>`")
                return
            source_name = args[1]
            skill_name = args[2]
            try:
                dest = await install_skill(self._config.skills.sources, source_name, skill_name)
            except SkillError as exc:
                await self._send_markdown(chat_id, f"安装失败: {exc}")
                return
            await self._send_markdown(chat_id, f"已安装 `{skill_name}` -> `{dest}`")
            return

        if action == "add-source":
            if len(args) < 4:
                await self._send_markdown(
                    chat_id,
                    "**用法**: `/skills add-source <name> <repo> <path> [ref] [token_env]`",
                )
                return
            if not self._config.config_path:
                await self._send_markdown(chat_id, "未找到配置路径，无法持久化 source。")
                return
            name = args[1].strip()
            repo = args[2].strip()
            path = args[3].strip()
            ref = args[4].strip() if len(args) > 4 else None
            token_env = args[5].strip() if len(args) > 5 else None
            if not name or not repo or not path:
                await self._send_markdown(
                    chat_id,
                    "**用法**: `/skills add-source <name> <repo> <path> [ref] [token_env]`",
                )
                return
            source = SkillSourceConfig(
                name=name,
                type="github",
                repo=repo,
                path=path,
                ref=ref or None,
                token_env=token_env or None,
            )
            try:
                updated = persist_skill_source(self._config.config_path, source)
            except Exception as exc:
                await self._send_markdown(chat_id, f"写入配置失败: {exc}")
                return

            replaced = False
            for idx, entry in enumerate(self._config.skills.sources):
                if entry.name == name:
                    self._config.skills.sources[idx] = source
                    replaced = True
                    break
            if not replaced:
                self._config.skills.sources.append(source)

            action_label = "已更新" if updated else "已添加"
            await self._send_markdown(chat_id, f"{action_label} source: `{name}`")
            return

        await self._send_markdown(chat_id, "未知 skills 子命令。")

    async def _cmd_memory(self, chat_id: str, args: list[str]) -> None:
        if not self._memory.enabled:
            await self._send_markdown(chat_id, "记忆功能已禁用。")
            return
        if not args:
            await self._send_markdown(
                chat_id,
                "**用法**: `/memory search <关键词>` | `/memory add <内容>` | "
                "`/memory get <path> [from] [lines]` | `/memory index` | `/memory status`",
            )
            return
        action = args[0].strip().lower()
        if action == "search":
            query = " ".join(args[1:]).strip()
            if not query:
                await self._send_markdown(chat_id, "**用法**: `/memory search <关键词>`")
                return
            try:
                results = await self._memory.search(query)
            except Exception:
                logger.exception("Memory search failed")
                await self._send_markdown(chat_id, "记忆搜索失败。")
                return
            if not results:
                await self._send_markdown(chat_id, "没有找到相关记忆。")
                return
            lines = ["**搜索结果**:"]
            for item in results:
                lines.append(
                    f"- `{item.path}` L{item.start_line}-L{item.end_line}: {item.snippet}"
                )
            await self._send_markdown(chat_id, "\n".join(lines))
            return

        if action == "add":
            content = " ".join(args[1:]).strip()
            if not content:
                await self._send_markdown(chat_id, "**用法**: `/memory add <内容>`")
                return
            try:
                path = await self._memory.append_daily(content)
                await self._memory.sync()
            except Exception:
                logger.exception("Memory append failed")
                await self._send_markdown(chat_id, "记忆写入失败。")
                return
            if path:
                await self._send_markdown(chat_id, f"已写入记忆：`{path}`")
            else:
                await self._send_markdown(chat_id, "未写入内容。")
            return

        if action == "get":
            if len(args) < 2:
                await self._send_markdown(
                    chat_id, "**用法**: `/memory get <path> [from] [lines]`"
                )
                return
            path = args[1]
            from_line = None
            lines_count = None
            if len(args) >= 3 and args[2].isdigit():
                from_line = int(args[2])
            if len(args) >= 4 and args[3].isdigit():
                lines_count = int(args[3])
            try:
                snippet = await self._memory.read_snippet(path, from_line, lines_count)
            except Exception:
                logger.exception("Memory read failed")
                await self._send_markdown(chat_id, "记忆读取失败。")
                return
            await self._send_markdown(chat_id, _format_code_block(f"📄 {path}", snippet))
            return

        if action == "index":
            try:
                await self._memory.sync(force=True)
            except Exception:
                logger.exception("Memory reindex failed")
                await self._send_markdown(chat_id, "记忆索引失败。")
                return
            await self._send_markdown(chat_id, "记忆索引已更新。")
            return

        if action == "status":
            try:
                stats = await self._memory.status()
            except Exception:
                logger.exception("Memory status failed")
                await self._send_markdown(chat_id, "记忆状态获取失败。")
                return
            await self._send_markdown(
                chat_id, f"**记忆状态**\n- files: {stats['files']}\n- chunks: {stats['chunks']}"
            )
            return

        await self._send_markdown(chat_id, "未知 memory 子命令。")

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

    async def _on_trigger(self, event: Event) -> None:
        payload = event.payload
        trigger_type = payload.get("type")
        if not trigger_type:
            logger.debug("Trigger missing type: %s", payload)
            return
        handler = self._trigger_handlers.get(trigger_type)
        if handler:
            await handler(payload)
            return
        logger.debug("Unhandled trigger: %s", payload)

    async def _handle_reminder_trigger(self, payload: dict) -> None:
        chat_id = payload.get("chat_id")
        message = payload.get("message") or "提醒"
        if chat_id:
            await self._send_message(chat_id, f"⏰ {message}")
        reminder_id = payload.get("reminder_id")
        repeat_interval_seconds = payload.get("repeat_interval_seconds")
        if reminder_id:
            await self._triggers.handle_reminder_fired(
                int(reminder_id),
                int(repeat_interval_seconds) if repeat_interval_seconds else None,
            )

    async def _handle_monitor_trigger(self, payload: dict) -> None:
        chat_id = payload.get("chat_id")
        message = (
            f"监控告警: {payload.get('name')} "
            f"{payload.get('metric')}={payload.get('value')} "
            f"(阈值 {payload.get('threshold')})"
        )
        if chat_id:
            await self._send_message(chat_id, message)

    async def _handle_schedule_trigger(self, payload: dict) -> None:
        chat_id = payload.get("chat_id")
        message = payload.get("message") or f"计划触发: {payload.get('name')}"
        if chat_id:
            await self._send_message(chat_id, message)

    async def _handle_webhook_trigger(self, payload: dict) -> None:
        webhook_payload = payload.get("payload")
        logger.info("Webhook fired: %s", webhook_payload)
        if isinstance(webhook_payload, dict):
            chat_id = webhook_payload.get("chat_id")
            message = webhook_payload.get("message")
            if chat_id and message:
                await self._send_message(str(chat_id), str(message))

    async def _handle_event_msg(self, chat_id: str, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type != "agent_reasoning":
            return
        reasoning_text = payload.get("text", "")
        if not reasoning_text:
            return
        summary = self._summarize_reasoning(reasoning_text)
        if not summary:
            return
        final_text = f"💭 思考\n{self._as_blockquote(summary)}"
        await self._send_markdown(chat_id, final_text, with_separator=False)

    async def _handle_response_item(self, chat_id: str, payload: dict) -> None:
        item_type = payload.get("type")
        if item_type != "function_call":
            return
        if not self._show_tool_messages(chat_id):
            return
        tool_name = payload.get("name", "")
        arguments = payload.get("arguments", "")
        tool_display = self._format_tool_call(tool_name, arguments)
        await self._send_markdown(chat_id, f"🔧 工具\n{tool_display}", with_separator=False)

    async def _handle_item_completed(self, chat_id: str, item: dict) -> None:
        item_type = item.get("type")
        if item_type == "reasoning":
            await self._handle_item_reasoning(chat_id, item)
            return
        if item_type == "command_execution":
            if not self._show_tool_messages(chat_id):
                return
            command = item.get("command", "")
            if command:
                await self._send_markdown(
                    chat_id,
                    _format_code_block("⚙️ 执行命令", command),
                    with_separator=False,
                )
            return
        if item_type == "tool_use":
            if not self._show_tool_messages(chat_id):
                return
            tool_name = item.get("name", "")
            tool_input = item.get("input", {})
            if tool_name:
                tool_display = self._format_tool_use(tool_name, tool_input)
                await self._send_markdown(chat_id, f"🔧 工具\n{tool_display}", with_separator=False)

    async def _handle_item_reasoning(self, chat_id: str, item: dict) -> None:
        reasoning_text = ""
        item_text = item.get("text")
        if isinstance(item_text, str) and item_text:
            reasoning_text = item_text
        if not reasoning_text:
            summary_list = item.get("summary", [])
            reasoning_texts = [
                s.get("text", "")
                for s in summary_list
                if isinstance(s, dict) and s.get("type") == "summary_text" and s.get("text")
            ]
            if reasoning_texts:
                reasoning_text = "\n\n".join(reasoning_texts)

        if reasoning_text:
            summary = self._summarize_reasoning(reasoning_text)
            if summary:
                final_text = f"💭 思考\n{self._as_blockquote(summary)}"
                await self._send_markdown(chat_id, final_text, with_separator=False)
            return

        await self._send_markdown(chat_id, "💭 _思考中_...", with_separator=False)

    async def _send_message(
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
        payload = {"chat_id": chat_id, "text": final_text}
        if markdown:
            payload["markdown"] = True
        if parse_mode:
            payload["parse_mode"] = parse_mode
        await self._event_bus.publish(EVENT_TELEGRAM_SEND, payload)

    async def _send_markdown(self, chat_id: str, text: str, *, with_separator: bool = True) -> None:
        await self._send_message(chat_id, text, markdown=True, with_separator=with_separator)

    async def _send_media(
        self,
        chat_id: str,
        media: list[dict],
        *,
        text: str | None = None,
        markdown: bool = False,
    ) -> None:
        payload = {"chat_id": chat_id, "media": media}
        if text:
            payload["text"] = text
        if markdown:
            payload["markdown"] = True
        await self._event_bus.publish(EVENT_TELEGRAM_SEND, payload)

    async def _with_session_prefix(self, chat_id: str, text: str, *, with_separator: bool = True) -> str:
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


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


def _parse_remind_args(args: list[str]) -> tuple[datetime | None, str | None]:
    if not args:
        return None, None
    if len(args) >= 2 and _DATE_RE.match(args[0]) and _TIME_RE.match(args[1]):
        dt_str = f"{args[0]} {args[1]}"
        message = " ".join(args[2:]).strip()
    else:
        dt_str = args[0]
        message = " ".join(args[1:]).strip()
    dt = _parse_datetime(dt_str)
    return dt, message if message else None


def _parse_datetime(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _format_tasks(tasks: list[TaskRecord]) -> str:
    if not tasks:
        return "**暂无任务**"
    lines = ["**任务列表**"]
    for task in tasks:
        status = "✅" if task.status == "done" else "📝"
        lines.append(f"- {status} `{task.id}` {task.description}")
    return "\n".join(lines)


def _format_reminders(reminders: list[ReminderRecord]) -> str:
    if not reminders:
        return "**暂无提醒**"
    lines = ["**提醒列表**"]
    for reminder in reminders:
        ts = reminder.trigger_time.isoformat(sep=" ", timespec="minutes")
        lines.append(f"- ⏰ `{reminder.id}` `{ts}` {reminder.message}")
    return "\n".join(lines)


def _format_skills_usage() -> str:
    return "\n".join(
        [
            "**用法**",
            "- `/skills sources`",
            "- `/skills list [source]`",
            "- `/skills installed`",
            "- `/skills install <source> <name>`",
            "- `/skills add-source <name> <repo> <path> [ref] [token_env]`",
        ]
    )


def _format_code_block(label: str, content: str) -> str:
    return f"{label}\n```\n{content}\n```"


def _format_tool_path(label: str, value: str) -> str:
    return f"{label}\n{value}"


def _truncate_text(text: str, max_chars: int) -> str:
    limit = max(50, max_chars)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(truncated)"
