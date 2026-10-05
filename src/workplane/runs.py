"""Agent runs: one row per agent invocation, tied to the work-item state machine.

A run's lifecycle drives its work item through the existing commands, so no
new work statuses exist for agents:

* creating a run   -> ``commit`` (if needed) then ``start``, claimed by the agent
* finishing with a PR      -> ``submit``  (shows up as "ready for your review")
* finishing without a PR   -> ``block`` waiting on me, with the agent's last words
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import db, work
from .config import Config
from .domain import Status, TransitionError

Row = dict[str, Any]

ACTIVE = ("starting", "running")
TERMINAL_STATES = ("finished", "failed", "stopped")
#: A running run whose heartbeat is older than this is shown as lost.
STALE_SECONDS = 60


class RunLimitExceeded(Exception):
    pass


class RunConflict(Exception):
    pass


_LIVE_STATE = f"""
    CASE WHEN r.state IN ('starting', 'running')
              AND COALESCE(r.heartbeat_at, r.started_at) < strftime('{db.TS_FORMAT}', 'now', '-{STALE_SECONDS} seconds')
         THEN 'lost' ELSE r.state END
"""

_SELECT = f"""
    SELECT r.*, {_LIVE_STATE} AS live_state,
           CAST(ROUND((julianday(COALESCE(r.ended_at, {db.NOW})) - julianday(r.started_at)) * 86400) AS INTEGER) AS elapsed_s,
           w.title, w.repo, w.number, w.status AS item_status, w.url AS item_url
    FROM runs r JOIN work_view w ON w.id = r.work_item_id
