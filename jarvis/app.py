from __future__ import annotations

import asyncio
import logging

from jarvis.codex import CodexManager
from jarvis.config import AppConfig
from jarvis.event_bus import EventBus
from jarvis.events import (
    TELEGRAM_COMMAND,
    TELEGRAM_MESSAGE_RECEIVED,
    TELEGRAM_MESSAGE_SENT,
    TRIGGER_FIRED,
)
from jarvis.handlers.command_router import CommandRouter
from jarvis.handlers.progress import CodexProgressHandler
from jarvis.handlers.message_sent import MessageSentHandler
from jarvis.handlers.trigger_dispatcher import TriggerDispatcher
from jarvis.memory import MemoryManager
from jarvis.messaging.bundler import MessageBundler
from jarvis.messaging.messenger import Messenger
from jarvis.pipeline.message_pipeline import MessagePipeline
from jarvis.pipeline.prompt_builder import PromptBuilder
from jarvis.pipeline.task_pipeline import TaskPipeline
from jarvis.storage import Storage
from jarvis.telegram import TelegramBot
from jarvis.triggers import TriggerManager
from jarvis.verbosity import VerbosityManager
from jarvis.workers import QueueWorker

logger = logging.getLogger(__name__)


class JarvisApp:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._event_bus = EventBus()
        self._storage = Storage(config.storage)
        self._codex = CodexManager(config.codex)
        self._memory = MemoryManager(config.memory)
        self._telegram = TelegramBot(config.telegram, self._event_bus)
        self._triggers = TriggerManager(self._event_bus, config.triggers)

        self._messenger = Messenger(self._event_bus, self._storage)
        self._verbosity = VerbosityManager(self._storage, config.output.verbosity)
        self._progress = CodexProgressHandler(self._messenger, self._storage, self._verbosity)
        self._message_sent_handler = MessageSentHandler(self._storage)
        self._prompt_builder = PromptBuilder(self._memory)
        self._message_pipeline = MessagePipeline(
            self._codex,
            self._storage,
            self._prompt_builder,
            self._progress,
            self._messenger,
            self._verbosity,
        )
        self._message_worker = QueueWorker(
            self._message_pipeline.handle,
            name="message-worker",
            concurrency=config.workers.message_concurrency,
        )
        self._task_pipeline = TaskPipeline(
            self._codex,
            self._storage,
            self._prompt_builder,
            self._messenger,
        )
        self._task_worker = QueueWorker(
            self._task_pipeline.handle,
            name="task-worker",
            concurrency=config.workers.task_concurrency,
        )
        self._command_router = CommandRouter(
            self._messenger,
            self._storage,
            self._codex,
            self._memory,
            config.skills,
            config.config_path,
            self._verbosity,
            self._task_worker.enqueue,
        )
        self._trigger_dispatcher = TriggerDispatcher(self._message_worker.enqueue)
        self._command_worker = QueueWorker(
            self._command_router.handle,
            name="command-worker",
            concurrency=config.workers.command_concurrency,
        )
        self._bundler = MessageBundler(
            config.telegram.bundle_wait_seconds,
            self._message_worker.enqueue,
        )

        self._event_bus.subscribe(TELEGRAM_MESSAGE_RECEIVED, self._bundler.handle_event)
        self._event_bus.subscribe(TELEGRAM_COMMAND, self._command_worker.enqueue)
        self._event_bus.subscribe(TELEGRAM_MESSAGE_SENT, self._message_sent_handler.handle)
        self._event_bus.subscribe(TRIGGER_FIRED, self._trigger_dispatcher.handle)

    async def start(self) -> None:
        await self._storage.connect()
        await self._memory.connect()
        await self._triggers.start()
        await self._telegram.start()
        await self._send_startup_message()
        await self._message_worker.start()
        await self._task_worker.start()
        await self._command_worker.start()
        await self._idle()

    async def stop(self) -> None:
        await self._telegram.stop()
        await self._triggers.stop()
        await self._bundler.flush_all()
        await self._message_worker.stop()
        await self._task_worker.stop()
        await self._command_worker.stop()
        await self._memory.close()
        await self._storage.close()

    async def _idle(self) -> None:
        logger.info("Jarvis app running")
        stop_event = asyncio.Event()
        await stop_event.wait()

    async def _send_startup_message(self) -> None:
        cfg = self._config.telegram
        if not cfg.startup_notify:
            return
        if not cfg.startup_chat_id:
            logger.warning("Startup notify enabled but startup_chat_id not set")
            return
        message = cfg.startup_message or "Jarvis 已就绪 ✅"
        await self._messenger.send_message(
            str(cfg.startup_chat_id),
            message,
            with_session_prefix=False,
        )
