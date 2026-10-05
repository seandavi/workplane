from __future__ import annotations

import asyncio
import datetime as dt

import psycopg
import pytest
from psycopg.rows import dict_row

from workplane import work
from workplane.domain import TransitionError
from workplane.sync import ingest_item

from .conftest import NOW


async def _manual(conn, cfg, title="task", **kw):
    return (await work.create_manual(conn, cfg, title=title, actor="seandavi", **kw))["id"]


async def _status(conn, work_id) -> str:
    return (await work.get_item(conn, work_id))["status"]


async def _event_count(conn, work_id) -> int:
    return len(await work.get_events(conn, work_id))


# --- commands and invariants -------------------------------------------------


async def test_commit_refuses_past_wip_limit_unless_forced(conn, cfg):
    ids = [await _manual(conn, cfg, f"t{i}", commit=True) for i in range(3)]
    extra = await _manual(conn, cfg, "one too many")

    with pytest.raises(work.WipLimitExceeded):
        await work.apply_event(conn, cfg, extra, "commit", actor="seandavi")
    assert await _status(conn, extra) == "inbox"

    await work.apply_event(conn, cfg, ids[0], "complete", actor="seandavi")
    await work.apply_event(conn, cfg, extra, "commit", actor="seandavi")
    assert await _status(conn, extra) == "ready"

    forced = await _manual(conn, cfg, "forced")
    await work.apply_event(conn, cfg, forced, "commit", actor="seandavi", payload={"force": True})
    assert await _status(conn, forced) == "ready"


async def test_concurrent_commits_cannot_both_take_the_last_slot(conn, cfg, database_url):
    for i in range(2):
        await _manual(conn, cfg, f"t{i}", commit=True)
    a, b = await _manual(conn, cfg, "a"), await _manual(conn, cfg, "b")

    async def commit(work_id):
        async with await psycopg.AsyncConnection.connect(
            database_url, autocommit=True, row_factory=dict_row
        ) as c:
            await work.apply_event(c, cfg, work_id, "commit", actor="seandavi")

    results = await asyncio.gather(commit(a), commit(b), return_exceptions=True)
    assert sum(r is None for r in results) == 1
    assert sum(isinstance(r, work.WipLimitExceeded) for r in results) == 1


async def test_rejected_command_changes_nothing(conn, cfg):
    work_id = await _manual(conn, cfg, commit=True)
    before = await _event_count(conn, work_id)
    with pytest.raises(TransitionError):
        await work.apply_event(conn, cfg, work_id, "submit", actor="seandavi")  # ready, not working
    assert await _status(conn, work_id) == "ready"
    assert await _event_count(conn, work_id) == before


async def test_start_respects_another_actors_claim(conn, cfg):
    work_id = await _manual(conn, cfg, commit=True)
    await work.apply_event(conn, cfg, work_id, "start", actor="agent-a")
    await work.apply_event(conn, cfg, work_id, "block", actor="agent-a", payload={"waiting_on": "seandavi"})

    with pytest.raises(work.ClaimConflict):
        await work.apply_event(conn, cfg, work_id, "start", actor="agent-b")
    await work.apply_event(conn, cfg, work_id, "start", actor="agent-a")
    assert (await work.get_item(conn, work_id))["claimed_by"] == "agent-a"

    await work.apply_event(conn, cfg, work_id, "submit", actor="agent-a")
    await work.apply_event(conn, cfg, work_id, "start", actor="agent-b", payload={"force": True})
    assert (await work.get_item(conn, work_id))["claimed_by"] == "agent-b"

    await work.apply_event(conn, cfg, work_id, "defer", actor="seandavi")
    item = await work.get_item(conn, work_id)
    assert (item["status"], item["claimed_by"]) == ("backlog", None)


async def test_blocked_on_me_needs_me_until_unblocked(conn, cfg):
    work_id = await _manual(conn, cfg, commit=True)
    with pytest.raises(work.InvalidPayload):
        await work.apply_event(conn, cfg, work_id, "block", actor="seandavi", payload={})

    await work.apply_event(conn, cfg, work_id, "block", actor="agent", payload={"waiting_on": "SeanDavi"})
    needs = await work.needs_me(conn, cfg)
    assert [(n["id"], n["why"]) for n in needs] == [(work_id, "blocked on you")]

    await work.apply_event(conn, cfg, work_id, "unblock", actor="seandavi")
    item = await work.get_item(conn, work_id)
    assert (item["status"], item["waiting_on"]) == ("ready", None)
    assert await work.needs_me(conn, cfg) == []


