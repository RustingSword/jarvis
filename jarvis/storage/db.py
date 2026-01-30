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
class TaskRecord:
    id: int
    chat_id: str
    description: str
    status: str
    created_at: datetime
    due_at: datetime | None


@dataclass(slots=True)
class ReminderRecord:
    id: int
    chat_id: str
    message: str
    trigger_time: datetime
    repeat_interval_seconds: int | None


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
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                due_at TEXT
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                message TEXT NOT NULL,
                trigger_time TEXT NOT NULL,
                repeat_interval_seconds INTEGER
            );
            """
        )
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
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def get_session(self, chat_id: str) -> Optional[SessionRecord]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT chat_id, session_id, thread_id, created_at, last_active FROM sessions WHERE chat_id = ?",
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

    async def upsert_session(self, chat_id: str, thread_id: str) -> SessionRecord:
        conn = self._require_conn()
        now = datetime.now(timezone.utc).isoformat()
        current = await self.get_session(chat_id)
        if current and current.thread_id == thread_id:
            await conn.execute(
                "UPDATE sessions SET last_active = ? WHERE chat_id = ?",
                (now, chat_id),
            )
            await conn.execute(
                "UPDATE session_history SET last_active = ? WHERE id = ?",
                (now, current.session_id),
            )
            await conn.commit()
            return SessionRecord(
                chat_id=chat_id,
                session_id=current.session_id,
                thread_id=thread_id,
                created_at=current.created_at,
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

    async def add_task(self, chat_id: str, description: str, due_at: datetime | None) -> int:
        conn = self._require_conn()
        created_at = datetime.now(timezone.utc).isoformat()
        due_value = due_at.isoformat() if due_at else None
        cursor = await conn.execute(
            """
            INSERT INTO tasks (chat_id, description, status, created_at, due_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, description, "open", created_at, due_value),
        )
        await conn.commit()
        return cursor.lastrowid

    async def list_tasks(self, chat_id: str, status: str | None = None) -> list[TaskRecord]:
        conn = self._require_conn()
        if status:
            cursor = await conn.execute(
                """
                SELECT id, chat_id, description, status, created_at, due_at
                FROM tasks WHERE chat_id = ? AND status = ?
                ORDER BY created_at DESC
                """,
                (chat_id, status),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT id, chat_id, description, status, created_at, due_at
                FROM tasks WHERE chat_id = ?
                ORDER BY created_at DESC
                """,
                (chat_id,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            TaskRecord(
                id=row[0],
                chat_id=row[1],
                description=row[2],
                status=row[3],
                created_at=_parse_ts(row[4]),
                due_at=_parse_ts(row[5]) if row[5] else None,
            )
            for row in rows
        ]

    async def complete_task(self, chat_id: str, task_id: int) -> bool:
        conn = self._require_conn()
        cursor = await conn.execute(
            "UPDATE tasks SET status = ? WHERE chat_id = ? AND id = ?",
            ("done", chat_id, task_id),
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def add_reminder(
        self,
        chat_id: str,
        message: str,
        trigger_time: datetime,
        repeat_interval_seconds: int | None,
    ) -> int:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            INSERT INTO reminders (chat_id, message, trigger_time, repeat_interval_seconds)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                message,
                trigger_time.isoformat(),
                repeat_interval_seconds,
            ),
        )
        await conn.commit()
        return cursor.lastrowid

    async def list_reminders(self, chat_id: str) -> list[ReminderRecord]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, chat_id, message, trigger_time, repeat_interval_seconds
            FROM reminders WHERE chat_id = ? ORDER BY trigger_time ASC
            """,
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ReminderRecord(
                id=row[0],
                chat_id=row[1],
                message=row[2],
                trigger_time=_parse_ts(row[3]),
                repeat_interval_seconds=row[4],
            )
            for row in rows
        ]

    async def delete_reminder(self, chat_id: str, reminder_id: int) -> bool:
        conn = self._require_conn()
        cursor = await conn.execute(
            "DELETE FROM reminders WHERE chat_id = ? AND id = ?",
            (chat_id, reminder_id),
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def delete_reminder_by_id(self, reminder_id: int) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await conn.commit()

    async def update_reminder_time(self, reminder_id: int, trigger_time: datetime) -> None:
        conn = self._require_conn()
        await conn.execute(
            "UPDATE reminders SET trigger_time = ? WHERE id = ?",
            (trigger_time.isoformat(), reminder_id),
        )
        await conn.commit()

    async def get_reminder_by_id(self, reminder_id: int) -> ReminderRecord | None:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, chat_id, message, trigger_time, repeat_interval_seconds
            FROM reminders WHERE id = ?
            """,
            (reminder_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return ReminderRecord(
            id=row[0],
            chat_id=row[1],
            message=row[2],
            trigger_time=_parse_ts(row[3]),
            repeat_interval_seconds=row[4],
        )

    async def list_pending_reminders(self) -> list[ReminderRecord]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, chat_id, message, trigger_time, repeat_interval_seconds
            FROM reminders ORDER BY trigger_time ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ReminderRecord(
                id=row[0],
                chat_id=row[1],
                message=row[2],
                trigger_time=_parse_ts(row[3]),
                repeat_interval_seconds=row[4],
            )
            for row in rows
        ]

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

    def _require_conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Storage not connected")
        return self._conn


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)