"""


async def create_run(
    conn: db.Connection,
    cfg: Config,
    *,
    work_item_id: int,
    harness: str,
    host: str,
    actor: str,
    worktree: str,
    branch: str,
    prompt: str,
    base_ref: str | None = None,
    session_dir: str | None = None,
    log_path: str | None = None,
    model: str | None = None,
    resumed_from: int | None = None,
    harness_session_id: str | None = None,
    force: bool = False,
) -> Row:
    """Claim the item for ``actor`` and record a new run, atomically."""
    async with conn.transaction():
        cur = await conn.execute(
            f"SELECT count(*) AS n FROM runs r WHERE r.host = ? AND ({_LIVE_STATE}) IN ('starting', 'running')",
            (host,),
        )
        active = (await cur.fetchone())["n"]
        if active >= cfg.runner.max_concurrent and not force:
            raise RunLimitExceeded(
                f"{active} runs already active on {host} (limit {cfg.runner.max_concurrent}); "
                "wait, stop one, or force"
            )
        cur = await conn.execute(
            f"SELECT 1 FROM runs r WHERE r.work_item_id = ? AND ({_LIVE_STATE}) IN ('starting', 'running')",
            (work_item_id,),
        )
        if await cur.fetchone():
            raise RunConflict(f"work item {work_item_id} already has an active run")

        item = await work.get_item(conn, work_item_id)
        if item["source"] != "github" or item["kind"] != "issue":
            raise work.InvalidPayload("agents can only be run on GitHub issues")
        if item["status"] in (Status.INBOX, Status.BACKLOG):
            await work.apply_event(
                conn, cfg, work_item_id, "commit", actor=cfg.default_actor, payload={"force": force} if force else {}
            )
        await work.apply_event(
            conn, cfg, work_item_id, "start", actor=actor, payload={"force": True} if force else {}
        )
        cur = await conn.execute(
            f"""
            INSERT INTO runs (work_item_id, resumed_from, harness, host, actor, worktree, branch,
                base_ref, session_dir, log_path, model, prompt, harness_session_id, heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {db.NOW})
            RETURNING id
            """,
            (
                work_item_id, resumed_from, harness, host, actor, worktree, branch, base_ref,
                session_dir, log_path, model, prompt, harness_session_id,
            ),
        )
        run_id = (await cur.fetchone())["id"]
        conn.notify(kind="run_created", run=run_id, item=work_item_id)
    return await get_run(conn, run_id)


async def record_events(
    conn: db.Connection,
    run_id: int,
    events: list[dict[str, Any]],
    *,
    pid: int | None = None,
    harness_session_id: str | None = None,
) -> Row:
    """Append normalized events, roll up counters, and refresh the heartbeat.

    Each event is ``{"kind", "summary"?, "payload"?}``. Kinds the rollup understands:
    ``tool_start``, ``tool_end`` (payload ``is_error``), ``turn_end`` (payload ``model``
    and ``usage``). Anything else is stored as-is.
    """
    async with conn.transaction():
        cur = await conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        run = await cur.fetchone()
        if run is None:
            raise work.NotFound(f"run {run_id} not found")
        params: list[Any] = []
        add = {"tool_calls": 0, "tool_errors": 0, "turns": 0, "tokens_in": 0, "tokens_out": 0,
               "tokens_cache_read": 0, "tokens_cache_write": 0}
        cost = Decimal(0)
        last_activity = None
        model = None
        for ev in events:
            kind = ev.get("kind")
            payload = ev.get("payload") or {}
            if kind == "tool_start":
                add["tool_calls"] += 1
                last_activity = ev.get("summary") or last_activity
            elif kind == "tool_end" and payload.get("is_error"):
                add["tool_errors"] += 1
            elif kind == "turn_end":
                add["turns"] += 1
                usage = payload.get("usage") or {}
                add["tokens_in"] += int(usage.get("input") or 0)
                add["tokens_out"] += int(usage.get("output") or 0)
                add["tokens_cache_read"] += int(usage.get("cacheRead") or 0)
                add["tokens_cache_write"] += int(usage.get("cacheWrite") or 0)
                cost += Decimal(str((usage.get("cost") or {}).get("total") or 0))
                model = payload.get("model") or model
            await conn.execute(
                "INSERT INTO run_events (run_id, kind, summary, payload) VALUES (?, ?, ?, ?)",
                (run_id, kind or "unknown", ev.get("summary"), payload),
            )

        assignments = [f"heartbeat_at = {db.NOW}"]
        for column, delta in add.items():
            if delta:
                assignments.append(f"{column} = {column} + ?")
                params.append(delta)
        if cost:
            assignments.append("cost_usd = cost_usd + ?")
            params.append(cost)
        if events:
            assignments.append(f"last_event_at = {db.NOW}")
        if last_activity:
            assignments.append("last_activity = ?")
            params.append(last_activity[:200])
        if model:
            assignments.append("model = ?")
            params.append(model)
        if pid is not None:
            assignments.append("pid = ?")
            params.append(pid)
        if harness_session_id:
            assignments.append("harness_session_id = ?")
            params.append(harness_session_id)
        if run["state"] == "starting":
            assignments.append("state = 'running'")
        params.append(run_id)
        await conn.execute(f"UPDATE runs SET {', '.join(assignments)} WHERE id = ?", params)
        conn.notify(kind="run_event", run=run_id, item=run["work_item_id"])
    return await get_run(conn, run_id)


def _outcome(state: str, pr_url: str | None, final_message: str | None) -> str:
    if state == "stopped":
        return "stopped"
    if pr_url:
        return "pr"
    if state == "failed":
        return "error"
    if (final_message or "").lstrip().upper().startswith("BLOCKED"):
        return "blocked"
    return "no_pr"


_REASONS = {
    "blocked": "agent is blocked",
    "no_pr": "agent stopped without opening a PR",
    "error": "agent run failed",
    "stopped": "run stopped",
}


async def finish_run(
    conn: db.Connection,
    cfg: Config,
    run_id: int,
    *,
    state: str,
    exit_code: int | None = None,
    final_message: str | None = None,
    pr_url: str | None = None,
    error: str | None = None,
) -> Row:
    """Close a run and move its work item. Idempotent: a finished run is returned unchanged."""
    if state not in TERMINAL_STATES:
        raise work.InvalidPayload(f"state must be one of {', '.join(TERMINAL_STATES)}")
    async with conn.transaction():
        cur = await conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        run = await cur.fetchone()
        if run is None:
            raise work.NotFound(f"run {run_id} not found")
        if run["state"] in TERMINAL_STATES:
            return await get_run(conn, run_id)
        outcome = _outcome(state, pr_url, final_message)
        await conn.execute(
            "UPDATE runs SET state = ?, outcome = ?, exit_code = ?, final_message = ?,"
            f" pr_url = COALESCE(?, pr_url), error = ?, ended_at = {db.NOW}, heartbeat_at = {db.NOW}"
            " WHERE id = ?",
            (state, outcome, exit_code, final_message, pr_url, error, run_id),
        )
        excerpt = (final_message or error or "").strip()[:500]
        try:
            if outcome == "pr":
                await work.apply_event(
                    conn, cfg, run["work_item_id"], "submit", actor=run["actor"],
                    payload={"run": run_id, "pr_url": pr_url},
                )
            else:
                await work.apply_event(
                    conn, cfg, run["work_item_id"], "block", actor=run["actor"],
                    payload={
                        "waiting_on": cfg.default_actor,
                        "reason": f"{_REASONS[outcome]}: {excerpt}" if excerpt else _REASONS[outcome],
                        "run": run_id,
                    },
                )
        except TransitionError as exc:
            # The item moved while the agent worked (e.g. you deferred it). Keep the record.
            await work.apply_event(
                conn, cfg, run["work_item_id"], "note", actor=run["actor"],
                payload={"text": f"run {run_id} ended ({outcome}); not moved: {exc}", "run": run_id},
            )
        conn.notify(kind="run_finished", run=run_id, item=run["work_item_id"])
    return await get_run(conn, run_id)


async def request_stop(conn: db.Connection, cfg: Config, run_id: int) -> Row:
    """Ask the runner to stop at its next heartbeat; close a lost run right away."""
    run = await get_run(conn, run_id)
    if run["live_state"] == "lost":
        return await finish_run(conn, cfg, run_id, state="stopped", error="no heartbeat; closed by stop")
    if run["live_state"] in ACTIVE:
        async with conn.transaction():
            await conn.execute("UPDATE runs SET stop_requested = 1 WHERE id = ?", (run_id,))
            conn.notify(kind="run_event", run=run_id, item=run["work_item_id"])
    return await get_run(conn, run_id)


async def get_run(conn: db.Connection, run_id: int) -> Row:
    cur = await conn.execute(_SELECT + " WHERE r.id = ?", (run_id,))
    row = await cur.fetchone()
    if row is None:
        raise work.NotFound(f"run {run_id} not found")
    return row


async def list_runs(
    conn: db.Connection,
    *,
    active: bool = False,
    work_item_id: int | None = None,
    since_hours: int | None = None,
    limit: int = 50,
) -> list[Row]:
    where, params = [], []
    if active:
        where.append(f"({_LIVE_STATE}) IN ('starting', 'running', 'lost')")
    if work_item_id is not None:
        where.append("r.work_item_id = ?")
        params.append(work_item_id)
    if since_hours is not None:
        where.append(f"r.started_at > strftime('{db.TS_FORMAT}', 'now', '-' || ? || ' hours')")
        params.append(since_hours)
    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY r.started_at DESC LIMIT ?"
    params.append(limit)
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def latest_run(conn: db.Connection, work_item_id: int) -> Row | None:
    runs = await list_runs(conn, work_item_id=work_item_id, limit=1)
    return runs[0] if runs else None


async def run_events(conn: db.Connection, run_id: int, *, after_id: int = 0, limit: int = 500) -> list[Row]:
    cur = await conn.execute(
        "SELECT * FROM run_events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after_id, limit),
    )
    return await cur.fetchall()


async def run_stats(conn: db.Connection) -> Row:
    cur = await conn.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE ({_LIVE_STATE}) IN ('starting', 'running')) AS active,
          count(*) FILTER (WHERE ({_LIVE_STATE}) = 'lost') AS lost,
          count(*) FILTER (WHERE r.started_at > strftime('{db.TS_FORMAT}', 'now', '-24 hours')) AS runs_24h,
          COALESCE(sum(r.cost_usd) FILTER (WHERE r.started_at > strftime('{db.TS_FORMAT}', 'now', '-24 hours')), 0) AS cost_24h,
          count(*) FILTER (WHERE r.outcome = 'pr') AS prs_total
        FROM runs r
        """
    )
    return await cur.fetchone()