async def test_due_dates_surface_in_needs_me(conn, cfg):
    today = cfg.today()
    overdue = await _manual(conn, cfg, "overdue", due_on=today - dt.timedelta(days=1))
    await _manual(conn, cfg, "later", due_on=today + dt.timedelta(days=10))
    needs = await work.needs_me(conn, cfg)
    assert [(n["id"], n["why"]) for n in needs] == [(overdue, "overdue")]


# --- GitHub ingestion ----------------------------------------------------------


async def test_import_routes_by_author_age_and_noise(conn, cfg, gh_item):
    cases = {
        "I_ext": gh_item(node_id="I_ext", number=1),
        "I_mine": gh_item(node_id="I_mine", number=2, author="seandavi"),
        "I_old": gh_item(node_id="I_old", number=3, created_at=NOW - dt.timedelta(days=60)),
        "I_alert": gh_item(node_id="I_alert", number=4, title="bioc-sync.service is failing"),
        "I_bot": gh_item(node_id="I_bot", number=5, author="dependabot[bot]"),
    }
    for item in cases.values():
        assert await ingest_item(conn, cfg, item, NOW) == "new"

    rows = {r["number"]: r for r in await work.list_items(conn, include_noise=True)}
    assert {n: rows[n]["status"] for n in rows} == {
        1: "inbox", 2: "backlog", 3: "backlog", 4: "backlog", 5: "backlog"
    }
    assert rows[1]["area"] == "bioc"
    visible = {r["number"] for r in await work.list_items(conn)}
    assert visible == {1, 2, 3}


async def test_github_close_applies_once(conn, cfg, gh_item):
    await ingest_item(conn, cfg, gh_item(), NOW)
    work_id = await work.resolve_ref(conn, cfg, "seandavi/bioc-edge#1")
    closed = gh_item(state="closed", state_reason="NOT_PLANNED", closed_at=NOW)

    assert await ingest_item(conn, cfg, closed, NOW) == "transition"
    assert await ingest_item(conn, cfg, closed, NOW) == "unchanged"
    assert await _status(conn, work_id) == "cancelled"
    assert [e["event_type"] for e in await work.get_events(conn, work_id)] == ["imported", "github.closed"]

    await ingest_item(conn, cfg, gh_item(state="open", updated_at=NOW + dt.timedelta(hours=1)), NOW)
    assert await _status(conn, work_id) == "backlog"


async def test_local_decisions_survive_resync(conn, cfg, gh_item):
    await ingest_item(conn, cfg, gh_item(), NOW)
    work_id = await work.resolve_ref(conn, cfg, "bioc-edge#1")
    await work.apply_event(conn, cfg, work_id, "commit", actor="seandavi")
    await work.apply_event(conn, cfg, work_id, "complete", actor="seandavi")

    # GitHub still says open: re-syncing must not undo the local "done".
    await ingest_item(conn, cfg, gh_item(title="Something broke (renamed)"), NOW)
    item = await work.get_item(conn, work_id)
    assert (item["status"], item["title"]) == ("done", "Something broke (renamed)")

    # GitHub closing later is recorded but does not move an already-done item.
    await ingest_item(conn, cfg, gh_item(state="closed", closed_at=NOW), NOW)
    events = await work.get_events(conn, work_id)
    assert events[-1]["event_type"] == "github.closed"
    assert events[-1]["from_status"] == events[-1]["to_status"] == "done"


async def test_short_refs_are_rejected_when_ambiguous(conn, cfg, gh_item):
    await ingest_item(conn, cfg, gh_item(), NOW)
    await ingest_item(
        conn, cfg, gh_item(node_id="I_other", repo="otherorg/bioc-edge", url="u2"), NOW
    )
    with pytest.raises(work.NotFound, match="ambiguous"):
        await work.resolve_ref(conn, cfg, "bioc-edge#1")
    assert await work.resolve_ref(conn, cfg, "otherorg/bioc-edge#1") == 2
