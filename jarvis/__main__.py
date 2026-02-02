from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
from datetime import datetime, timedelta
from logging.handlers import BaseRotatingHandler
from pathlib import Path

from jarvis.app import JarvisApp
from jarvis.config import load_config


class DailySizeRotatingFileHandler(BaseRotatingHandler):
    def __init__(
        self, filename: str, max_bytes: int, backup_days: int, encoding: str | None = None
    ) -> None:
        super().__init__(filename, mode="a", encoding=encoding, delay=True)
        self.max_bytes = max(0, int(max_bytes))
        self.backup_days = int(backup_days)
        self.base_path = Path(filename).expanduser()
        self._archive_re = re.compile(
            rf"^{re.escape(self.base_path.name)}\.(\d{{4}}-\d{{2}}-\d{{2}})(?:\.\d+)?$"
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        today = datetime.now().date()
        file_date = self._get_file_date()
        if file_date and file_date != today:
            return True
        if self.max_bytes <= 0:
            return False
        current_size = 0
        try:
            current_size = self.base_path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        msg = f"{self.format(record)}{os.linesep}"
        msg_size = len(msg.encode(self.encoding or "utf-8", errors="replace"))
        return (current_size + msg_size) >= self.max_bytes

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        file_date = self._get_file_date() or datetime.now().date()
        if self.base_path.exists():
            target = self._next_archive_path(file_date.isoformat())
            self.rotate(str(self.base_path), str(target))

        self._cleanup_old_logs()

    def _get_file_date(self) -> datetime.date | None:
        try:
            ts = self.base_path.stat().st_mtime
        except FileNotFoundError:
            return None
        return datetime.fromtimestamp(ts).date()

    def _next_archive_path(self, date_str: str) -> Path:
        base = self.base_path.with_name(f"{self.base_path.name}.{date_str}")
        if not base.exists():
            return base
        index = 1
        while True:
            candidate = self.base_path.with_name(f"{self.base_path.name}.{date_str}.{index}")
            if not candidate.exists():
                return candidate
            index += 1

    def _cleanup_old_logs(self) -> None:
        if self.backup_days <= 0:
            return
        cutoff = datetime.now().date() - timedelta(days=self.backup_days - 1)
        for entry in self.base_path.parent.iterdir():
            if not entry.is_file():
                continue
            match = self._archive_re.match(entry.name)
            if not match:
                continue
            try:
                entry_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if entry_date < cutoff:
                try:
                    entry.unlink()
                except OSError:
                    continue


def _setup_logging(level: str, log_file: str | None, max_bytes: int, backup_count: int) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            DailySizeRotatingFileHandler(
                str(path),
                max_bytes=max_bytes,
                backup_days=backup_count,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    _silence_polling_logs()


def _silence_polling_logs() -> None:
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _run(config_path: str) -> None:
    config = load_config(config_path)
    _setup_logging(
        config.logging.level,
        config.logging.file,
        config.logging.max_bytes,
        config.logging.backup_count,
    )

    app = JarvisApp(config)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    app_task = asyncio.create_task(app.start())
    try:
        await stop_event.wait()
    finally:
        app_task.cancel()
        await app.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis Telegram Assistant")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.exception("Jarvis failed to start")


if __name__ == "__main__":
    main()
