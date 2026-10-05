"""Tests run against a real SQLite file in a temporary directory; nothing to start first."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from workplane import db
from workplane.config import Config
from workplane.github import GhItem


@pytest.fixture
async def database(tmp_path: Path) -> db.Database:
    path = tmp_path / "workplane.db"
    await db.migrate(path)
    return db.Database(path)


@pytest.fixture
async def conn(database: db.Database) -> AsyncIterator[db.Connection]:
    async with database.connection() as c:
        yield c


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        database_path=tmp_path / "workplane.db",
        me=("alice",),
        wip_limit=3,
        inbox_window_days=14,
        owners=("alice",),
        noise_title_patterns=(re.compile(r"is failing$"),),
        areas={"web": ("acme/web-*",)},
    )


NOW = dt.datetime(2026, 10, 5, 12, tzinfo=dt.UTC)


@pytest.fixture
def gh_item() -> Callable[..., GhItem]:
    def make(**kw: object) -> GhItem:
        base = {
            "node_id": "I_1",
            "repo": "acme/web-app",
            "repo_private": False,
            "repo_fork": False,
            "repo_archived": False,
            "number": 1,
            "kind": "issue",
            "title": "Something broke",
            "state": "open",
            "state_reason": None,
            "author": "bob",
            "labels": [],
            "assignees": [],
            "review_requests": [],
            "comments_count": 0,
            "is_draft": False,
            "url": "https://github.com/acme/web-app/issues/1",
            "created_at": NOW - dt.timedelta(days=1),
            "updated_at": NOW,
            "closed_at": None,
        }
        base.update(kw)
        return GhItem(**base)  # type: ignore[arg-type]

    return make
