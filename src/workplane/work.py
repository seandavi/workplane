"""The single write path for work items, plus the read views built on it.

``apply_event`` is the only function that changes a work item's status or
fields. It locks the row, asks :func:`workplane.domain.decide` for the next
status, appends the event, and updates the projection in one transaction.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from . import db
from .config import Config
from .domain import (
    COMMANDS,
    COMMITTED,
    CREATION,
    TERMINAL,
    Status,
    UnknownEvent,
    decide,
)

Row = dict[str, Any]

#: Fields an ``update`` event may set, with a parser for each.
_UPDATABLE = {
    "priority": lambda v: None if v in (None, "") else int(v),
    "due_on": lambda v: None if v in (None, "") else dt.date.fromisoformat(str(v)),
    "next_action": lambda v: v or None,
    "area": lambda v: v or None,
    "title": lambda v: str(v).strip(),
}


class NotFound(Exception):
    pass


class ClaimConflict(Exception):
    pass


class WipLimitExceeded(Exception):
    pass


class InvalidPayload(Exception):
    pass


async def apply_event(
    conn: db.Connection,
    cfg: Config,
    work_id: int,
    event_type: str,
    *,
    actor: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> Row:
    """Validate and record one event; return the stored event row.

    With ``dedupe_key``, replaying the same event is a no-op that returns the
    original row. Raises :class:`~workplane.domain.TransitionError`,
    :class:`ClaimConflict`, :class:`WipLimitExceeded`, :class:`InvalidPayload`
    or :class:`NotFound`.
    """
    payload = dict(payload or {})
    if event_type in CREATION:
        raise UnknownEvent(f"{event_type} is written only at creation")
    async with conn.transaction():
        if dedupe_key:
            cur = await conn.execute(
                "SELECT * FROM work_events WHERE dedupe_key = ?", (dedupe_key,)
            )
            if existing := await cur.fetchone():
                return existing
        # transaction() is BEGIN IMMEDIATE: one writer at a time, so two concurrent commits
        # cannot both slip under the WIP limit.
        cur = await conn.execute("SELECT * FROM work_items WHERE id = ?", (work_id,))
        item = await cur.fetchone()
        if item is None:
            raise NotFound(f"work item {work_id} not found")
        current = Status(item["status"])
        target = decide(current, event_type, payload)
        changes: dict[str, Any] = {}
        force = bool(payload.get("force"))

        if event_type == "commit" and cfg.wip_limit > 0 and not force:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM work_items WHERE status IN (SELECT value FROM json_each(?)) AND id <> ?",
                ([s.value for s in COMMITTED], work_id),
            )
            n = (await cur.fetchone())["n"]
            if n >= cfg.wip_limit:
                raise WipLimitExceeded(
                    f"{n} items already committed (limit {cfg.wip_limit}); "
                    "finish, defer or cancel one first, or force"
                )
        if event_type == "start":
            holder = item["claimed_by"]
            if holder and holder != actor and not force:
                raise ClaimConflict(f"claimed by {holder}")
            changes["claimed_by"] = actor
        if event_type == "block":
            waiting_on = (payload.get("waiting_on") or "").strip()
            if not waiting_on:
                raise InvalidPayload("block needs waiting_on")
            payload["waiting_on"] = waiting_on
            changes["waiting_on"] = waiting_on
        if event_type == "update":
            unknown = set(payload) - set(_UPDATABLE)
            if unknown:
                raise InvalidPayload(f"cannot update: {', '.join(sorted(unknown))}")
            if "title" in payload and item["source"] != "manual":
                raise InvalidPayload("title of a GitHub item comes from GitHub")
            try:
                for key, value in payload.items():
                    changes[key] = _UPDATABLE[key](value)
            except ValueError as exc:
                raise InvalidPayload(str(exc)) from exc
            if "priority" in changes and changes["priority"] not in (None, 0, 1, 2, 3):
                raise InvalidPayload("priority must be 0-3")
            if changes.get("title") == "":
                raise InvalidPayload("title cannot be empty")
        if event_type == "note" and not str(payload.get("text", "")).strip():
            raise InvalidPayload("note needs text")

        if target is not None and target != current:
            changes["status"] = target.value
            if current == Status.BLOCKED:
                changes["waiting_on"] = None
            if target in TERMINAL or target in (Status.BACKLOG, Status.READY):
                changes["claimed_by"] = None

        cur = await conn.execute(
            "INSERT INTO work_events"
            " (work_item_id, actor, event_type, from_status, to_status, payload, dedupe_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (
                work_id,
                actor,
                event_type,
                current.value,
                changes.get("status", current.value),
                _jsonable(payload),
                dedupe_key,
            ),
        )
        event = await cur.fetchone()
        await _update_projection(conn, work_id, changes)
        return event


async def _update_projection(conn: db.Connection, work_id: int, changes: dict[str, Any]) -> None:
    sets = [f"updated_at = {db.NOW}"]
    params: list[Any] = []
    for key, value in changes.items():
        sets.append(f"{key} = ?")
        params.append(value)
    if "status" in changes:
        sets.append(f"status_changed_at = {db.NOW}")
    params.append(work_id)
    await conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id = ?", params)


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if isinstance(v, dt.date) else v for k, v in payload.items()}


async def create_manual(
    conn: db.Connection,
    cfg: Config,
    *,
    title: str,
    actor: str,
    area: str | None = None,
    due_on: dt.date | None = None,
    next_action: str | None = None,
    priority: int | None = None,
    commit: bool = False,
) -> Row:
    """Capture a task that has no GitHub issue (an email, a letter, a decision)."""
    title = title.strip()
    if not title:
        raise InvalidPayload("title cannot be empty")
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO work_items (source, title, status, area, due_on, next_action, priority)"
            " VALUES ('manual', ?, 'inbox', ?, ?, ?, ?) RETURNING id",
            (title, area or None, due_on, next_action or None, priority),
        )
        work_id = (await cur.fetchone())["id"]
        await conn.execute(
            "INSERT INTO work_events (work_item_id, actor, event_type, to_status, payload)"
            " VALUES (?, ?, 'created', 'inbox', ?)",
            (work_id, actor, {"title": title}),
        )
        if commit:
            await apply_event(conn, cfg, work_id, "commit", actor=actor)
    return await get_item(conn, work_id)


# --- reads -----------------------------------------------------------------

_REF = re.compile(r"^(?:(?P<owner>[\w.-]+)/)?(?P<repo>[\w.-]+)#(?P<num>\d+)$")


async def resolve_ref(conn: db.Connection, cfg: Config, ref: str) -> int:
    """Turn ``42``, ``repo#7`` or ``owner/repo#7`` into a work-item id."""
    ref = ref.strip()
    if ref.isdigit():
        return int(ref)
    m = _REF.match(ref)
    if not m:
        raise NotFound(f"not a work reference: {ref!r}")
    if m["owner"]:
        cur = await conn.execute(
            "SELECT id FROM work_view WHERE lower(repo) = lower(?) AND number = ?",
            (f"{m['owner']}/{m['repo']}", int(m["num"])),
        )
    else:
        cur = await conn.execute(
            "SELECT id, repo FROM work_view WHERE lower(substr(repo, instr(repo, '/') + 1)) = lower(?)"
            " AND number = ?",
            (m["repo"], int(m["num"])),
        )
    rows = await cur.fetchall()
    if not rows:
        raise NotFound(f"no work item for {ref}")
    if len(rows) > 1:
        options = ", ".join(r["repo"] for r in rows)
        raise NotFound(f"{ref} is ambiguous: {options}")
    return rows[0]["id"]


