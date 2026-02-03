#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.request import urlopen

DEFAULT_GIST_ID = "e6d2bf860ccc367fe37ff953ba6de66b"
XML_URL_PATTERN = re.compile(r'xmlUrl="([^"]+)"')
URL_PATTERN = re.compile(r"https?://[^\s\"'<>)+]+")


def fetch_text(url: str) -> str:
    with urlopen(url) as resp:  # nosec B310 - controlled URL
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str) -> dict:
    with urlopen(url) as resp:  # nosec B310 - controlled URL
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def resolve_opml_raw_url(gist_ref: str) -> str:
    gist_id = gist_ref
    if gist_ref.startswith("http") and "api.github.com/gists/" in gist_ref:
        api_url = gist_ref
    else:
        if "/" in gist_ref:
            gist_id = gist_ref.rstrip("/").split("/")[-1]
        api_url = f"https://api.github.com/gists/{gist_id}"

    payload = fetch_json(api_url)
    files = payload.get("files", {}) if isinstance(payload, dict) else {}
    opml_raw = None
    fallback_raw = None
    for entry in files.values():
        if not isinstance(entry, dict):
            continue
        raw_url = entry.get("raw_url")
        filename = str(entry.get("filename", ""))
        if raw_url and not fallback_raw:
            fallback_raw = raw_url
        if raw_url and filename.endswith(".opml"):
            opml_raw = raw_url
            break
    if opml_raw:
        return opml_raw
    if fallback_raw:
        return fallback_raw
    raise RuntimeError("No raw_url found in gist files")


def extract_urls(text: str) -> list[str]:
    urls = XML_URL_PATTERN.findall(text)
    if not urls:
        urls = URL_PATTERN.findall(text)
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        url = url.strip()
        if url.endswith(")"):
            url = url[:-1]
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch RSS feed list from gist")
    parser.add_argument(
        "--gist",
        default=DEFAULT_GIST_ID,
        help="Gist ID or API URL (default: emschwartz opml gist)",
    )
    parser.add_argument(
        "--output",
        default="data/rss_feeds.txt",
        help="Output file (one URL per line)",
    )
    args = parser.parse_args()

    try:
        raw_url = resolve_opml_raw_url(args.gist)
        text = fetch_text(raw_url)
    except Exception as exc:
        print(f"Failed to fetch gist: {exc}", file=sys.stderr)
        return 1

    urls = extract_urls(text)
    if not urls:
        print("No URLs found in gist content.", file=sys.stderr)
        return 2

    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    print(f"Wrote {len(urls)} feeds to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
