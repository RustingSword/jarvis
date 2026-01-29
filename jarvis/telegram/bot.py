from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from jarvis.config import TelegramConfig
from jarvis.event_bus import EventBus

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, config: TelegramConfig, event_bus: EventBus) -> None:
        self._config = config
        self._event_bus = event_bus
        self._app: Application | None = None

        event_bus.subscribe("telegram.send_message", self._on_send_message)

    async def start(self) -> None:
        app = ApplicationBuilder().token(self._config.token).build()
        app.add_handler(CommandHandler("start", self._handle_start))
        app.add_handler(CommandHandler("help", self._handle_help))
        app.add_handler(CommandHandler("reset", self._handle_reset))
        app.add_handler(CommandHandler("compact", self._handle_compact))
        app.add_handler(CommandHandler("task", self._handle_task))
        app.add_handler(CommandHandler("remind", self._handle_remind))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        self._app = app
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling()
        logger.info("Telegram bot started")

    async def stop(self) -> None:
        if not self._app:
            return
        if self._app.updater:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        logger.info("Telegram bot stopped")

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._publish_command(update, context, "start")

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._publish_command(update, context, "help")

    async def _handle_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._publish_command(update, context, "reset")

    async def _handle_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._publish_command(update, context, "compact")

    async def _handle_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._publish_command(update, context, "task")

    async def _handle_remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._publish_command(update, context, "remind")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        payload = {
            "chat_id": str(update.effective_chat.id) if update.effective_chat else "",
            "user_id": str(update.effective_user.id) if update.effective_user else "",
            "text": update.message.text or "",
            "message_id": update.message.message_id,
        }
        await self._event_bus.publish("telegram.message_received", payload)

    async def _publish_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
    ) -> None:
        payload = {
            "chat_id": str(update.effective_chat.id) if update.effective_chat else "",
            "user_id": str(update.effective_user.id) if update.effective_user else "",
            "command": command,
            "args": list(getattr(context, "args", []) or []),
            "raw_text": update.message.text if update.message else "",
            "message_id": update.message.message_id if update.message else None,
        }
        await self._event_bus.publish("telegram.command", payload)

    async def _on_send_message(self, event) -> None:
        if not self._app or not self._app.bot:
            return
        chat_id = event.payload.get("chat_id")
        text = event.payload.get("text")
        if not chat_id or not text:
            return
        await self._app.bot.send_message(chat_id=chat_id, text=text)