async def get_item(conn: db.Connection, work_id: int) -> Row:
    cur = await conn.execute("SELECT * FROM work_view WHERE id = ?", (work_id,))
    row = await cur.fetchone()
    if row is None:
        raise NotFound(f"work item {work_id} not found")
    return row


async def get_events(conn: db.Connection, work_id: int) -> list[Row]:
    cur = await conn.execute(
        "SELECT * FROM work_events WHERE work_item_id = ? ORDER BY occurred_at, id", (work_id,)
    )
    return await cur.fetchall()


_ORDER = (
    " ORDER BY priority NULLS LAST, due_on NULLS LAST,"
    " COALESCE(github_updated_at, updated_at) DESC"
)


async def list_items(
    conn: db.Connection,
    *,
    statuses: list[str] | None = None,
    area: str | None = None,
    repo: str | None = None,
    query: str | None = None,
    include_noise: bool = False,
    limit: int = 200,
) -> list[Row]:
    where: list[str] = []
    params: list[Any] = []
    if statuses:
        where.append("status IN (SELECT value FROM json_each(?))")
        params.append(statuses)
    if area:
        where.append("area = ?")
        params.append(area)
    if repo:
        where.append("(lower(repo) = lower(?) OR lower(substr(repo, instr(repo, '/') + 1)) = lower(?))")
        params += [repo, repo]
    if query:
        where.append("(title LIKE ? OR repo LIKE ?)")
        params += [f"%{query}%", f"%{query}%"]
    if not include_noise:
        where.append("NOT is_noise")
    sql = "SELECT * FROM work_view"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += _ORDER + " LIMIT ?"
    params.append(limit)
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def needs_me(conn: db.Connection, cfg: Config, *, horizon_days: int = 2) -> list[Row]:
    """Open items waiting on me, each with a ``why``.

    * blocked and ``waiting_on`` is me
    * in review and claimed by someone else (an agent or a collaborator handed it to me)
    * an open GitHub PR requests my review
    * due within ``horizon_days`` (or overdue)
    """
    me = [m.lower() for m in cfg.me]
    cutoff = cfg.today() + dt.timedelta(days=horizon_days)
    sql = """
        SELECT *, CASE
            WHEN status = 'blocked' AND lower(waiting_on) IN (SELECT value FROM json_each(:me))
                THEN 'blocked on you'
            WHEN status = 'review'
                 AND (claimed_by IS NULL OR lower(claimed_by) NOT IN (SELECT value FROM json_each(:me)))
                THEN 'ready for your review'
            WHEN kind = 'pr' AND github_state = 'open'
                 AND EXISTS (SELECT 1 FROM json_each(review_requests) r
                             WHERE lower(r.value) IN (SELECT value FROM json_each(:me)))
                THEN 'review requested on GitHub'
            WHEN due_on IS NOT NULL AND due_on < :today THEN 'overdue'
            ELSE 'due soon'
        END AS why
        FROM work_view
        WHERE status NOT IN (SELECT value FROM json_each(:terminal)) AND NOT is_noise AND (
            (status = 'blocked' AND lower(waiting_on) IN (SELECT value FROM json_each(:me)))
            OR (status = 'review'
                AND (claimed_by IS NULL OR lower(claimed_by) NOT IN (SELECT value FROM json_each(:me))))
            OR (kind = 'pr' AND github_state = 'open'
                AND EXISTS (SELECT 1 FROM json_each(review_requests) r
                            WHERE lower(r.value) IN (SELECT value FROM json_each(:me))))
            OR (due_on IS NOT NULL AND due_on <= :cutoff)
        )
        ORDER BY due_on NULLS LAST, priority NULLS LAST, updated_at DESC
    """
    cur = await conn.execute(
        sql,
        {"me": me, "terminal": [s.value for s in TERMINAL], "cutoff": cutoff, "today": cfg.today()},
    )
    return await cur.fetchall()


