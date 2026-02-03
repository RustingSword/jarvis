from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Iterable

import aiohttp
import feedparser
import trafilatura
from loguru import logger

from jarvis.config import RssConfig
from jarvis.messaging.messenger import Messenger
from jarvis.rss.pdf import render_digest_pdf


@dataclass(slots=True)
class RssItem:
    feed_url: str
    feed_title: str
    title: str
    link: str
    published: str | None
    summary: str
    item_id: str
    published_at: datetime | None = None
    summary_zh: str | None = None
    content_full: str | None = None


@dataclass(slots=True)
class FeedUpdate:
    feed_url: str
    feed_title: str
    items: list[RssItem]


class RssService:
    def __init__(
        self,
        config: RssConfig,
        messenger: Messenger | None,
        *,
        openai_base_url: str,
        openai_api_key: str | None,
        config_path: str | None,
    ) -> None:
        self._config = config
        self._messenger = messenger
        self._openai_base_url = openai_base_url.rstrip("/")
        self._openai_api_key = openai_api_key
        self._config_path = config_path
        self._feeds_path = _resolve_path(config.feeds_path, config_path)
        self._state_path = _resolve_path(config.state_path, config_path)
        self._lock = asyncio.Lock()

    async def run(self, chat_id: str) -> None:
        if not self._config.enabled:
            logger.info("RSS disabled; skip run.")
            return
        if not chat_id:
            logger.warning("RSS run missing chat_id.")
            return
        if not self._messenger:
            logger.warning("RSS messenger not configured; skip send.")
            return
        if not self._feeds_path.exists():
            logger.warning("RSS feeds file not found: {}", self._feeds_path)
            return

        async with self._lock:
            digest, digest_md = await self.build_digest_bundle()
            if not digest:
                if self._config.send_empty:
                    await self._messenger.send_message(
                        chat_id,
                        "RSS：今日无更新。",
                        with_session_prefix=False,
                    )
                return
            for chunk in _split_message(digest):
                await self._messenger.send_message(
                    chat_id,
                    chunk,
                    with_session_prefix=False,
                )
            await self._send_pdf(chat_id, digest_md)

    async def build_digest_bundle(self) -> tuple[str, str]:
        updates = await self._collect_updates()
        if not updates:
            return "", ""
        await self._summarize_updates(updates)
        return _format_digest(updates), _format_digest_markdown(updates)

    async def build_digest(self) -> str:
        digest, _digest_md = await self.build_digest_bundle()
        return digest

    async def _send_pdf(self, chat_id: str, digest_md: str) -> None:
        if not self._messenger or not self._config.pdf_enabled:
            return
        if not digest_md.strip():
            return
        output_dir = _resolve_path(self._config.pdf_output_dir, self._config_path)
        title = f"RSS 摘要 {datetime.now().astimezone().strftime('%Y-%m-%d')}"
        title_for_pdf = None if self._config.pdf_backend == "pandoc" else title
        try:
            template_path = _resolve_path(self._config.pdf_template, self._config_path)
            path = render_digest_pdf(
                digest_md,
                output_dir,
                title=title_for_pdf,
                backend=self._config.pdf_backend,
                template_path=template_path,
                pdf_engine=self._config.pdf_engine,
                timeout_seconds=self._config.pdf_timeout_seconds,
            )
        except Exception as exc:
            logger.warning("RSS PDF render failed: {}", exc)
            return
        await self._messenger.send_media(
            chat_id,
            [{"type": "document", "path": str(path), "caption": "RSS 摘要 PDF"}],
        )

    async def _collect_updates(self) -> list[FeedUpdate]:
        feeds = _read_feeds(self._feeds_path)
        if not feeds:
            logger.warning("RSS feeds list is empty: {}", self._feeds_path)
            return []

        state = _RssStateStore(self._state_path, self._config.max_ids_per_feed).load()
        timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)
        headers = {"User-Agent": self._config.user_agent}
        semaphore = asyncio.Semaphore(max(1, self._config.concurrency))

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            tasks = [self._fetch_feed(session, url, semaphore) for url in feeds]
            results = await asyncio.gather(*tasks)

        updates: list[FeedUpdate] = []
        total_items = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for feed_url, parsed in results:
            if not parsed:
                continue
            feed_title = str(parsed.feed.get("title") or feed_url)
            items = _extract_items(feed_url, feed_title, parsed)
            if not items:
                continue
            feed_state = state.feeds.get(feed_url, {})
            seen_ids = set(feed_state.get("ids", []))
            new_items = [item for item in items if item.item_id not in seen_ids]
            if not new_items:
                continue
            new_items.sort(key=_sort_key, reverse=True)
            new_items = new_items[: max(1, self._config.max_items_per_feed)]

            total_items += len(new_items)
            if total_items > self._config.max_total_items:
                overflow = total_items - self._config.max_total_items
                if overflow >= len(new_items):
                    total_items -= len(new_items)
                    continue
                new_items = new_items[: len(new_items) - overflow]
                total_items = self._config.max_total_items

            updates.append(FeedUpdate(feed_url=feed_url, feed_title=feed_title, items=new_items))

            merged_ids = [item.item_id for item in new_items] + list(feed_state.get("ids", []))
            feed_state["ids"] = merged_ids[: self._config.max_ids_per_feed]
            feed_state["last_seen"] = now_iso
            feed_state["title"] = feed_title
            state.feeds[feed_url] = feed_state

        if updates:
            if self._config.fulltext_enabled:
                await self._populate_fulltext(updates)
            state.updated_at = now_iso
            _RssStateStore(self._state_path, self._config.max_ids_per_feed).save(state)

        logger.info("RSS updates: feeds={} items={}", len(updates), total_items)
        return updates

    async def _fetch_feed(
        self,
        session: aiohttp.ClientSession,
        url: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[str, feedparser.FeedParserDict | None]:
        async with semaphore:
            try:
                async with session.get(url) as resp:
                    if resp.status >= 400:
                        logger.warning("RSS fetch failed: {} HTTP {}", url, resp.status)
                        return url, None
                    content = await resp.read()
            except Exception as exc:
                logger.warning("RSS fetch error: {} ({})", url, exc)
                return url, None

        try:
            parsed = feedparser.parse(content)
        except Exception as exc:
            logger.warning("RSS parse error: {} ({})", url, exc)
            return url, None

        if not parsed.entries:
            return url, None
        return url, parsed

    async def _summarize_updates(self, updates: list[FeedUpdate]) -> None:
        if not (self._config.translate and self._openai_api_key):
            for update in updates:
                for item in update.items:
                    item.summary_zh = _structured_fallback_summary(
                        item.content_full or item.summary or item.title,
                        self._config.summary_max_chars,
                    )
            if self._config.translate and not self._openai_api_key:
                logger.info("RSS translate enabled but OPENAI_API_KEY missing; using fallback.")
            return

        timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)
        semaphore = asyncio.Semaphore(max(1, self._config.summary_concurrency))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                self._summarize_item(session, semaphore, item)
                for update in updates
                for item in update.items
            ]
            await asyncio.gather(*tasks)

    async def _populate_fulltext(self, updates: list[FeedUpdate]) -> None:
        timeout = aiohttp.ClientTimeout(total=self._config.fulltext_timeout_seconds)
        headers = {"User-Agent": self._config.user_agent}
        semaphore = asyncio.Semaphore(max(1, self._config.fulltext_concurrency))
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            tasks = [
                self._fetch_fulltext_item(session, semaphore, item)
                for update in updates
                for item in update.items
            ]
            await asyncio.gather(*tasks)

    async def _fetch_fulltext_item(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        item: RssItem,
    ) -> None:
        link = (item.link or "").strip()
        if not link:
            return
        async with semaphore:
            try:
                async with session.get(link) as resp:
                    if resp.status >= 400:
                        logger.warning("RSS fulltext fetch failed: {} HTTP {}", link, resp.status)
                        return
                    html = await resp.text(errors="ignore")
            except Exception as exc:
                logger.warning("RSS fulltext fetch error: {} ({})", link, exc)
                return

        try:
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                output_format="txt",
            )
        except Exception as exc:
            logger.warning("RSS fulltext extract error: {} ({})", link, exc)
            return

        if not text:
            return
        cleaned = _clean_text(text)
        if self._config.fulltext_min_chars > 0 and len(cleaned) < self._config.fulltext_min_chars:
            return
        cleaned = _truncate(cleaned, self._config.fulltext_max_chars)
        item.content_full = cleaned

    async def _summarize_item(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        item: RssItem,
    ) -> None:
        fallback = _structured_fallback_summary(
            item.content_full or item.summary or item.title,
            self._config.summary_max_chars,
        )
        source_text = item.content_full or item.summary
        content = _truncate(source_text, self._config.summary_input_chars)
        if not content:
            item.summary_zh = fallback
            return

        payload = {
            "model": self._config.openai_model,
            "temperature": self._config.openai_temperature,
            "max_tokens": self._config.openai_max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是信息摘要助手。请将内容提炼为中文分段总结，"
                        "保留关键术语、专有名词和数字，细节段可以适当多一些。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"标题：{item.title}\n"
                        f"内容：{content}\n"
                        "要求：输出三段，格式固定为：\n"
                        "要点：...\n"
                        "细节：...\n"
                        "影响：...\n"
                        f"总字数尽量不超过{self._config.summary_max_chars}字。"
                    ),
                },
            ],
        }

        url = f"{self._openai_base_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self._openai_api_key}"}

        async with semaphore:
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status >= 400:
                        logger.warning("RSS summarize failed: HTTP {}", resp.status)
                        item.summary_zh = fallback
                        return
                    data = await resp.json()
            except Exception as exc:
                logger.warning("RSS summarize error: {}", exc)
                item.summary_zh = fallback
                return

        summary = _extract_openai_text(data)
        item.summary_zh = _normalize_structured_summary(
            summary,
            self._config.summary_max_chars,
            fallback,
        )


