from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from jarvis.codex import CodexError, CodexProcessError, CodexTimeoutError, CodexManager
from jarvis.config import AppConfig
from jarvis.event_bus import EventBus
from jarvis.storage import ReminderRecord, Storage, TaskRecord
from jarvis.telegram import TelegramBot
from jarvis.triggers import TriggerManager

logger = logging.getLogger(__name__)


class JarvisApp:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._event_bus = EventBus()
        self._storage = Storage(config.storage)
        self._codex = CodexManager(config.codex)
        self._telegram = TelegramBot(config.telegram, self._event_bus)
        self._triggers = TriggerManager(self._event_bus, self._storage, config.triggers)

        self._event_bus.subscribe("telegram.message_received", self._on_message)
        self._event_bus.subscribe("telegram.command", self._on_command)
        self._event_bus.subscribe("trigger.fired", self._on_trigger)

    async def start(self) -> None:
        await self._storage.connect()
        await self._triggers.start()
        await self._telegram.start()
        await self._idle()

    async def stop(self) -> None:
        await self._telegram.stop()
        await self._triggers.stop()
        await self._storage.close()

    async def _idle(self) -> None:
        logger.info("Jarvis app running")
        stop_event = asyncio.Event()
        await stop_event.wait()

    async def _on_message(self, event) -> None:
        chat_id = event.payload.get("chat_id")
        text = event.payload.get("text", "")
        if not chat_id or not text:
            return

        session = await self._storage.get_session(chat_id)
        session_id = session.thread_id if session else None
        try:
            result = await self._codex.run(text, session_id=session_id)
        except CodexTimeoutError:
            logger.warning("Codex timed out")
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "Codex 调用超时，请稍后再试。"},
            )
            return
        except CodexProcessError as exc:
            logger.exception("Codex run failed")
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": f"Codex 调用失败: {exc}"},
            )
            return

        if result.thread_id:
            await self._storage.upsert_session(chat_id, result.thread_id)

        response_text = result.response_text or "(无可用回复)"
        await self._event_bus.publish(
            "telegram.send_message",
            {"chat_id": chat_id, "text": response_text},
        )

    async def _on_command(self, event) -> None:
        chat_id = event.payload.get("chat_id")
        command = event.payload.get("command")
        args = event.payload.get("args", [])
        if not chat_id or not command:
            return

        if command == "start":
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "Jarvis 已启动。输入消息即可对话。"},
            )
            return

        if command == "help":
            await self._event_bus.publish(
                "telegram.send_message",
                {
                    "chat_id": chat_id,
                    "text": "可用命令: /start, /help, /reset, /compact, /task, /remind",
                },
            )
            return

        if command == "reset":
            await self._storage.clear_session(chat_id)
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "会话已重置。"},
            )
            return

        if command == "compact":
            await self._handle_compact(chat_id)
            return

        if command == "task":
            await self._handle_task_command(chat_id, args)
            return

        if command == "remind":
            await self._handle_remind_command(chat_id, args)
            return

        await self._event_bus.publish(
            "telegram.send_message",
            {"chat_id": chat_id, "text": f"未知命令: {command}"},
        )

    async def _handle_compact(self, chat_id: str) -> None:
        session = await self._storage.get_session(chat_id)
        if not session:
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "当前没有可压缩的会话。"},
            )
            return
        try:
            summary_result = await self._codex.run(
                "请总结到目前为止的对话内容，包含关键上下文、决策与待办事项，"
                "用简洁的要点列出，控制在 200 字以内。",
                session_id=session.thread_id,
            )
        except CodexTimeoutError:
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "会话压缩超时，请稍后再试。"},
            )
            return
        except CodexProcessError as exc:
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": f"会话压缩失败: {exc}"},
            )
            return

        summary = summary_result.response_text.strip()
        if not summary:
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "未获取到摘要内容，压缩失败。"},
            )
            return

        await self._storage.save_summary(chat_id, summary)
        await self._storage.clear_session(chat_id)

        seed_prompt = "以下是之前对话的摘要，请基于这些内容继续后续对话：\n" + summary
        try:
            seed_result = await self._codex.run(seed_prompt)
        except CodexError:
            seed_result = None

        if seed_result and seed_result.thread_id:
            await self._storage.upsert_session(chat_id, seed_result.thread_id)

        await self._event_bus.publish(
            "telegram.send_message",
            {"chat_id": chat_id, "text": "会话已压缩并重置。"},
        )

    async def _handle_task_command(self, chat_id: str, args: list[str]) -> None:
        if not args:
            await self._event_bus.publish(
                "telegram.send_message",
                {
                    "chat_id": chat_id,
                    "text": "用法: /task add <描述> | /task list | /task done <id>",
                },
            )
            return
        action = args[0]
        if action == "add":
            description = " ".join(args[1:]).strip()
            if not description:
                await self._event_bus.publish(
                    "telegram.send_message",
                    {"chat_id": chat_id, "text": "请提供任务描述。"},
                )
                return
            task_id = await self._storage.add_task(chat_id, description, due_at=None)
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": f"任务已添加，ID: {task_id}"},
            )
            return
        if action == "list":
            tasks = await self._storage.list_tasks(chat_id)
            message = _format_tasks(tasks)
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": message},
            )
            return
        if action == "done":
            if len(args) < 2 or not args[1].isdigit():
                await self._event_bus.publish(
                    "telegram.send_message",
                    {"chat_id": chat_id, "text": "用法: /task done <id>"},
                )
                return
            task_id = int(args[1])
            ok = await self._storage.complete_task(chat_id, task_id)
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "任务已完成。" if ok else "未找到该任务。"},
            )
            return

        await self._event_bus.publish(
            "telegram.send_message",
            {"chat_id": chat_id, "text": "未知 task 子命令。"},
        )

    async def _handle_remind_command(self, chat_id: str, args: list[str]) -> None:
        if not args:
            await self._event_bus.publish(
                "telegram.send_message",
                {
                    "chat_id": chat_id,
                    "text": "用法: /remind <YYYY-MM-DD HH:MM> <内容> | /remind list | /remind cancel <id>",
                },
            )
            return
        action = args[0]
        if action == "list":
            reminders = await self._storage.list_reminders(chat_id)
            message = _format_reminders(reminders)
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": message},
            )
            return
        if action == "cancel":
            if len(args) < 2 or not args[1].isdigit():
                await self._event_bus.publish(
                    "telegram.send_message",
                    {"chat_id": chat_id, "text": "用法: /remind cancel <id>"},
                )
                return
            reminder_id = int(args[1])
            ok = await self._storage.delete_reminder(chat_id, reminder_id)
            await self._event_bus.publish(
                "telegram.send_message",
                {"chat_id": chat_id, "text": "提醒已取消。" if ok else "未找到该提醒。"},
            )
            return

        dt, message = _parse_remind_args(args)
        if not dt or not message:
            await self._event_bus.publish(
                "telegram.send_message",
                {
                    "chat_id": chat_id,
                    "text": "用法: /remind <YYYY-MM-DD HH:MM> <内容>",
                },
            )
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
        await self._event_bus.publish(
            "telegram.send_message",
            {"chat_id": chat_id, "text": f"提醒已设置，ID: {reminder_id}"},
        )

    async def _on_trigger(self, event) -> None:
        payload = event.payload
        trigger_type = payload.get("type")
        if trigger_type == "reminder":
            chat_id = payload.get("chat_id")
            message = payload.get("message") or "提醒"
            if chat_id:
                await self._event_bus.publish(
                    "telegram.send_message",
                    {"chat_id": chat_id, "text": f"⏰ {message}"},
                )
            reminder_id = payload.get("reminder_id")
            repeat_interval_seconds = payload.get("repeat_interval_seconds")
            if reminder_id:
                await self._triggers.handle_reminder_fired(
                    int(reminder_id),
                    int(repeat_interval_seconds) if repeat_interval_seconds else None,
                )
            return

        if trigger_type == "monitor":
            chat_id = payload.get("chat_id")
            message = (
                f"监控告警: {payload.get('name')} "
                f"{payload.get('metric')}={payload.get('value')} "
                f"(阈值 {payload.get('threshold')})"
            )
            if chat_id:
                await self._event_bus.publish(
                    "telegram.send_message",
                    {"chat_id": chat_id, "text": message},
                )
            return

        if trigger_type == "schedule":
            chat_id = payload.get("chat_id")
            message = payload.get("message") or f"计划触发: {payload.get('name')}"
            if chat_id:
                await self._event_bus.publish(
                    "telegram.send_message",
                    {"chat_id": chat_id, "text": message},
                )
            return

        if trigger_type == "webhook":
            webhook_payload = payload.get("payload")
            logger.info("Webhook fired: %s", webhook_payload)
            if isinstance(webhook_payload, dict):
                chat_id = webhook_payload.get("chat_id")
                message = webhook_payload.get("message")
                if chat_id and message:
                    await self._event_bus.publish(
                        "telegram.send_message",
                        {"chat_id": str(chat_id), "text": str(message)},
                    )
            return

        logger.debug("Unhandled trigger: %s", payload)


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
        return "暂无任务。"
    lines = ["任务列表:"]
    for task in tasks:
        status = "✅" if task.status == "done" else "📝"
        lines.append(f"{status} [{task.id}] {task.description}")
    return "\n".join(lines)


def _format_reminders(reminders: list[ReminderRecord]) -> str:
    if not reminders:
        return "暂无提醒。"
    lines = ["提醒列表:"]
    for reminder in reminders:
        ts = reminder.trigger_time.isoformat(sep=" ", timespec="minutes")
        lines.append(f"⏰ [{reminder.id}] {ts} {reminder.message}")
    return "\n".join(lines)
