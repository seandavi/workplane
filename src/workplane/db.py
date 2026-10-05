"""Connection pool and a minimal forward-only SQL migration runner."""

from __future__ import annotations

from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

#: Arbitrary constants for pg advisory locks.
MIGRATION_LOCK = 0x5750_0001
SYNC_LOCK = 0x5750_0002
WIP_LOCK = 0x5750_0003


def open_pool(database_url: str) -> AsyncConnectionPool:
    """Autocommit pool: each ``conn.transaction()`` block is a real transaction."""
    return AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("workplane") / "migrations"
    files = sorted(p for p in root.iterdir() if p.name.endswith(".sql"))
    return [(p.name, p.read_text()) for p in files]


async def migrate(database_url: str) -> list[str]:
    """Apply pending migrations. Safe to run from several processes at once."""
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {row[0] for row in await cur.fetchall()}
            for name, sql in _migration_files():
                if name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
                applied.append(name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
    return applied
