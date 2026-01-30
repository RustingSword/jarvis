from __future__ import annotations

import functools
import logging
from typing import Any

import telegramify_markdown
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from jarvis.config import TelegramConfig
from jarvis.event_bus import EventBus

logger = logging.getLogger(__name__)

COMMAND_SPECS = (
    ("start", "开始使用 Jarvis"),
    ("help", "显示帮助信息"),
    ("reset", "重置对话上下文"),
    ("compact", "压缩对话历史"),
    ("task", "创建或管理任务"),
    ("remind", "设置提醒"),
)


class TelegramBot:
    def __init__(self, config: TelegramConfig, event_bus: EventBus) -> None:
        self._config = config
        self._event_bus = event_bus
        self._app: Application | None = None

        event_bus.subscribe("telegram.send_message", self._on_send_message)

    async def start(self) -> None:
        app = ApplicationBuilder().token(self._config.token).build()
        self._register_handlers(app)

        self._app = app
        await app.initialize()
        await app.start()

        # 设置 bot 命令列表，清除之前的所有命令
        commands = [BotCommand(name, description) for name, description in COMMAND_SPECS]
        await app.bot.set_my_commands(commands)
        logger.info("Telegram bot commands set")

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
        parse_mode = event.payload.get("parse_mode")
        use_markdown = event.payload.get("markdown", False)
        raw_text = text
        send_text = text
        send_parse_mode = None
        if parse_mode:
            send_parse_mode = parse_mode
        elif use_markdown:
            # 使用 telegramify-markdown 转换为 MarkdownV2 格式
            send_parse_mode = ParseMode.MARKDOWN_V2
            send_text = telegramify_markdown.markdownify(text)
            # 调试日志
            logger.debug(f"Markdown conversion:\nOriginal: {text[:200]}\nConverted: {send_text[:200]}\nParse mode: {send_parse_mode}")
        else:
            # 不使用格式化，直接发送纯文本
            send_parse_mode = None
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=send_text, parse_mode=send_parse_mode)
        except BadRequest as exc:
            if send_parse_mode:
                logger.warning("Failed to send Markdown message, retrying as plain text: %s", exc)
                await self._app.bot.send_message(chat_id=chat_id, text=raw_text, parse_mode=None)
            else:
                raise

    def _register_handlers(self, app: Application) -> None:
        for command, _description in COMMAND_SPECS:
            handler = functools.partial(self._publish_command, command=command)
            app.add_handler(CommandHandler(command, handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
