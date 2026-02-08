#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import aiohttp
import trafilatura

from jarvis.config import load_config


@dataclass(slots=True)
class RadarItem:
    item_id: str
    source: str
    title: str
    summary: str
    url: str
    score: int = 0
    meta: str | None = None


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_latest(pattern: str) -> Path | None:
    matches = sorted(Path("/home/nazgul/jarvis").glob(pattern), reverse=True)
    return matches[0] if matches else None


def _load_twitter_items(path: Path, limit: int = 40) -> list[RadarItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items: list[RadarItem] = []
    for raw in payload:
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        author = raw.get("author") or {}
        username = author.get("username") or "unknown"
        tweet_id = str(raw.get("id") or "")
        if not tweet_id:
            continue
        url = f"https://x.com/{username}/status/{tweet_id}"
        like_count = int(raw.get("likeCount") or 0)
        reply_count = int(raw.get("replyCount") or 0)
        retweet_count = int(raw.get("retweetCount") or 0)
        score = like_count + 2 * retweet_count + reply_count
        title = text if len(text) <= 80 else text[:77] + "..."
        meta = f"likes={like_count} replies={reply_count} retweets={retweet_count}"
        items.append(
            RadarItem(
                item_id=f"tw_{tweet_id}",
                source="twitter",
                title=title,
                summary=text,
                url=url,
                score=score,
                meta=meta,
            )
        )
    items.sort(key=lambda x: x.score, reverse=True)
    return items[:limit]


def _load_hn_items(path: Path, limit: int = 30) -> list[RadarItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items = payload.get("items") or []
    items: list[RadarItem] = []
    for raw in raw_items:
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        if not title or not url:
            continue
        score = int(raw.get("score") or 0)
        comments = int(raw.get("descendants") or 0)
        meta = f"score={score} comments={comments}"
        items.append(
            RadarItem(
                item_id=f"hn_{raw.get('id')}",
                source="hn",
                title=title,
                summary=raw.get("story_text") or raw.get("article_text") or title,
                url=url,
                score=score,
                meta=meta,
            )
        )
    items.sort(key=lambda x: x.score, reverse=True)
    return items[:limit]


def _parse_rss_md(path: Path, limit: int = 40) -> list[RadarItem]:
    lines = path.read_text(encoding="utf-8").splitlines()
    items: list[RadarItem] = []
    current: dict[str, str] = {}
    for line in lines:
        if line.startswith("### "):
            if current:
                items.append(_rss_item_from_block(current))
            title = line[4:].strip()
            current = {"title": title, "summary": "", "url": ""}
            continue
        if line.startswith("- **要点**："):
            current["summary"] = line.split("：", 1)[1].strip()
            continue
        if line.startswith("- 原文"):
            match = re.search(r"\((https?://[^)]+)\)", line)
            if match:
                current["url"] = match.group(1)
            continue
    if current:
        items.append(_rss_item_from_block(current))
    return [item for item in items if item.title and item.url][:limit]


def _rss_item_from_block(block: dict[str, str]) -> RadarItem:
    title = block.get("title", "").strip()
    url = block.get("url", "").strip()
    summary = block.get("summary", "").strip() or title
    item_id = f"rss_{hashlib.sha256((title + url).encode('utf-8')).hexdigest()[:12]}"
    return RadarItem(
        item_id=item_id,
        source="rss",
        title=title,
        summary=summary,
        url=url,
        score=0,
        meta=None,
    )


def _normalize_key(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[\W_]+", "", lowered)
    return lowered[:80] or "unknown"


def _state_path() -> Path:
    return Path("/home/nazgul/jarvis/reports/radar/radar_state.json")


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {"version": 1, "topics": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "topics": {}}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _should_process(topic_key: str, sources: list[str], state: dict) -> bool:
    topics = state.get("topics", {})
    entry = topics.get(topic_key)
    if not entry:
        return True
    old_sources = set(entry.get("sources") or [])
    new_sources = set(sources) - old_sources
    return bool(new_sources)


def _update_state(topic_key: str, title: str, sources: list[str], state: dict) -> None:
    topics = state.setdefault("topics", {})
    entry = topics.get(topic_key)
    now = _now_utc_iso()
    if not entry:
        topics[topic_key] = {
            "title": title,
            "sources": sorted(set(sources)),
            "first_seen": now,
            "last_seen": now,
            "evidence_count": len(set(sources)),
        }
        return
    merged = sorted(set(entry.get("sources") or []) | set(sources))
    entry["sources"] = merged
    entry["last_seen"] = now
    entry["evidence_count"] = len(merged)


async def _openai_chat(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> str:
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    async with session.post(url, json=payload, headers=headers) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"OpenAI HTTP {resp.status}")
        data = await resp.json()
    return _extract_openai_text(data) or ""


def _extract_openai_text(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not choices or not isinstance(choices, list):
        return None
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")
    return content.strip() if isinstance(content, str) else None


def _extract_json_block(text: str) -> dict | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _shorten(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


async def _search_duckduckgo(
    session: aiohttp.ClientSession, query: str, max_results: int = 5
) -> list[str]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    async with session.get(url) as resp:
        if resp.status >= 400:
            return []
        html = await resp.text()
    links = re.findall(r'class="result__a"[^>]+href="([^"]+)"', html)
    results: list[str] = []
    for link in links:
        decoded = _decode_ddg_url(link)
        if not decoded:
            continue
        if decoded.startswith("http://") or decoded.startswith("https://"):
            results.append(decoded)
        if len(results) >= max_results:
            break
    return results


def _decode_ddg_url(url: str) -> str | None:
    if "duckduckgo.com/l/" not in url:
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    uddg = qs.get("uddg")
    if not uddg:
        return None
    return unquote(uddg[0])


async def _fetch_fulltext(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url) as resp:
            if resp.status >= 400:
                return None
            html = await resp.text()
    except Exception:
        return None
    text = trafilatura.extract(
        html, include_comments=False, include_tables=False, favor_recall=True
    )
    return text.strip() if text else None


def _render_pdf(
    md_path: Path, pdf_path: Path, template_path: Path, pdf_engine: str, timeout: int
) -> None:
    cmd = [
        "pandoc",
        str(md_path),
        "-o",
        str(pdf_path),
        f"--pdf-engine={pdf_engine}",
        f"--template={template_path}",
    ]
    subprocess.run(cmd, check=True, timeout=timeout)


async def _select_topics(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    items: list[RadarItem],
) -> list[dict]:
    payload_items = [
        {
            "id": item.item_id,
            "source": item.source,
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
            "meta": item.meta,
        }
        for item in items
    ]
    system = (
        "你是洞察雷达筛选器。目标是从多源内容中挑选1-3个最值得深度解读的主题。"
        "优先新颖性/影响力/争议性/与你关注主题相关度（AI、开发工具、创业）。"
        "不要机械按规则评分，允许判断与归纳。"
    )
    user = (
        "请从候选条目中选出1-3个主题，每个主题包含："
        "title, reason, tags, item_ids, search_queries(2-3条)。"
        '只输出JSON，格式如下：{"topics":[...]}\n\n候选条目：'
        f"{json.dumps(payload_items, ensure_ascii=False)}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    text = await _openai_chat(
        session,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=messages,
    )
    data = _extract_json_block(text) or {}
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list):
        return []
    return topics


async def _build_topic_report(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    topic: dict,
    items_by_id: dict[str, RadarItem],
    extra_sources: list[dict],
) -> str:
    title = str(topic.get("title") or "未命名主题").strip()
    item_ids = topic.get("item_ids") or []
    signals: list[str] = []
    for item_id in item_ids:
        item = items_by_id.get(str(item_id))
        if not item:
            continue
        meta = f"（{item.meta}）" if item.meta else ""
        signals.append(f"- [{item.source}] {item.title} {meta}\n  {item.summary}\n  {item.url}")
    evidence_blocks: list[str] = []
    for src in extra_sources:
        snippet = _shorten(src.get("content") or "", 1200)
        evidence_blocks.append(f"- {src.get('url')}\n  {snippet}")
    system = (
        "你是深度解读作者。请根据给定信号与补充证据，写出结构化分析。"
        "输出为Markdown，仅包含以下小节标题：\n"
        "### 概述\n### 证据与来源\n### 影响与机会\n### 风险与反对观点\n### 可观察指标\n### 参考来源"
    )
    signals_text = chr(10).join(signals) if signals else "（无）"
    evidence_text = (
        chr(10).join(evidence_blocks) if evidence_blocks else "（补充证据不足，请注明证据受限）"
    )
    user = (
        f"主题：{title}\n\n"
        f"候选信号：\n{signals_text}\n\n"
        f"补充证据：\n{evidence_text}\n\n"
        "要求：输出中文，逻辑清晰；若证据不足请明确说明。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return await _openai_chat(
        session,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=messages,
    )


async def _gather_extra_sources(
    session: aiohttp.ClientSession,
    *,
    topic: dict,
    existing_urls: set[str],
    max_sources: int = 4,
) -> list[dict]:
    queries = topic.get("search_queries") or []
    if not queries:
        queries = [str(topic.get("title") or "")]
    candidates: list[str] = []
    for query in queries:
        if not query:
            continue
        urls = await _search_duckduckgo(session, str(query), max_results=5)
        candidates.extend(urls)
    deduped: list[str] = []
    for url in candidates:
        if url in existing_urls:
            continue
        if urlparse(url).netloc in {"x.com", "twitter.com", "news.ycombinator.com"}:
            continue
        if url not in deduped:
            deduped.append(url)
        if len(deduped) >= max_sources:
            break
    results: list[dict] = []
    for url in deduped:
        text = await _fetch_fulltext(session, url)
        if not text:
            continue
        results.append({"url": url, "content": text})
    return results


async def run_radar(mode: str, config_path: Path) -> int:
    config = load_config(config_path)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Radar failed: missing OPENAI_API_KEY")
        return 1
    base_url = config.openai.base_url
    model = config.rss.openai_model
    temperature = config.rss.openai_temperature
    max_tokens = min(config.rss.openai_max_tokens, 2048)

    twitter_path = _find_latest("reports/twitter/twitter_for_you_raw_*.json")
    hn_path = _find_latest("reports/hn/hn_raw_*.json")
    rss_path = _find_latest("reports/rss/rss-digest-*-dated.md") or _find_latest(
        "reports/rss/rss-digest-*.md"
    )

    items: list[RadarItem] = []
    if twitter_path and twitter_path.exists():
        items.extend(_load_twitter_items(twitter_path))
    if hn_path and hn_path.exists():
        items.extend(_load_hn_items(hn_path))
    if rss_path and rss_path.exists():
        items.extend(_parse_rss_md(rss_path))

    if not items:
        if mode == "heartbeat":
            print("HEARTBEAT_OK")
            return 0
        print("Radar: no items found.")
        return 0

    items_by_id = {item.item_id: item for item in items}

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        topics = await _select_topics(
            session,
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            items=items,
        )

        if not topics:
            if mode == "heartbeat":
                print("HEARTBEAT_OK")
                return 0
            print("Radar: no topics selected.")
            return 0

        state = _load_state()
        selected: list[dict] = []
        for topic in topics[:3]:
            title = str(topic.get("title") or "").strip()
            if not title:
                continue
            item_ids = [str(x) for x in (topic.get("item_ids") or [])]
            sources = [items_by_id[x].url for x in item_ids if x in items_by_id]
            topic_key = _normalize_key(title)
            if _should_process(topic_key, sources, state):
                selected.append({"topic": topic, "key": topic_key, "sources": sources})

        if not selected:
            if mode == "heartbeat":
                print("HEARTBEAT_OK")
                return 0
            print("Radar: topics already processed.")
            return 0

        report_sections: list[str] = []
        for entry in selected:
            topic = entry["topic"]
            title = str(topic.get("title") or "未命名主题").strip()
            existing_urls = set(entry["sources"])
            extra_sources = await _gather_extra_sources(
                session,
                topic=topic,
                existing_urls=existing_urls,
                max_sources=4,
            )
            section = await _build_topic_report(
                session,
                base_url=base_url,
                api_key=api_key,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                topic=topic,
                items_by_id=items_by_id,
                extra_sources=extra_sources,
            )
            report_sections.append(f"## {title}\n\n{section}\n")
            all_sources = entry["sources"] + [src.get("url") for src in extra_sources]
            _update_state(entry["key"], title, [s for s in all_sources if s], state)

    _save_state(state)

    now = _now_local()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    output_dir = Path("/home/nazgul/jarvis/reports/radar")
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"radar_report_{stamp}.md"
    pdf_path = output_dir / f"radar_report_{stamp}.pdf"

    header = f"# Insight Radar（{now.strftime('%Y-%m-%d %H:%M')}）\n"
    md_path.write_text(header + "\n" + "\n".join(report_sections), encoding="utf-8")

    template_path = Path("/home/nazgul/jarvis/reports/templates/zh_template.tex")
    try:
        _render_pdf(md_path, pdf_path, template_path, "lualatex", timeout=180)
    except Exception as exc:
        print(f"Radar PDF 生成失败：{exc}")
        print(f"Markdown 已生成：{md_path}")
        return 1

    titles = [str(entry["topic"].get("title") or "").strip() for entry in selected]
    titles = [t for t in titles if t]
    summary = "；".join(titles[:3])
    print(f"洞察雷达命中 {len(titles)} 个主题：{summary}。已生成深度解读 PDF。")
    print(f"send_to_user:///{pdf_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Insight Radar runner")
    parser.add_argument("--mode", default="manual", choices=["manual", "heartbeat"])
    parser.add_argument("--config", default="/home/nazgul/jarvis/config.yaml")
    args = parser.parse_args()
    return asyncio.run(run_radar(args.mode, Path(args.config)))


if __name__ == "__main__":
    raise SystemExit(main())
