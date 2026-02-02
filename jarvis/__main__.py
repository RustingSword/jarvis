from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from jarvis.app import JarvisApp
from jarvis.config import load_config


def _build_rotation(max_bytes: int):
    max_bytes = max(0, int(max_bytes))
    state: dict[str, datetime.date | None] = {"date": None}

    def _rotation(message, file) -> bool:
        if state["date"] is None:
            try:
                state["date"] = datetime.fromtimestamp(Path(file.name).stat().st_mtime).date()
            except FileNotFoundError:
                state["date"] = message.record["time"].date()
        msg_date = message.record["time"].date()
        if state["date"] and msg_date != state["date"]:
            state["date"] = msg_date
            return True
        if max_bytes <= 0:
            return False
        file.seek(0, os.SEEK_END)
        return file.tell() >= max_bytes

    return _rotation


def _setup_logging(level: str, log_file: str | None, max_bytes: int, backup_count: int) -> None:
    logger.remove()
    log_format = "{time:YYYY-MM-DD HH:mm:ss} {level} {name}: {message}"
    logger.add(sys.stderr, level=level, format=log_format)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        retention = timedelta(days=backup_count) if backup_count > 0 else None
        logger.add(
            str(path),
            level=level,
            format=log_format,
            rotation=_build_rotation(max_bytes),
            retention=retention,
            encoding="utf-8",
        )


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
        logger.exception("Jarvis failed to start")


if __name__ == "__main__":
    main()
