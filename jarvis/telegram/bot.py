from __future__ import annotations

import functools
import logging
import re
from pathlib import Path
from typing import Any

import telegramify_markdown
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from jarvis.config import TelegramConfig
from jarvis.event_bus import EventBus
from jarvis.events import TELEGRAM_COMMAND, TELEGRAM_MESSAGE_RECEIVED, TELEGRAM_SEND

logger = logging.getLogger(__name__)

COMMAND_SPECS = (
    ("start", "开始使用 Jarvis"),
    ("help", "显示帮助信息"),
    ("reset", "重置对话上下文"),
    ("compact", "压缩对话历史"),
    ("resume", "恢复历史会话"),
    ("verbosity", "设置输出详细程度"),
    ("skills", "查看或安装技能"),
    ("memory", "记忆搜索与写入"),
)

TELEGRAM_MESSAGE_MAX_CHARS = 4096


class TelegramBot:
    def __init__(self, config: TelegramConfig, event_bus: EventBus) -> None:
        self._config = config
        self._event_bus = event_bus
        self._app: Application | None = None
        self._media_dir = Path(self._config.media_dir).expanduser()

        event_bus.subscribe(TELEGRAM_SEND, self._on_send_message)

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
        message = update.message
        text = message.text or message.caption or ""
        attachments = await self._collect_attachments(message)
        if not text and not attachments:
            return
        payload = {
            "chat_id": str(update.effective_chat.id) if update.effective_chat else "",
            "user_id": str(update.effective_user.id) if update.effective_user else "",
            "text": text,
            "message_id": message.message_id,
            "media_group_id": message.media_group_id,
            "attachments": attachments,
        }
        await self._event_bus.publish(TELEGRAM_MESSAGE_RECEIVED, payload)

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
        await self._event_bus.publish(TELEGRAM_COMMAND, payload)

    async def _on_send_message(self, event) -> None:
        if not self._app or not self._app.bot:
            return
        chat_id = event.payload.get("chat_id")
        if not chat_id:
            return
        text = event.payload.get("text")
        if not text:
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
            await self._send_text_chunks(chat_id, send_text, send_parse_mode)
        except BadRequest as exc:
            if _is_chat_not_found_error(exc):
                logger.warning("Telegram chat not found (chat_id=%s); drop message.", chat_id)
                return
            if send_parse_mode:
                logger.warning("Failed to send Markdown message, retrying as plain text: %s", exc)
                try:
                    await self._send_text_chunks(chat_id, raw_text, None)
                except BadRequest as retry_exc:
                    if _is_chat_not_found_error(retry_exc):
                        logger.warning("Telegram chat not found (chat_id=%s); drop message.", chat_id)
                        return
                    raise
            else:
                raise
        media_items = event.payload.get("media") or event.payload.get("attachments") or []
        if media_items:
            await self._send_media_items(chat_id, media_items, event.payload)

    async def _send_text_chunks(self, chat_id: str, text: str, parse_mode: str | None) -> None:
        if not self._app or not self._app.bot:
            return
        for chunk in _split_text(text, TELEGRAM_MESSAGE_MAX_CHARS):
            if not chunk:
                continue
            await self._app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)

    def _register_handlers(self, app: Application) -> None:
        for command, _description in COMMAND_SPECS:
            handler = functools.partial(self._publish_command, command=command)
            app.add_handler(CommandHandler(command, handler))
        media_filters = (
            filters.PHOTO
            | filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE
            | filters.VIDEO_NOTE
            | filters.ANIMATION
        )
        app.add_handler(MessageHandler((filters.TEXT | media_filters) & ~filters.COMMAND, self._handle_message))

    async def _collect_attachments(self, message) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []

        if message.photo:
            photo = message.photo[-1]
            item = await self._download_media(
                kind="photo",
                media=photo,
                filename_hint=None,
                mime_type=None,
            )
            if item:
                attachments.append(item)

        if message.document:
            item = await self._download_media(
                kind="document",
                media=message.document,
                filename_hint=message.document.file_name,
                mime_type=message.document.mime_type,
            )
            if item:
                attachments.append(item)

        if message.video:
            item = await self._download_media(
                kind="video",
                media=message.video,
                filename_hint=message.video.file_name,
                mime_type=message.video.mime_type,
            )
            if item:
                attachments.append(item)

        if message.audio:
            item = await self._download_media(
                kind="audio",
                media=message.audio,
                filename_hint=message.audio.file_name,
                mime_type=message.audio.mime_type,
            )
            if item:
                attachments.append(item)

        if message.voice:
            item = await self._download_media(
                kind="voice",
                media=message.voice,
                filename_hint=None,
                mime_type=message.voice.mime_type,
            )
            if item:
                attachments.append(item)

        if message.video_note:
            item = await self._download_media(
                kind="video_note",
                media=message.video_note,
                filename_hint=None,
                mime_type=None,
            )
            if item:
                attachments.append(item)

        if message.animation:
            item = await self._download_media(
                kind="animation",
                media=message.animation,
                filename_hint=message.animation.file_name,
                mime_type=message.animation.mime_type,
            )
            if item:
                attachments.append(item)

        return attachments

    async def _download_media(
        self,
        *,
        kind: str,
        media,
        filename_hint: str | None,
        mime_type: str | None,
    ) -> dict[str, Any] | None:
        try:
            file_obj = await media.get_file()
            local_path = await self._save_file(file_obj, kind=kind, filename_hint=filename_hint)
        except Exception:
            logger.exception("Failed to download telegram media (%s)", kind)
            return None

        file_id = getattr(media, "file_id", None)
        file_unique_id = getattr(media, "file_unique_id", None)
        return {
            "type": kind,
            "path": str(local_path),
            "file_name": filename_hint,
            "mime_type": mime_type,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
        }

    async def _save_file(self, file_obj, *, kind: str, filename_hint: str | None) -> Path:
        self._ensure_media_dir()
        suffix = Path(getattr(file_obj, "file_path", "") or "").suffix
        if not suffix and filename_hint:
            suffix = Path(filename_hint).suffix

        safe_hint = _sanitize_filename(filename_hint) if filename_hint else ""
        raw_unique = getattr(file_obj, "file_unique_id", None) or getattr(file_obj, "file_id", "file")
        unique_id = _sanitize_filename(str(raw_unique))

        if safe_hint:
            filename = f"{unique_id}_{safe_hint}"
        else:
            filename = f"{kind}_{unique_id}"
        if suffix and not filename.endswith(suffix):
            filename = f"{filename}{suffix}"

        target_path = self._media_dir / filename
        await file_obj.download_to_drive(custom_path=target_path)
        return target_path

    def _ensure_media_dir(self) -> None:
        try:
            self._media_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("Failed to create media dir: %s", self._media_dir)

    async def _send_media_items(self, chat_id: str, media_items: list[dict[str, Any]], payload: dict) -> None:
        if not self._app or not self._app.bot:
            return
        for item in media_items:
            path = item.get("path") or item.get("file")
            if not path:
                continue
            kind = (item.get("type") or "document").lower()
            caption = item.get("caption")
            parse_mode = item.get("parse_mode")
            try:
                await self._send_single_media(chat_id, kind, path, caption=caption, parse_mode=parse_mode)
            except Exception:
                logger.exception("Failed to send media: %s", path)

    async def _send_single_media(
        self,
        chat_id: str,
        kind: str,
        path: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if not self._app or not self._app.bot:
            return
        file_path = Path(path)
        if not file_path.exists():
            logger.warning("Media file not found: %s", file_path)
            return
        try:
            if kind == "photo":
                await self._app.bot.send_photo(chat_id=chat_id, photo=file_path, caption=caption, parse_mode=parse_mode)
                return
            if kind == "video":
                await self._app.bot.send_video(chat_id=chat_id, video=file_path, caption=caption, parse_mode=parse_mode)
                return
            if kind == "audio":
                await self._app.bot.send_audio(chat_id=chat_id, audio=file_path, caption=caption, parse_mode=parse_mode)
                return
            if kind == "voice":
                await self._app.bot.send_voice(chat_id=chat_id, voice=file_path, caption=caption, parse_mode=parse_mode)
                return
            if kind == "animation":
                await self._app.bot.send_animation(
                    chat_id=chat_id,
                    animation=file_path,
                    caption=caption,
                    parse_mode=parse_mode,
                )
                return
            if kind == "video_note":
                await self._app.bot.send_video_note(chat_id=chat_id, video_note=file_path)
                return
            await self._app.bot.send_document(chat_id=chat_id, document=file_path, caption=caption, parse_mode=parse_mode)
        except BadRequest as exc:
            if _is_chat_not_found_error(exc):
                logger.warning("Telegram chat not found (chat_id=%s); skip media: %s", chat_id, file_path)
                return
            raise


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str | None) -> str:
    if not name:
        return ""
    sanitized = _SAFE_FILENAME_RE.sub("_", name).strip("._")
    return sanitized or "file"


def _is_chat_not_found_error(exc: Exception) -> bool:
    return "chat not found" in str(exc).lower()


def _split_text(text: str, max_chars: int) -> list[str]:
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for idx in range(0, len(line), max_chars):
                chunks.append(line[idx : idx + max_chars])
            continue
        if len(buffer) + len(line) > max_chars:
            chunks.append(buffer)
            buffer = ""
        buffer += line
    if buffer:
        chunks.append(buffer)
    return chunks
