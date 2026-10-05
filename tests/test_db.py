from __future__ import annotations

import datetime as dt
import json

import pytest

from workplane import db, work
from workplane.sync import ingest_item

from .conftest import NOW


def test_timestamps_keep_three_fractional_digits_so_text_order_is_time_order():
    whole = dt.datetime(2026, 10, 5, 13, 30, 7, tzinfo=dt.UTC)
    # isoformat() drops the fraction when it is zero, which would sort 07Z after 07.250Z.
    assert db.ts(whole) == "2026-10-05T13:30:07.000Z"
    assert db.ts(whole) < db.ts(whole + dt.timedelta(milliseconds=250))
    assert db.ts(whole.astimezone(dt.timezone(dt.timedelta(hours=-4)))) == db.ts(whole)
    with pytest.raises(ValueError):
        db.ts(dt.datetime(2026, 10, 5, 13, 30, 7))


async def test_rows_come_back_as_python_types(conn, cfg, gh_item):
    await ingest_item(conn, cfg, gh_item(labels=["bug"], review_requests=["alice"]), NOW)
    manual = await work.create_manual(conn, cfg, title="letter", actor="alice", due_on=dt.date(2026, 10, 8))
    row = (await work.list_items(conn, include_noise=True))[-1]

    assert row["labels"] == ["bug"] and row["review_requests"] == ["alice"]
    assert row["is_draft"] is False and row["is_noise"] is False
    assert row["created_at"].utcoffset() == dt.timedelta(0)  # timezone-aware UTC, not a string
    assert manual["due_on"] == dt.date(2026, 10, 8)


async def test_notifications_go_out_only_after_the_outermost_commit(database):
    async with database.hub.subscribe() as queue, database.connection() as conn:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                conn.notify(kind="rolled_back")
                raise RuntimeError

        async with conn.transaction():
            conn.notify(kind="outer")
            async with conn.transaction():
                conn.notify(kind="inner")
            assert queue.empty()  # the outer transaction is still open
        assert [json.loads(queue.get_nowait())["kind"] for _ in range(2)] == ["outer", "inner"]

        conn.notify(kind="immediate")  # no transaction open: nothing to wait for
        assert json.loads(queue.get_nowait())["kind"] == "immediate"
        assert queue.empty()


async def test_migrate_applies_each_file_once(tmp_path):
    path = tmp_path / "fresh" / "workplane.db"
    assert await db.migrate(path)
    assert await db.migrate(path) == []