@dataclass(slots=True)
class _RssState:
    feeds: dict[str, dict]
    updated_at: str | None = None


class _RssStateStore:
    def __init__(self, path: Path, max_ids_per_feed: int) -> None:
        self._path = path
        self._max_ids_per_feed = max_ids_per_feed

    def load(self) -> _RssState:
        if not self._path.exists():
            return _RssState(feeds={})
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("RSS state load failed; reset: {}", self._path)
            return _RssState(feeds={})
        feeds = payload.get("feeds") if isinstance(payload, dict) else None
        if not isinstance(feeds, dict):
            feeds = {}
        return _RssState(feeds=feeds, updated_at=payload.get("updated_at"))

    def save(self, state: _RssState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": state.updated_at,
            "feeds": state.feeds,
        }
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self._path)


def _read_feeds(path: Path) -> list[str]:
    content = path.read_text(encoding="utf-8")
    feeds: list[str] = []
    for line in content.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        feeds.append(raw)
    seen: set[str] = set()
    unique: list[str] = []
    for feed in feeds:
        if feed in seen:
            continue
        seen.add(feed)
        unique.append(feed)
    return unique


def _extract_items(
    feed_url: str,
    feed_title: str,
    parsed: feedparser.FeedParserDict,
) -> list[RssItem]:
    items: list[RssItem] = []
    for entry in parsed.entries:
        title = str(entry.get("title") or "").strip()
        link = str(entry.get("link") or entry.get("id") or feed_url).strip()
        summary = _extract_entry_text(entry)
        published_at = _entry_datetime(entry)
        published = published_at.isoformat() if published_at else None
        item_id_source = str(entry.get("id") or entry.get("guid") or link or title)
        if not item_id_source:
            item_id_source = f"{title}|{published or ''}"
        digest = hashlib.sha256(f"{feed_url}|{item_id_source}".encode("utf-8")).hexdigest()
        items.append(
            RssItem(
                feed_url=feed_url,
                feed_title=feed_title,
                title=title or "(untitled)",
                link=link or feed_url,
                summary=summary,
                published=published,
                item_id=digest,
                published_at=published_at,
            )
        )
    return items


