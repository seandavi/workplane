from __future__ import annotations

import contextlib
import dataclasses
import re
from collections.abc import AsyncIterator

import httpx
import pytest

from workplane import db, runs, work
from workplane.api import create_app
from workplane.config import Config
from workplane.sync import ingest_item

from .conftest import NOW


@contextlib.asynccontextmanager
async def serve(cfg: Config, gh_item) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, lifespan included, over the temporary database in ``cfg``, holding one of
    each kind of row the pages show."""
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        async with db.Database(cfg.database_path).connection() as conn:
            await ingest_item(conn, cfg, gh_item(node_id="I_1", number=1, labels=["bug"]), NOW)
            await ingest_item(conn, cfg, gh_item(node_id="I_2", number=2, author="alice"), NOW)
            await work.create_manual(
                conn, cfg, title="Reply to the vendor", actor="alice", due_on=cfg.today(), commit=True
            )
            live = await runs.create_run(
                conn, cfg, work_item_id=1, harness="omp", host="laptop", actor="omp@laptop",
                worktree="/tmp/wt-1", branch="wp/1-x", prompt="fix it",
            )
            await runs.record_events(
                conn,
                live["id"],
                [
                    {"kind": "tool_start", "summary": "Reading the failing test", "payload": {"tool": "read"}},
                    {
                        "kind": "turn_end",
                        "payload": {"model": "m", "usage": {"input": 100, "output": 20, "cost": {"total": 0.01}}},
                    },
                ],
                pid=1,
            )
            done = await runs.create_run(
                conn, cfg, work_item_id=2, harness="omp", host="laptop", actor="omp@laptop",
                worktree="/tmp/wt-2", branch="wp/2-x", prompt="fix it",
            )
            await runs.finish_run(
                conn, cfg, done["id"], state="finished", exit_code=0,
                final_message="Opened a PR.", pr_url="https://github.com/acme/web-app/pull/9",
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture
async def site(cfg: Config, gh_item) -> AsyncIterator[httpx.AsyncClient]:
    async with serve(cfg, gh_item) as client:
        yield client


async def test_every_page_renders_from_stored_rows(site):
    items = (await site.get("/api/work?status=inbox,backlog,ready,working,review,blocked&limit=50")).json()
    run_ids = [r["id"] for r in (await site.get("/api/runs?since_hours=24")).json()]
    assert len(items) == 3 and len(run_ids) == 2

    paths = ["/", "/items", "/items?status=committed", "/items?status=inbox&q=broke", "/fragments/agents"]
    paths += [f"/items/{i['id']}" for i in items]
    paths += [f"/runs/{r}" for r in run_ids] + [f"/fragments/run/{r}" for r in run_ids]
    for path in paths:
        assert (await site.get(path)).status_code == 200, path

    home = (await site.get("/")).text
    for expected in ("Something broke", "Reply to the vendor", "Reading the failing test"):
        assert expected in home


async def test_pages_reference_assets_that_load(site):
    html = (await site.get("/")).text
    urls = re.findall(r'(?:href|src)="(?:https?://[^/"]+)?(/static/[^"]+)"', html)
    assert urls
    for url in urls:
        assert (await site.get(url)).status_code == 200, url


async def test_cross_site_form_posts_are_refused(site):
    evil = await site.post(
        "/capture", data={"title": "from another site"}, headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    dashboard = await site.post(
        "/capture", data={"title": "from the dashboard"}, headers={"Origin": "http://testserver"}
    )
    assert dashboard.status_code == 303
    cli = await site.post("/capture", data={"title": "from curl"})  # no Origin: not a browser form post
    assert cli.status_code == 303

    titles = [i["title"] for i in (await site.get("/api/work?status=inbox&limit=50")).json()]
    assert "from the dashboard" in titles and "from curl" in titles
    assert "from another site" not in titles


async def test_a_proxys_origin_is_accepted_only_when_listed(cfg, gh_item):
    proxied = dataclasses.replace(cfg, allowed_origins=("https://wp.example.ts.net",))
    async with serve(proxied, gh_item) as client:
        listed = await client.post("/capture", data={"title": "x"}, headers={"Origin": "https://wp.example.ts.net"})
        other = await client.post("/capture", data={"title": "y"}, headers={"Origin": "https://evil.example"})
    assert (listed.status_code, other.status_code) == (303, 403)


async def test_form_redirects_cannot_leave_this_server(site):
    item = (await site.post("/api/work", json={"title": "note target"})).json()
    url = f"/items/{item['id']}/act"
    for back in ("https://evil.example/x", "//evil.example/x", "/\\evil.example"):
        resp = await site.post(url, data={"type": "note", "text": "hi", "back": back})
        assert resp.status_code == 303
        assert resp.headers["location"].startswith(f"/items/{item['id']}?"), back

    resp = await site.post(url, data={"type": "note", "text": "hi", "back": "/items?status=inbox"})
    assert resp.headers["location"].startswith("/items?status=inbox&")
