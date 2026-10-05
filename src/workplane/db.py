"""SQLite access: connections with dict rows, nested transactions, change notifications, migrations.

Why a wrapper around aiosqlite:

* Every write transaction is ``BEGIN IMMEDIATE``. SQLite then allows one writer at a time and makes
  the others wait (``busy_timeout``) inside their own connection thread, never on the event loop.
  That replaces Postgres advisory locks and ``FOR UPDATE``: a check-then-write inside
  ``transaction()`` cannot interleave with another writer.
* ``transaction()`` nests as savepoints, so a caller can catch an error from an inner block and keep
  the outer transaction (``runs.finish_run`` relies on that).
* ``Connection.notify`` queues a message that is published to ``Database.hub`` only after the
  outermost transaction commits, which is what the dashboard's server-sent events need.
* Rows are dicts. Columns declared TIMESTAMP, DATE, JSON and BOOLEAN come back as timezone-aware
  datetime (UTC), date, list/dict and bool. datetime, date, dict, list and Decimal parameters are
  converted on the way in, so a list can be passed to ``json_each(?)`` for ``IN`` queries.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import decimal
import json
import sqlite3
from collections.abc import AsyncIterator, Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite

#: RETURNING and the built-in JSON functions need SQLite 3.38.
MIN_SQLITE = (3, 38, 0)
BUSY_TIMEOUT_SECONDS = 10.0

#: Every timestamp column holds UTC text in this one format, so comparing two of them as strings is
#: comparing them in time. SQL writes it with NOW; Python writes it through ts() (the datetime
#: adapter below). Never use datetime(), date() or CURRENT_TIMESTAMP: they use a different format.
TS_FORMAT = "%Y-%m-%dT%H:%M:%fZ"
NOW = f"strftime('{TS_FORMAT}', 'now')"

#: Booleans computed in a view lose their declared type, so name them here to get a bool back.
_COMPUTED_BOOLEANS = frozenset({"is_noise"})


def ts(value: dt.datetime) -> str:
    """UTC text with exactly three fractional digits, e.g. ``2026-10-05T13:30:07.250Z``."""
    if value.tzinfo is None:
        raise ValueError("naive datetime: give it a timezone before storing it")
    value = value.astimezone(dt.UTC)
    return f"{value:%Y-%m-%dT%H:%M:%S}.{value.microsecond // 1000:03d}Z"


sqlite3.register_adapter(dt.datetime, ts)
sqlite3.register_adapter(dt.date, dt.date.isoformat)
sqlite3.register_adapter(decimal.Decimal, float)
sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)
sqlite3.register_converter("TIMESTAMP", lambda raw: dt.datetime.fromisoformat(raw.decode()))
sqlite3.register_converter("DATE", lambda raw: dt.date.fromisoformat(raw.decode()))
sqlite3.register_converter("JSON", json.loads)
sqlite3.register_converter("BOOLEAN", lambda raw: raw != b"0")


def _row(cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> dict[str, Any]:
    row = {column[0]: value for column, value in zip(cursor.description, values, strict=True)}
    for name in _COMPUTED_BOOLEANS & row.keys():
        if row[name] is not None:
            row[name] = bool(row[name])
    return row


class Result:
    """Rows of one statement, fetched up front so no statement is left half-read on the connection."""

    __slots__ = ("_rows",)

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class Hub:
    """Fan-out of change messages to the dashboard's server-sent-event streams (this process only)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[str]] = set()

    def publish(self, message: str) -> None:
        for queue in self._queues:
            # A client that stopped reading misses a nudge; it refetches on the next one anyway.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)


class Connection:
    """One SQLite connection. Get it from ``Database.connection()``."""

    def __init__(self, raw: aiosqlite.Connection, hub: Hub) -> None:
        self._raw = raw
        self._hub = hub
        self._depth = 0
        self._pending: list[str] = []

    @classmethod
    async def open(cls, path: Path, hub: Hub) -> Connection:
        raw = await aiosqlite.connect(
            path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit: transactions are explicit, see transaction()
            timeout=BUSY_TIMEOUT_SECONDS,
        )
        raw.row_factory = _row
        await raw.execute("PRAGMA foreign_keys = ON")
        await raw.execute("PRAGMA synchronous = NORMAL")
        return cls(raw, hub)

    async def close(self) -> None:
        await self._raw.close()

    async def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Result:
        return Result(list(await self._raw.execute_fetchall(sql, params)))

    async def script(self, sql: str) -> None:
        """Run several statements at once. The script must carry its own BEGIN and COMMIT."""
        await self._raw.executescript(sql)

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """``BEGIN IMMEDIATE`` at the top level, a savepoint inside another ``transaction()``.

        Commits on a clean exit; when the block raises, rolls back this level only and re-raises.
        """
        top = self._depth == 0
        savepoint = f"sp{self._depth}"
        await self._raw.execute("BEGIN IMMEDIATE" if top else f"SAVEPOINT {savepoint}")
        self._depth += 1
        queued = len(self._pending)
        try:
            yield
        except BaseException:
            self._depth -= 1
            del self._pending[queued:]
            if top:
                await self._raw.execute("ROLLBACK")
            else:
                await self._raw.execute(f"ROLLBACK TO {savepoint}")
                await self._raw.execute(f"RELEASE {savepoint}")
            raise
        self._depth -= 1
        await self._raw.execute("COMMIT" if top else f"RELEASE {savepoint}")
        if top:
            messages, self._pending = self._pending, []
            for message in messages:
                self._hub.publish(message)

    def notify(self, **payload: Any) -> None:
        """Queue a change message; it goes out when the outermost transaction commits."""
        message = json.dumps(payload)
        if self._depth:
            self._pending.append(message)
        else:
            self._hub.publish(message)


class Database:
    """A SQLite file plus the in-process hub that tells the dashboard when runs change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.hub = Hub()
        #: Held for the whole of a sync so a manual sync and the background one never overlap.
        self.sync_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        conn = await Connection.open(self.path, self.hub)
        try:
            yield conn
        finally:
            await conn.close()


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("workplane") / "migrations"
    files = sorted((p for p in root.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name, p.read_text()) for p in files]


async def migrate(path: Path) -> list[str]:
    """Create the database file if needed and apply pending migrations; returns the names applied."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        needed = ".".join(map(str, MIN_SQLITE))
        raise RuntimeError(f"SQLite {needed} or newer is required; this Python has {sqlite3.sqlite_version}")
    path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    async with Database(path).connection() as conn:
        await conn.execute("PRAGMA journal_mode = WAL")  # persistent: readers stop blocking the writer
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            f" name TEXT PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT ({NOW}))"
        )
        cur = await conn.execute("SELECT name FROM schema_migrations")
        done = {row["name"] for row in await cur.fetchall()}
        for name, sql in _migration_files():
            if name in done:
                continue
            try:
                # The script carries its own transaction, so a failing migration leaves nothing behind.
                await conn.script(
                    f"BEGIN IMMEDIATE;\n{sql}\nINSERT INTO schema_migrations (name) VALUES ('{name}');\nCOMMIT;"
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    await conn.execute("ROLLBACK")
                raise
            applied.append(name)
    return applied