async def recently_done(conn: db.Connection, *, days: int = 7, limit: int = 30) -> list[Row]:
    cur = await conn.execute(
        "SELECT * FROM work_view WHERE status IN (SELECT value FROM json_each(?)) AND NOT is_noise"
        f" AND status_changed_at > strftime('{db.TS_FORMAT}', 'now', '-' || ? || ' days')"
        " ORDER BY status_changed_at DESC LIMIT ?",
        ([s.value for s in TERMINAL], days, limit),
    )
    return await cur.fetchall()


async def summary(conn: db.Connection, cfg: Config, *, stale_days: int = 14) -> Row:
    cur = await conn.execute(
        "SELECT status, count(*) AS n FROM work_view WHERE NOT is_noise GROUP BY status"
    )
    by_status = {r["status"]: r["n"] for r in await cur.fetchall()}
    cur = await conn.execute(
        "SELECT count(*) AS n FROM work_items WHERE status IN (SELECT value FROM json_each(?))"
        f" AND status_changed_at < strftime('{db.TS_FORMAT}', 'now', '-' || ? || ' days')",
        ([s.value for s in COMMITTED], stale_days),
    )
    stale = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT area, count(*) AS n FROM work_view WHERE status IN (SELECT value FROM json_each(?)) AND NOT is_noise"
        " GROUP BY area ORDER BY n DESC",
        ([s.value for s in COMMITTED | {Status.INBOX}],),
    )
    by_area = {r["area"] or "(none)": r["n"] for r in await cur.fetchall()}
    cur = await conn.execute("SELECT count(*) AS n FROM work_view WHERE is_noise AND status NOT IN (SELECT value FROM json_each(?))",
                             ([s.value for s in TERMINAL],))
    noise = (await cur.fetchone())["n"]
    cur = await conn.execute("SELECT value, updated_at FROM sync_state WHERE key = 'last_sync'")
    last_sync = await cur.fetchone()
    committed = sum(by_status.get(s.value, 0) for s in COMMITTED)
    return {
        "by_status": {s.value: by_status.get(s.value, 0) for s in Status},
        "committed": committed,
        "wip_limit": cfg.wip_limit,
        "stale_committed": stale,
        "stale_days": stale_days,
        "by_area": by_area,
        "open_noise": noise,
        "needs_me": len(await needs_me(conn, cfg)),
        "last_sync": last_sync["value"] if last_sync else None,
    }


def command_help() -> dict[str, str]:
    return {name: cmd.help for name, cmd in COMMANDS.items()}
