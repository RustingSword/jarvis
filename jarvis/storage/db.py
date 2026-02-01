from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import aiosqlite

from jarvis.config import StorageConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionRecord:
    chat_id: str
    session_id: int
    thread_id: str
    created_at: datetime
    last_active: datetime


@dataclass(slots=True)
class MessageSession:
    session_id: int
    thread_id: str


@dataclass(slots=True)
class MonitorRecord:
    id: int
    chat_id: str
    type: str
    threshold: float
    interval_seconds: int
    enabled: bool


class Storage:
    def __init__(self, config: StorageConfig) -> None:
        self._db_path = Path(config.db_path).expanduser()
        self._session_dir = Path(config.session_dir).expanduser()
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id TEXT PRIMARY KEY,
                session_id INTEGER,
                thread_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active TEXT NOT NULL
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active TEXT NOT NULL
            );
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_history_chat ON session_history(chat_id);"
        )
        await self._ensure_session_columns()
        await self._migrate_sessions()
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                type TEXT NOT NULL,
                threshold REAL NOT NULL,
                interval_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, key)
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_sessions_lookup ON "
            "message_sessions(chat_id, message_id);"
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def get_session(self, chat_id: str) -> Optional[SessionRecord]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT chat_id, session_id, thread_id, created_at, last_active "
            "FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        if row[1] is None:
            return None
        return SessionRecord(
            chat_id=row[0],
            session_id=int(row[1]),
            thread_id=row[2],
            created_at=_parse_ts(row[3]),
            last_active=_parse_ts(row[4]),
        )

    async def get_session_by_id(self, chat_id: str, session_id: int) -> Optional[SessionRecord]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, chat_id, thread_id, created_at, last_active
            FROM session_history
            WHERE chat_id = ? AND id = ?
            """,
            (chat_id, session_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return SessionRecord(
            chat_id=row[1],
            session_id=int(row[0]),
            thread_id=row[2],
            created_at=_parse_ts(row[3]),
            last_active=_parse_ts(row[4]),
        )

    async def list_sessions(self, chat_id: str, limit: int = 10) -> list[SessionRecord]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, chat_id, thread_id, created_at, last_active
            FROM session_history
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            SessionRecord(
                chat_id=row[1],
                session_id=int(row[0]),
                thread_id=row[2],
                created_at=_parse_ts(row[3]),
                last_active=_parse_ts(row[4]),
            )
            for row in rows
        ]

    async def get_session_by_thread_id(
        self, chat_id: str, thread_id: str
    ) -> Optional[SessionRecord]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, chat_id, thread_id, created_at, last_active
            FROM session_history
            WHERE chat_id = ? AND thread_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, thread_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return SessionRecord(
            chat_id=row[1],
            session_id=int(row[0]),
            thread_id=row[2],
            created_at=_parse_ts(row[3]),
            last_active=_parse_ts(row[4]),
        )

    async def activate_session(self, chat_id: str, session_id: int) -> Optional[SessionRecord]:
        record = await self.get_session_by_id(chat_id, session_id)
        if not record:
            return None
        conn = self._require_conn()
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            "UPDATE session_history SET last_active = ? WHERE id = ?",
            (now, session_id),
        )
        await conn.execute(
            """
            INSERT INTO sessions (chat_id, session_id, thread_id, created_at, last_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                session_id = excluded.session_id,
                thread_id = excluded.thread_id,
                created_at = excluded.created_at,
                last_active = excluded.last_active
            """,
            (chat_id, session_id, record.thread_id, record.created_at.isoformat(), now),
        )
        await conn.commit()
        return SessionRecord(
            chat_id=chat_id,
            session_id=session_id,
            thread_id=record.thread_id,
            created_at=record.created_at,
            last_active=_parse_ts(now),
        )

    async def upsert_session(
        self, chat_id: str, thread_id: str, *, set_active: bool = True
    ) -> SessionRecord:
        conn = self._require_conn()
        now = datetime.now(timezone.utc).isoformat()
        existing = await self.get_session_by_thread_id(chat_id, thread_id)
        if existing:
            await conn.execute(
                "UPDATE session_history SET last_active = ? WHERE id = ?",
                (now, existing.session_id),
            )
            if set_active:
                await conn.execute(
                    """
                    INSERT INTO sessions (chat_id, session_id, thread_id, created_at, last_active)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        session_id = excluded.session_id,
                        thread_id = excluded.thread_id,
                        created_at = excluded.created_at,
                        last_active = excluded.last_active
                    """,
                    (chat_id, existing.session_id, thread_id, existing.created_at.isoformat(), now),
                )
            else:
                current = await self.get_session(chat_id)
                if current and current.thread_id == thread_id:
                    await conn.execute(
                        "UPDATE sessions SET last_active = ? WHERE chat_id = ?",
                        (now, chat_id),
                    )
            await conn.commit()
            return SessionRecord(
                chat_id=chat_id,
                session_id=existing.session_id,
                thread_id=thread_id,
                created_at=existing.created_at,
                last_active=_parse_ts(now),
            )

        cursor = await conn.execute(
            """
            INSERT INTO session_history (chat_id, thread_id, created_at, last_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, thread_id, now, now),
        )
        session_id = cursor.lastrowid
        if set_active:
            await conn.execute(
                """
                INSERT INTO sessions (chat_id, session_id, thread_id, created_at, last_active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    thread_id = excluded.thread_id,
                    created_at = excluded.created_at,
                    last_active = excluded.last_active
                """,
                (chat_id, session_id, thread_id, now, now),
            )
        else:
            current = await self.get_session(chat_id)
            if current and current.thread_id == thread_id:
                await conn.execute(
                    "UPDATE sessions SET last_active = ? WHERE chat_id = ?",
                    (now, chat_id),
                )
        await conn.commit()
        return SessionRecord(
            chat_id=chat_id,
            session_id=int(session_id),
            thread_id=thread_id,
            created_at=_parse_ts(now),
            last_active=_parse_ts(now),
        )

    async def clear_session(self, chat_id: str) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
        await conn.commit()

    async def save_message_session(
        self,
        chat_id: str,
        message_id: int,
        session_id: int,
        thread_id: str,
    ) -> None:
        conn = self._require_conn()
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            INSERT INTO message_sessions (chat_id, message_id, session_id, thread_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, int(message_id), int(session_id), thread_id, now),
        )
        await conn.commit()

    async def get_message_session(self, chat_id: str, message_id: int) -> Optional[MessageSession]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT session_id, thread_id
            FROM message_sessions
            WHERE chat_id = ? AND message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, int(message_id)),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return MessageSession(session_id=int(row[0]), thread_id=str(row[1]))

    async def save_summary(self, chat_id: str, summary: str) -> str:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{chat_id}_{uuid4().hex}.summary.txt"
        path = self._session_dir / filename
        path.write_text(summary)
        return str(path)

    async def _ensure_session_columns(self) -> None:
        conn = self._require_conn()
        async with conn.execute("PRAGMA table_info(sessions);") as cursor:
            rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        if "session_id" not in columns:
            await conn.execute("ALTER TABLE sessions ADD COLUMN session_id INTEGER;")

    async def _migrate_sessions(self) -> None:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT chat_id, thread_id, created_at, last_active
            FROM sessions
            WHERE session_id IS NULL
            """
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return
        for row in rows:
            cursor = await conn.execute(
                """
                INSERT INTO session_history (chat_id, thread_id, created_at, last_active)
                VALUES (?, ?, ?, ?)
                """,
                (row[0], row[1], row[2], row[3]),
            )
            session_id = cursor.lastrowid
            await conn.execute(
                "UPDATE sessions SET session_id = ? WHERE chat_id = ?",
                (session_id, row[0]),
            )

    async def list_monitors(self) -> list[MonitorRecord]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, chat_id, type, threshold, interval_seconds, enabled
            FROM monitors
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            MonitorRecord(
                id=row[0],
                chat_id=row[1],
                type=row[2],
                threshold=row[3],
                interval_seconds=row[4],
                enabled=bool(row[5]),
            )
            for row in rows
        ]

    async def get_setting(self, chat_id: str, key: str) -> str | None:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT value FROM settings WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return str(row[0])

    async def set_setting(self, chat_id: str, key: str, value: str) -> None:
        conn = self._require_conn()
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            INSERT INTO settings (chat_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (chat_id, key, value, now),
        )
        await conn.commit()

    async def delete_setting(self, chat_id: str, key: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            "DELETE FROM settings WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        )
        await conn.commit()

    def _require_conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Storage not connected")
        return self._conn


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)
