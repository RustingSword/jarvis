#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from telegram import Bot

from jarvis.config import load_config
from jarvis.rss import RssService
from jarvis.rss.pdf import render_digest_pdf


def _resolve_path(path: str, config_path: str | None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute() or not config_path:
        return candidate
    return Path(config_path).expanduser().parent / candidate


async def _send_pdf(token: str, chat_id: str, pdf_path: Path) -> None:
    bot = Bot(token=token)
    async with bot:
        with open(pdf_path, "rb") as handle:
            await bot.send_document(
                chat_id=str(chat_id),
                document=handle,
                filename=pdf_path.name,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build RSS digest PDF and send to Telegram")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--chat-id",
        default=None,
        help="Telegram chat id (default: config.telegram.startup_chat_id)",
    )
    parser.add_argument(
        "--max-total",
        type=int,
        default=None,
        help="Override rss.max_total_items for this run",
    )
    parser.add_argument(
        "--max-feeds",
        type=int,
        default=None,
        help="Limit to first N feeds for this run",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    config = load_config(str(config_path))

    if args.max_total is not None:
        config.rss.max_total_items = max(1, int(args.max_total))
    if args.max_feeds is not None and args.max_feeds > 0:
        feeds_path = Path(config.rss.feeds_path).expanduser()
        feeds = [
            line.strip()
            for line in feeds_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        subset = feeds[: args.max_feeds]
        tmp_path = Path("/tmp/jarvis-rss-feeds.txt")
        tmp_path.write_text("\n".join(subset) + "\n", encoding="utf-8")
        config.rss.feeds_path = str(tmp_path)

    chat_id = args.chat_id or config.telegram.startup_chat_id
    if not chat_id:
        print("Missing chat_id. Provide --chat-id or set telegram.startup_chat_id.")
        return 2

    service = RssService(
        config.rss,
        messenger=None,
        openai_base_url=config.openai.base_url,
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        config_path=config.config_path,
    )
    digest, digest_md = asyncio.run(service.build_digest_bundle())
    if not digest:
        print("RSS：无新内容。")
        return 0

    output_dir = _resolve_path(config.rss.pdf_output_dir, config.config_path)
    template_path = _resolve_path(config.rss.pdf_template, config.config_path)
    pdf_path = render_digest_pdf(
        digest_md,
        output_dir,
        title=None if config.rss.pdf_backend == "pandoc" else "RSS 摘要",
        backend=config.rss.pdf_backend,
        template_path=template_path,
        pdf_engine=config.rss.pdf_engine,
        timeout_seconds=config.rss.pdf_timeout_seconds,
    )

    asyncio.run(_send_pdf(config.telegram.token, chat_id, pdf_path))
    print(f"Sent PDF to chat_id={chat_id}: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
