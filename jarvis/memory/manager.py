from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import aiosqlite

from jarvis.config import MemoryConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MemorySearchResult:
    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str


@dataclass(slots=True)
class MemoryFileEntry:
    path: str
    abs_path: Path
    mtime: int
    size: int
    hash: str
    content: str


@dataclass(slots=True)
class MemoryChunk:
    start_line: int
    end_line: int
    text: str
    hash: str


class MemoryManager:
    def __init__(self, config: MemoryConfig) -> None:
        self._config = config
        self._workspace_dir = Path(config.workspace_dir).expanduser()
        self._db_path = Path(config.db_path).expanduser()
        self._conn: aiosqlite.Connection | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_dir

    async def connect(self) -> None:
        if not self.enabled:
            return
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._workspace_dir.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self._db_path)
            await self._conn.execute("PRAGMA journal_mode=WAL;")
            await self._conn.execute("PRAGMA foreign_keys=ON;")
            await self._ensure_schema()
        except Exception:
            logger.exception("Memory init failed; disabling memory feature")
            if self._conn:
                await self._conn.close()
            self._conn = None
            self._config.enabled = False

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def sync(self, force: bool = False) -> None:
        if not self.enabled:
            return
        conn = self._require_conn()
        files = await self._list_memory_files()
        active_paths = {entry.path for entry in files}

        for entry in files:
            if not force:
                existing = await self._fetchone(
                    "SELECT hash FROM files WHERE path = ?",
                    (entry.path,),
                )
                if existing and existing[0] == entry.hash:
                    continue
            await self._index_file(entry)

        stale_rows = await self._fetchall("SELECT path FROM files")
        for (path,) in stale_rows:
            if path in active_paths:
                continue
            await conn.execute("DELETE FROM files WHERE path = ?", (path,))
            await conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
            await conn.execute("DELETE FROM chunks_fts WHERE path = ?", (path,))

        await conn.commit()

    async def search(self, query: str, max_results: int | None = None) -> list[MemorySearchResult]:
        if not self.enabled:
            return []
        cleaned = query.strip()
        if not cleaned:
            return []
        await self.sync()
        fts_query = _build_fts_query(cleaned)
        if not fts_query:
            return []
        limit = max_results or self._config.max_results
        try:
            rows = await self._fetchall(
                """
                SELECT path, start_line, end_line, text, bm25(chunks_fts) AS rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (fts_query, limit),
            )
        except sqlite3.OperationalError as exc:
            logger.warning("FTS query failed; fallback to LIKE search: %s", exc)
            rows = await self._fetchall(
                """
                SELECT path, start_line, end_line, text, 0.0 AS rank
                FROM chunks
                WHERE text LIKE ?
                LIMIT ?
                """,
                (f"%{cleaned}%", limit),
            )
        results: list[MemorySearchResult] = []
        for path, start_line, end_line, text, rank in rows:
            snippet = _truncate_text(text, self._config.snippet_chars)
            score = _bm25_rank_to_score(rank)
            results.append(
                MemorySearchResult(
                    path=str(path),
                    start_line=int(start_line),
                    end_line=int(end_line),
                    score=score,
                    snippet=snippet,
                )
            )
        return results

    async def read_snippet(
        self, path: str, from_line: int | None = None, lines: int | None = None
    ) -> str:
        if not self.enabled:
            return ""
        abs_path = self._resolve_memory_path(path)
        content = abs_path.read_text(encoding="utf-8")
        if not from_line and not lines:
            return content
        start = max(1, from_line or 1)
        count = max(1, lines or 1)
        parts = content.splitlines()
        slice_lines = parts[start - 1 : start - 1 + count]
        return "\n".join(slice_lines)

    async def append_daily(self, text: str) -> Path | None:
        if not self.enabled:
            return None
        content = text.strip()
        if not content:
            return None
        target = self._daily_memory_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().strftime("%H:%M")
        line = f"- [{now}] {content}"
        with target.open("a", encoding="utf-8") as handle:
            if target.stat().st_size > 0:
                handle.write("\n")
            handle.write(line)
        return target

    async def append_daily_block(self, text: str, title: str = "memory") -> Path | None:
        if not self.enabled:
            return None
        content = text.strip()
        if not content:
            return None
        target = self._daily_memory_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().strftime("%H:%M")
        header = f"## {now} {title}".strip()
        block = f"{header}\n{content}".rstrip()
        with target.open("a", encoding="utf-8") as handle:
            if target.stat().st_size > 0:
                handle.write("\n\n")
            handle.write(block)
        return target

    async def append_global_block(self, text: str, title: str | None = None) -> Path | None:
        if not self.enabled:
            return None
        content = text.strip()
        if not content:
            return None
        target = self._workspace_dir / "MEMORY.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        header = f"## {title}".strip() if title else "## Consolidated"
        block = f"{header}\n{content}".rstrip()
        with target.open("a", encoding="utf-8") as handle:
            if target.stat().st_size > 0:
                handle.write("\n\n")
            handle.write(block)
        return target

    async def status(self) -> dict[str, int]:
        if not self.enabled:
            return {"files": 0, "chunks": 0}
        row = await self._fetchone("SELECT COUNT(*) FROM files")
        files = int(row[0]) if row else 0
        row = await self._fetchone("SELECT COUNT(*) FROM chunks")
        chunks = int(row[0]) if row else 0
        return {"files": files, "chunks": chunks}

    async def _ensure_schema(self) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                mtime INTEGER NOT NULL,
                size INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                hash TEXT NOT NULL,
                text TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);")
        await conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text,
                path UNINDEXED,
                start_line UNINDEXED,
                end_line UNINDEXED
            );
            """
        )
        await conn.commit()

    async def _index_file(self, entry: MemoryFileEntry) -> None:
        conn = self._require_conn()
        chunks = _chunk_text(entry.content, self._config.chunk_chars)
        now = int(datetime.now().timestamp())
        await conn.execute("DELETE FROM chunks WHERE path = ?", (entry.path,))
        await conn.execute("DELETE FROM chunks_fts WHERE path = ?", (entry.path,))
        for chunk in chunks:
            chunk_id = _hash_text(
                f"{entry.path}:{chunk.start_line}:{chunk.end_line}:{chunk.hash}"
            )
            await conn.execute(
                """
                INSERT INTO chunks (id, path, start_line, end_line, hash, text, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    entry.path,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.hash,
                    chunk.text,
                    now,
                ),
            )
            await conn.execute(
                """
                INSERT INTO chunks_fts (text, path, start_line, end_line)
                VALUES (?, ?, ?, ?)
                """,
                (chunk.text, entry.path, chunk.start_line, chunk.end_line),
            )
        await conn.execute(
            """
            INSERT INTO files (path, hash, mtime, size, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                hash=excluded.hash,
                mtime=excluded.mtime,
                size=excluded.size,
                updated_at=excluded.updated_at
            """,
            (entry.path, entry.hash, entry.mtime, entry.size, now),
        )

    async def _list_memory_files(self) -> list[MemoryFileEntry]:
        files = list(_iter_memory_files(self._workspace_dir, self._config.extra_paths))
        entries: list[MemoryFileEntry] = []
        for abs_path in files:
            try:
                stat = abs_path.stat()
                content = abs_path.read_text(encoding="utf-8")
            except OSError:
                continue
            path = _relative_to_workspace(abs_path, self._workspace_dir)
            entries.append(
                MemoryFileEntry(
                    path=path,
                    abs_path=abs_path,
                    mtime=int(stat.st_mtime),
                    size=int(stat.st_size),
                    hash=_hash_text(content),
                    content=content,
                )
            )
        return entries

    def _resolve_memory_path(self, value: str) -> Path:
        raw = value.strip()
        if not raw:
            raise ValueError("path required")
        abs_path = Path(raw).expanduser()
        if not abs_path.is_absolute():
            abs_path = (self._workspace_dir / abs_path).resolve()
        else:
            abs_path = abs_path.resolve()

        if not abs_path.name.endswith(".md"):
            raise ValueError("path required")
        if abs_path.is_symlink() or not abs_path.is_file():
            raise ValueError("path required")

        rel_path = _safe_relative(abs_path, self._workspace_dir)
        if rel_path and _is_workspace_memory_path(rel_path):
            return abs_path

        if self._is_allowed_extra_path(abs_path):
            return abs_path

        raise ValueError("path required")

    def _is_allowed_extra_path(self, abs_path: Path) -> bool:
        for raw in self._config.extra_paths:
            if not raw:
                continue
            extra = Path(raw).expanduser()
            if not extra.is_absolute():
                extra = (self._workspace_dir / extra).resolve()
            else:
                extra = extra.resolve()
            if not extra.exists() or extra.is_symlink():
                continue
            if extra.is_dir():
                try:
                    abs_path.relative_to(extra)
                    return True
                except ValueError:
                    continue
            if extra.is_file() and extra.name.endswith(".md") and abs_path == extra:
                return True
        return False

    def _daily_memory_path(self) -> Path:
        day = datetime.now().strftime("%Y-%m-%d")
        return self._workspace_dir / "memory" / f"{day}.md"

    def _require_conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Memory database not connected")
        return self._conn

    async def _fetchone(self, query: str, params: tuple | None = None):
        conn = self._require_conn()
        async with conn.execute(query, params or ()) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, query: str, params: tuple | None = None):
        conn = self._require_conn()
        async with conn.execute(query, params or ()) as cursor:
            return await cursor.fetchall()


def _iter_memory_files(workspace_dir: Path, extra_paths: Iterable[str]) -> Iterable[Path]:
    seen: set[str] = set()

    def add_file(candidate: Path) -> None:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                return
            if candidate.suffix.lower() != ".md":
                return
            key = str(candidate.resolve())
            if key in seen:
                return
            seen.add(key)
            yield_files.append(candidate)
        except OSError:
            return

    def walk_dir(root: Path) -> None:
        try:
            for entry in root.iterdir():
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    walk_dir(entry)
                elif entry.is_file() and entry.suffix.lower() == ".md":
                    add_file(entry)
        except OSError:
            return

    yield_files: list[Path] = []

    add_file(workspace_dir / "MEMORY.md")
    add_file(workspace_dir / "memory.md")
    memory_dir = workspace_dir / "memory"
    if memory_dir.exists() and memory_dir.is_dir() and not memory_dir.is_symlink():
        walk_dir(memory_dir)

    for raw in extra_paths:
        if not raw:
            continue
        extra = Path(raw).expanduser()
        if not extra.is_absolute():
            extra = (workspace_dir / extra).resolve()
        else:
            extra = extra.resolve()
        if not extra.exists() or extra.is_symlink():
            continue
        if extra.is_dir():
            walk_dir(extra)
        elif extra.is_file():
            add_file(extra)

    return yield_files


def _chunk_text(content: str, max_chars: int) -> list[MemoryChunk]:
    lines = content.splitlines()
    if not lines:
        return []
    chunks: list[MemoryChunk] = []
    buf: list[str] = []
    start_line = 1
    size = 0
    limit = max(64, max_chars)

    def flush(end_line: int) -> None:
        nonlocal buf, size, start_line
        if not buf:
            return
        text = "\n".join(buf).strip()
        buf = []
        size = 0
        if not text:
            start_line = end_line + 1
            return
        chunks.append(
            MemoryChunk(
                start_line=start_line,
                end_line=end_line,
                text=text,
                hash=_hash_text(text),
            )
        )
        start_line = end_line + 1

    for idx, line in enumerate(lines, start=1):
        line_size = len(line) + 1
        if size + line_size > limit and buf:
            flush(idx - 1)
        buf.append(line)
        size += line_size
    flush(len(lines))
    return chunks


def _build_fts_query(query: str) -> str:
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    if not terms:
        return ""
    safe_terms = []
    for term in terms:
        escaped = term.replace('"', '""')
        safe_terms.append(f'"{escaped}"')
    return " AND ".join(safe_terms)


def _bm25_rank_to_score(rank: float) -> float:
    try:
        rank_value = float(rank)
    except (TypeError, ValueError):
        return 0.0
    return 1.0 / (1.0 + max(rank_value, 0.0))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate_text(text: str, max_chars: int) -> str:
    limit = max(20, max_chars)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _relative_to_workspace(abs_path: Path, workspace_dir: Path) -> str:
    rel = _safe_relative(abs_path, workspace_dir)
    if rel:
        return rel.replace(os.sep, "/")
    return str(abs_path)


def _safe_relative(abs_path: Path, workspace_dir: Path) -> str | None:
    try:
        rel = abs_path.relative_to(workspace_dir)
    except ValueError:
        return None
    return str(rel)


def _is_workspace_memory_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    if not normalized:
        return False
    if normalized in {"MEMORY.md", "memory.md"}:
        return True
    return normalized.startswith("memory/")
