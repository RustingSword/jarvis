#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from jarvis.config import load_config
from jarvis.rss import RssService


def main() -> int:
    parser = argparse.ArgumentParser(description="Run RSS digest once and print output")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--feeds-path",
        default=None,
        help="Override rss.feeds_path for this run",
    )
    parser.add_argument(
        "--max-feeds",
        type=int,
        default=None,
        help="Limit to first N feeds for this run",
    )
    parser.add_argument(
        "--max-total",
        type=int,
        default=None,
        help="Override rss.max_total_items for this run",
    )
    parser.add_argument(
        "--max-per-feed",
        type=int,
        default=None,
        help="Override rss.max_items_per_feed for this run",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    config = load_config(str(config_path))

    if args.feeds_path:
        config.rss.feeds_path = args.feeds_path
    if args.max_feeds is not None and args.max_feeds > 0:
        feeds_path = Path(config.rss.feeds_path).expanduser()
        feeds = [
            line.strip()
            for line in feeds_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        subset = feeds[: args.max_feeds]
        tmp = tempfile.NamedTemporaryFile(prefix="jarvis-rss-feeds-", suffix=".txt", delete=False)
        Path(tmp.name).write_text("\n".join(subset) + "\n", encoding="utf-8")
        config.rss.feeds_path = tmp.name

    if args.max_total is not None:
        config.rss.max_total_items = max(1, int(args.max_total))
    if args.max_per_feed is not None:
        config.rss.max_items_per_feed = max(1, int(args.max_per_feed))

    service = RssService(
        config.rss,
        messenger=None,
        openai_base_url=config.openai.base_url,
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        config_path=config.config_path,
    )

    digest = asyncio.run(service.build_digest())
    if digest:
        print(digest)
    else:
        print("RSS：无新内容。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