def _extract_entry_text(entry: dict) -> str:
    if not entry:
        return ""
    summary = entry.get("summary") or entry.get("description")
    if summary:
        return _clean_text(str(summary))
    content = entry.get("content")
    if isinstance(content, list) and content:
        value = content[0].get("value") if isinstance(content[0], dict) else None
        if value:
            return _clean_text(str(value))
    return ""


def _entry_datetime(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _clean_text(value: str) -> str:
    text = unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _truncate(value: str, max_chars: int) -> str:
    if not value:
        return ""
    if max_chars <= 0:
        return value
    return value[:max_chars]


def _fallback_summary(item: RssItem, max_chars: int) -> str:
    summary = _truncate(item.content_full or item.summary, max_chars)
    if summary:
        return summary
    return _truncate(item.title, max_chars)


def _structured_fallback_summary(text: str, max_chars: int) -> str:
    base = _clean_text(text)
    if not base:
        return ""
    sentences = _split_sentences(base)
    if not sentences:
        return _truncate(base, max_chars)
    key = sentences[0]
    details = " ".join(sentences[1:3]) or sentences[0]
    impact = sentences[-1] if len(sentences) > 1 else sentences[0]
    summary = "\n".join(
        [
            f"要点：{key}",
            f"细节：{details}",
            f"影响：{impact}",
        ]
    )
    return _truncate(summary, max_chars)


def _normalize_structured_summary(summary: str | None, max_chars: int, fallback: str) -> str:
    if not summary:
        return fallback
    text = summary.strip().replace("\r\n", "\n").replace("\r", "\n")
    if not any(label in text for label in ("要点：", "细节：", "影响：")):
        return fallback
    text = _truncate(text, max_chars)
    return text


def _extract_openai_text(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not content:
        return None
    return str(content).strip()


def _sort_key(item: RssItem) -> float:
    if item.published_at:
        return item.published_at.timestamp()
    return 0.0


def _format_digest(updates: Iterable[FeedUpdate]) -> str:
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines: list[str] = [f"RSS 晨间更新（{date_str}）"]
    for update in updates:
        lines.append("")
        lines.append(f"【{update.feed_title}】")
        for item in update.items:
            summary = item.summary_zh or _structured_fallback_summary(
                item.content_full or item.summary or item.title,
                400,
            )
            lines.append(f"- {item.title}")
            lines.append("  摘要：")
            for line in summary.splitlines():
                lines.append(f"  {line}")
            lines.append(f"  链接：{item.link}")
    return "\n".join(lines).strip()


def _format_digest_markdown(updates: Iterable[FeedUpdate]) -> str:
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines: list[str] = [f"# RSS 晨间更新（{date_str}）"]
    for update in updates:
        lines.append("")
        lines.append(f"## {update.feed_title}")
        for item in update.items:
            summary = item.summary_zh or _structured_fallback_summary(
                item.content_full or item.summary or item.title,
                400,
            )
            lines.append(f"- **{item.title}**")
            for line in summary.splitlines():
                if line.strip():
                    lines.append(f"  - {line.strip()}")
            link = item.link
            lines.append(f"  - 链接：[原文]({link})")
    return "\n".join(lines).strip()


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    parts = re.split(r"[。！？.!?]\\s*", text)
    return [p.strip() for p in parts if p.strip()]


def _split_message(text: str, max_len: int = 3500) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines():
        extra = len(line) + 1
        if length + extra > max_len and current:
            parts.append("\n".join(current))
            current = [line]
            length = len(line) + 1
        else:
            current.append(line)
            length += extra
    if current:
        parts.append("\n".join(current))
    return parts


def _resolve_path(path: str, config_path: str | None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute() or not config_path:
        return candidate
    return Path(config_path).expanduser().parent / candidate
