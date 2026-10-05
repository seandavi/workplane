from __future__ import annotations

import json
from pathlib import Path

import pytest

from workplane import db, runs, work
from workplane.runner import final_text, normalize
from workplane.sync import ingest_item

from .conftest import NOW

FIXTURE = Path(__file__).parent / "fixtures" / "omp_print_json.jsonl"


async def _issue(conn, cfg, gh_item, number=1, **kw) -> int:
    await ingest_item(conn, cfg, gh_item(node_id=f"I_{number}", number=number, **kw), NOW)
    return await work.resolve_ref(conn, cfg, f"web-app#{number}")


async def _run(conn, cfg, work_id, **kw):
    args = dict(
        work_item_id=work_id, harness="omp", host="laptop", actor="omp@laptop",
        worktree="/tmp/wt", branch="wp/1-x", prompt="fix it",
    )
    args.update(kw)
    return await runs.create_run(conn, cfg, **args)


async def test_run_claims_item_and_blocks_a_second_run(conn, cfg, gh_item):
    work_id = await _issue(conn, cfg, gh_item, author="alice")  # starts in backlog
    run = await _run(conn, cfg, work_id)
    item = await work.get_item(conn, work_id)
    assert (item["status"], item["claimed_by"]) == ("working", "omp@laptop")
    assert run["live_state"] == "starting"

    with pytest.raises(runs.RunConflict):
        await _run(conn, cfg, work_id)


async def test_concurrency_limit_per_host(conn, cfg, gh_item):
    ids = [await _issue(conn, cfg, gh_item, number=n) for n in (1, 2, 3)]
    await _run(conn, cfg, ids[0])
    await _run(conn, cfg, ids[1])
    with pytest.raises(runs.RunLimitExceeded):
        await _run(conn, cfg, ids[2])
    await _run(conn, cfg, ids[2], host="buildbox")  # other hosts have their own budget


async def test_only_github_issues_can_be_run(conn, cfg, gh_item):
    manual = (await work.create_manual(conn, cfg, title="write a letter", actor="alice"))["id"]
    pr = await _issue(conn, cfg, gh_item, number=9, kind="pr")
    for work_id in (manual, pr):
        with pytest.raises(work.InvalidPayload):
            await _run(conn, cfg, work_id)


async def test_events_roll_up_into_the_run(conn, cfg, gh_item):
    run = await _run(conn, cfg, await _issue(conn, cfg, gh_item))
    events = [normalize(json.loads(line)) for line in FIXTURE.read_text().splitlines()]
    events = [e for e in events if e]
    updated = await runs.record_events(conn, run["id"], events, pid=4242, harness_session_id="01a10c09")

    assert updated["state"] == "running"
    assert (updated["tool_calls"], updated["tool_errors"], updated["turns"]) == (2, 0, 3)
    assert updated["last_activity"] == [e["summary"] for e in events if e["kind"] == "tool_start"][-1]
    assert updated["harness_session_id"] == "01a10c09"
    assert updated["cost_usd"] > 0 and updated["tokens_out"] > 0


async def test_finishing_with_a_pr_hands_the_item_to_me_for_review(conn, cfg, gh_item):
    work_id = await _issue(conn, cfg, gh_item)
    run = await _run(conn, cfg, work_id)
    done = await runs.finish_run(conn, cfg, run["id"], state="finished", exit_code=0,
                                 final_message="Fixed it.", pr_url="https://github.com/x/y/pull/2")
    assert (done["state"], done["outcome"]) == ("finished", "pr")
    assert (await work.get_item(conn, work_id))["status"] == "review"
    needs = {n["id"]: n["why"] for n in await work.needs_me(conn, cfg)}
    assert needs[work_id] == "ready for your review"

    again = await runs.finish_run(conn, cfg, run["id"], state="failed")
    assert again["state"] == "finished"  # idempotent


async def test_finishing_without_a_pr_blocks_on_me_with_the_agents_question(conn, cfg, gh_item):
    work_id = await _issue(conn, cfg, gh_item)
    run = await _run(conn, cfg, work_id)
    await runs.finish_run(conn, cfg, run["id"], state="finished", exit_code=0,
                          final_message="BLOCKED: which bucket should site builds use?")
    item = await work.get_item(conn, work_id)
    assert (item["status"], item["waiting_on"]) == ("blocked", "alice")
    last = (await work.get_events(conn, work_id))[-1]
    assert "which bucket" in last["payload"]["reason"]

    # Resuming starts the same agent again from blocked.
    await _run(conn, cfg, work_id, resumed_from=run["id"], harness_session_id="01a10c09")
    assert (await work.get_item(conn, work_id))["status"] == "working"


async def test_run_ending_after_the_item_moved_does_not_fail(conn, cfg, gh_item):
    work_id = await _issue(conn, cfg, gh_item)
    run = await _run(conn, cfg, work_id)
    await work.apply_event(conn, cfg, work_id, "defer", actor="alice")
    done = await runs.finish_run(conn, cfg, run["id"], state="finished", pr_url="https://x/pull/1")
    assert done["outcome"] == "pr"
    assert (await work.get_item(conn, work_id))["status"] == "backlog"
    assert (await work.get_events(conn, work_id))[-1]["event_type"] == "note"


async def test_stop_flags_live_runs_and_closes_lost_ones(conn, cfg, gh_item):
    live = await _run(conn, cfg, await _issue(conn, cfg, gh_item, number=1))
    flagged = await runs.request_stop(conn, cfg, live["id"])
    assert (flagged["stop_requested"], flagged["state"]) == (True, "starting")

    lost_item = await _issue(conn, cfg, gh_item, number=2)
    lost = await _run(conn, cfg, lost_item)
    await conn.execute(
        f"UPDATE runs SET heartbeat_at = strftime('{db.TS_FORMAT}', 'now', '-5 minutes'),"
        f" started_at = strftime('{db.TS_FORMAT}', 'now', '-5 minutes') WHERE id = ?", (lost["id"],),
    )
    assert (await runs.get_run(conn, lost["id"]))["live_state"] == "lost"
    closed = await runs.request_stop(conn, cfg, lost["id"])
    assert (closed["state"], closed["outcome"]) == ("stopped", "stopped")
    assert (await work.get_item(conn, lost_item))["status"] == "blocked"


def test_normalize_keeps_what_the_dashboard_needs():
    events = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    normalized = [e for e in map(normalize, events) if e]
    assert [e["kind"] for e in normalized][:3] == ["session", "tool_start", "tool_end"]
    start = normalized[1]
    assert start["summary"] == "Creating hello file" and start["payload"]["tool"] == "write"
    turn = next(e for e in normalized if e["kind"] == "turn_end")
    assert turn["payload"]["model"] and turn["payload"]["usage"]["cost"]["total"] > 0
    agent_end = next(e for e in events if e["type"] == "agent_end")
    assert final_text(agent_end) == "done"
