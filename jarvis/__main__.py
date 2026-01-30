from __future__ import annotations

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal

from jarvis.app import JarvisApp
from jarvis.config import load_config


def _setup_logging(level: str, log_file: str | None, max_bytes: int, backup_count: int) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
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
