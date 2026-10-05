"""Mirror GitHub issues/PRs into the database and turn state changes into events.

GitHub is read, never written. A work item's status changes only when the
GitHub *state* changes (open -> closed, closed -> open, open -> merged);
an item you marked done here stays done even if the issue is still open.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from . import db
from .config import Config
from .domain import Status
from .github import GhItem, GitHub
from .work import apply_event

log = logging.getLogger(__name__)

#: Search results are capped at 1000; past that, fall back to a full sync.
SEARCH_CAP = 1000
#: Re-scan this much before the last sync to cover search-index lag.
OVERLAP = dt.timedelta(minutes=10)


@dataclass
class SyncReport:
    mode: str
    repos: int = 0
    seen: int = 0
    new: int = 0
    transitions: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def initial_status(cfg: Config, item: GhItem, now: dt.datetime) -> Status:
    """Where a newly seen GitHub item starts.

    Other people's recent open issues and PRs go to the inbox; everything I
    (or my agents, posting as me) filed goes to the backlog.
    """
    if item.state == "merged":
        return Status.DONE
    if item.state == "closed":
        return Status.DONE if item.state_reason in (None, "COMPLETED") else Status.CANCELLED
    if cfg.is_noise(item.title, item.author) or cfg.is_me(item.author):
        return Status.BACKLOG
    if item.created_at >= now - dt.timedelta(days=cfg.inbox_window_days):
        return Status.INBOX
    return Status.BACKLOG


async def _upsert_repo(conn: db.Connection, cfg: Config, full_name: str, private: bool) -> int:
    cur = await conn.execute(
        "INSERT INTO repositories (full_name, is_private, area, synced_at)"
        f" VALUES (?, ?, ?, {db.NOW})"
        " ON CONFLICT (full_name) DO UPDATE SET is_private = EXCLUDED.is_private,"
        f" area = EXCLUDED.area, synced_at = {db.NOW} RETURNING id",
        (full_name, private, cfg.area_for(full_name)),
    )
    return (await cur.fetchone())["id"]


async def ingest_item(conn: db.Connection, cfg: Config, item: GhItem, now: dt.datetime) -> str:
    """Upsert one GitHub item. Returns ``"new"``, ``"transition"`` or ``"unchanged"``."""
    async with conn.transaction():
        repo_id = await _upsert_repo(conn, cfg, item.repo, item.repo_private)
        cur = await conn.execute(
            "SELECT g.id, g.state, w.id AS work_id FROM github_items g"
            " JOIN work_items w ON w.github_item_id = g.id WHERE g.node_id = ?",
            (item.node_id,),
        )
        prev = await cur.fetchone()
        cur = await conn.execute(
            """
            INSERT INTO github_items (node_id, repository_id, number, kind, title, state,
                state_reason, author, labels, assignees, review_requests, comments_count,
                is_draft, is_noise, url, created_at, updated_at, closed_at)
            VALUES (:node_id, :repo_id, :number, :kind, :title, :state,
                :state_reason, :author, :labels, :assignees, :review_requests,
                :comments_count, :is_draft, :is_noise, :url, :created_at,
                :updated_at, :closed_at)
            ON CONFLICT (node_id) DO UPDATE SET
                repository_id = EXCLUDED.repository_id, number = EXCLUDED.number,
                title = EXCLUDED.title, state = EXCLUDED.state,
                state_reason = EXCLUDED.state_reason, author = EXCLUDED.author,
                labels = EXCLUDED.labels, assignees = EXCLUDED.assignees,
                review_requests = EXCLUDED.review_requests,
                comments_count = EXCLUDED.comments_count, is_draft = EXCLUDED.is_draft,
                is_noise = EXCLUDED.is_noise, url = EXCLUDED.url,
                updated_at = EXCLUDED.updated_at, closed_at = EXCLUDED.closed_at
            RETURNING id
            """,
            {
                "node_id": item.node_id,
                "repo_id": repo_id,
                "number": item.number,
                "kind": item.kind,
                "title": item.title,
                "state": item.state,
                "state_reason": item.state_reason,
                "author": item.author,
                "labels": item.labels,
                "assignees": item.assignees,
                "review_requests": item.review_requests,
                "comments_count": item.comments_count,
                "is_draft": item.is_draft,
                "is_noise": cfg.is_noise(item.title, item.author),
                "url": item.url,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "closed_at": item.closed_at,
            },
        )
        gh_id = (await cur.fetchone())["id"]

        if prev is None:
            status = initial_status(cfg, item, now)
            cur = await conn.execute(
                "INSERT INTO work_items (source, github_item_id, title, status)"
                " VALUES ('github', ?, ?, ?) RETURNING id",
                (gh_id, item.title, status.value),
            )
            work_id = (await cur.fetchone())["id"]
            await conn.execute(
                "INSERT INTO work_events (work_item_id, actor, event_type, to_status, payload)"
                " VALUES (?, 'github', 'imported', ?, ?)",
                (
                    work_id,
                    status.value,
                    {"state": item.state, "author": item.author, "url": item.url},
                ),
            )
            return "new"

        await conn.execute(
            "UPDATE work_items SET title = ? WHERE id = ? AND title <> ?",
            (item.title, prev["work_id"], item.title),
        )
        if prev["state"] == item.state:
            return "unchanged"
        fact = {"open": "github.reopened", "closed": "github.closed", "merged": "github.merged"}[
            item.state
        ]
        stamp = (item.closed_at or item.updated_at).isoformat()
        await apply_event(
            conn,
            cfg,
            prev["work_id"],
            fact,
            actor="github",
            payload={"state_reason": item.state_reason, "url": item.url},
            dedupe_key=f"{fact}:{item.node_id}:{stamp}",
        )
        return "transition"


async def _ingest_all(
    conn: db.Connection, cfg: Config, items: list[GhItem], report: SyncReport, now: dt.datetime
) -> None:
    for item in items:
        if (item.repo_fork and not cfg.include_forks) or item.repo_archived:
            report.skipped += 1
            continue
        report.seen += 1
        outcome = await ingest_item(conn, cfg, item, now)
        report.new += outcome == "new"
        report.transitions += outcome == "transition"


async def _tracked_repos(gh: GitHub, cfg: Config) -> list:
    repos = []
    for owner in cfg.owners:
        repos.extend(await gh.owner_repos(owner, include_forks=cfg.include_forks))
    for full_name in cfg.repos:
        repos.append(await gh.repo(full_name))
    unique = {r.full_name.lower(): r for r in repos}
    return list(unique.values())


async def full_sync(conn: db.Connection, cfg: Config, gh: GitHub) -> SyncReport:
    """Fetch every open issue/PR in tracked repos, then re-check ones that vanished."""
    report = SyncReport(mode="full")
    now = dt.datetime.now(dt.UTC)
    repos = await _tracked_repos(gh, cfg)
    report.repos = len(repos)
    seen_ids: set[str] = set()
    for repo in repos:
        async with conn.transaction():
            await _upsert_repo(conn, cfg, repo.full_name, repo.is_private)
        if repo.open_count == 0:
            continue
        items = [i async for i in gh.open_items(repo.full_name)]
        seen_ids.update(i.node_id for i in items)
        await _ingest_all(conn, cfg, items, report, now)
        log.info("synced %s (%d open)", repo.full_name, len(items))

    # Items we think are open but GitHub no longer lists as open: closed, merged or deleted.
    cur = await conn.execute("SELECT node_id FROM github_items WHERE state = 'open'")
    missing = [r["node_id"] for r in await cur.fetchall() if r["node_id"] not in seen_ids]
    if missing:
        fetched = await gh.nodes(missing)
        await _ingest_all(conn, cfg, [i for i in fetched.values() if i], report, now)
        for node_id, item in fetched.items():
            if item is None:
                report.errors.append(f"{node_id}: deleted or no longer visible")
    return report


async def incremental_sync(
    conn: db.Connection, cfg: Config, gh: GitHub, since: dt.datetime
) -> SyncReport:
    """Ingest everything updated since ``since`` via issue search."""
    report = SyncReport(mode="incremental")
    now = dt.datetime.now(dt.UTC)
    stamp = (since - OVERLAP).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    scopes = [f"user:{o}" for o in cfg.owners] + [f"repo:{r}" for r in cfg.repos]
    for scope in scopes:
        total, items = await gh.search(f"{scope} updated:>={stamp}")
        if total > SEARCH_CAP:
            log.warning("%s: %d updates exceed the search cap; running a full sync", scope, total)
            return await full_sync(conn, cfg, gh)
        report.repos += len({i.repo for i in items})
        await _ingest_all(conn, cfg, items, report, now)
    return report


async def run_sync(database: db.Database, cfg: Config, *, full: bool = False) -> SyncReport:
    """Run one sync. A request made while another sync is running is skipped."""
    if not cfg.github_token:
        raise RuntimeError("GITHUB_TOKEN is not set")
    if database.sync_lock.locked():
        return SyncReport(mode="skipped", errors=["another sync is running"])
    async with database.sync_lock:
        started = dt.datetime.now(dt.UTC)
        gh = GitHub(cfg.github_token)
        try:
            async with database.connection() as conn:
                cur = await conn.execute("SELECT value FROM sync_state WHERE key = 'last_sync'")
                row = await cur.fetchone()
                if full or row is None:
                    report = await full_sync(conn, cfg, gh)
                else:
                    since = dt.datetime.fromisoformat(row["value"])
                    report = await incremental_sync(conn, cfg, gh, since)
                await conn.execute(
                    "INSERT INTO sync_state (key, value) VALUES ('last_sync', ?)"
                    f" ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = {db.NOW}",
                    (started.isoformat(),),
                )
        finally:
            await gh.aclose()
        log.info("sync %s", report.as_dict())
        return report
