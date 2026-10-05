"""Tests run against a real Postgres: ``docker compose up -d db`` first.

A throwaway ``workplane_test`` database is created on the compose server
(override with ``TEST_DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
from collections.abc import AsyncIterator, Callable

import psycopg
import pytest
from psycopg.rows import dict_row

from workplane import db
from workplane.config import Config
from workplane.github import GhItem

ADMIN_URL = os.environ.get("TEST_ADMIN_URL", "postgresql://workplane:workplane@localhost:5433/workplane")
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://workplane:workplane@localhost:5433/workplane_test"
)


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc}); run `docker compose up -d db`")
    with admin:
        admin.execute("DROP DATABASE IF EXISTS workplane_test WITH (FORCE)")
        admin.execute("CREATE DATABASE workplane_test")
    asyncio.run(db.migrate(TEST_URL))
    return TEST_URL


@pytest.fixture
async def conn(database_url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as c:
        await c.execute(
            "TRUNCATE run_events, runs, work_events, work_items, github_items, repositories, sync_state"
            " RESTART IDENTITY"
        )
        yield c


@pytest.fixture
def cfg(database_url: str) -> Config:
    return Config(
        database_url=database_url,
        me=("seandavi",),
        wip_limit=3,
        inbox_window_days=14,
        owners=("seandavi",),
        noise_title_patterns=(re.compile(r"is failing$"),),
        areas={"bioc": ("seandavi/bioc-*",)},
    )


NOW = dt.datetime(2026, 10, 5, 12, tzinfo=dt.UTC)


@pytest.fixture
def gh_item() -> Callable[..., GhItem]:
    def make(**kw: object) -> GhItem:
        base = dict(
            node_id="I_1",
            repo="seandavi/bioc-edge",
            repo_private=False,
            repo_fork=False,
            repo_archived=False,
            number=1,
            kind="issue",
            title="Something broke",
            state="open",
            state_reason=None,
            author="bob",
            labels=[],
            assignees=[],
            review_requests=[],
            comments_count=0,
            is_draft=False,
            url="https://github.com/seandavi/bioc-edge/issues/1",
            created_at=NOW - dt.timedelta(days=1),
            updated_at=NOW,
            closed_at=None,
        )
        base.update(kw)
        return GhItem(**base)  # type: ignore[arg-type]

    return make
