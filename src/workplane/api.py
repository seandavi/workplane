"""HTTP surface: a JSON API (for the CLI and, later, agents) and a server-rendered dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import dataclasses
import hashlib
from collections.abc import AsyncIterator
from importlib import resources
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db, runs, work
from .config import Config, load
from .domain import COMMANDS, COMMITTED, TERMINAL, Status, TransitionError, UnknownEvent, available_commands
from .sync import run_sync

log = logging.getLogger(__name__)

_PKG = resources.files("workplane")
templates = Jinja2Templates(directory=str(_PKG / "templates"))


def _asset_version() -> str:
    """Hash of the static files. It is appended to their URLs so a browser never pairs new
    templates with a cached old stylesheet."""
    digest = hashlib.sha1(usedforsecurity=False)
    for path in sorted((_PKG / "static").iterdir(), key=lambda p: p.name):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


templates.env.globals["asset_v"] = _asset_version()

_ERRORS: dict[type[Exception], int] = {
    work.NotFound: 404,
    TransitionError: 409,
    work.ClaimConflict: 409,
    work.WipLimitExceeded: 409,
    work.InvalidPayload: 422,
    UnknownEvent: 422,
    runs.RunLimitExceeded: 409,
    runs.RunConflict: 409,
}


class EventIn(BaseModel):
    type: str
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ManualIn(BaseModel):
    title: str
    actor: str | None = None
    area: str | None = None
    due_on: dt.date | None = None
    next_action: str | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    commit: bool = False


class RunIn(BaseModel):
    work_item_id: int
    harness: str
    host: str
    actor: str
    worktree: str
    branch: str
    prompt: str
    base_ref: str | None = None
    session_dir: str | None = None
    log_path: str | None = None
    model: str | None = None
    resumed_from: int | None = None
    harness_session_id: str | None = None
    force: bool = False


class RunEventsIn(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    pid: int | None = None
    harness_session_id: str | None = None


class RunFinishIn(BaseModel):
    state: str
    exit_code: int | None = None
    final_message: str | None = None
    pr_url: str | None = None
    error: str | None = None


async def _sync_loop(database: db.Database, cfg: Config) -> None:
    while True:
        try:
            await run_sync(database, cfg)
        except Exception:
            log.exception("background sync failed")
        await asyncio.sleep(cfg.sync_interval_seconds)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        applied = await db.migrate(cfg.database_path)
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
        app.state.db = db.Database(cfg.database_path)
        task = None
        if cfg.github_token and cfg.sync_interval_seconds > 0:
            task = asyncio.create_task(_sync_loop(app.state.db, cfg))
        else:
            log.warning("background sync off (GITHUB_TOKEN unset or interval 0)")
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="workplane", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(_PKG / "static")), name="static")
    app.state.cfg = cfg
    app.add_middleware(SameOriginPosts, allowed=cfg.allowed_origins)

    for exc_type, code in _ERRORS.items():

        async def handler(request: Request, exc: Exception, code: int = code) -> JSONResponse:
            return JSONResponse({"error": str(exc), "kind": type(exc).__name__}, status_code=code)

        app.add_exception_handler(exc_type, handler)

    def database(request: Request) -> db.Database:
        return request.app.state.db

    # --- JSON API ---------------------------------------------------------

    @app.get("/api/healthz")
    async def healthz(request: Request) -> dict:
        async with database(request).connection() as conn:
            await conn.execute("SELECT 1")
        return {"ok": True}

    @app.get("/api/summary")
    async def api_summary(request: Request) -> dict:
        async with database(request).connection() as conn:
            return await work.summary(conn, cfg)

    @app.get("/api/commands")
    async def api_commands() -> dict:
        return {
            name: {"help": c.help, "from": sorted(c.sources), "to": c.target}
            for name, c in COMMANDS.items()
        }

    @app.get("/api/work")
    async def api_list(
        request: Request,
        status: str | None = None,
        area: str | None = None,
        repo: str | None = None,
        q: str | None = None,
        noise: bool = False,
        limit: int = Query(200, le=2000),
    ) -> list[dict]:
        statuses = _parse_statuses(status)
        async with database(request).connection() as conn:
            return await work.list_items(
                conn, statuses=statuses, area=area, repo=repo, query=q, include_noise=noise, limit=limit
            )

    @app.get("/api/resolve")
    async def api_resolve(request: Request, ref: str) -> dict:
        async with database(request).connection() as conn:
            return {"id": await work.resolve_ref(conn, cfg, ref)}

    @app.get("/api/work/{work_id}")
    async def api_get(request: Request, work_id: int) -> dict:
        async with database(request).connection() as conn:
            item = await work.get_item(conn, work_id)
            events = await work.get_events(conn, work_id)
        return {**item, "events": events, "commands": available_commands(Status(item["status"]))}

    @app.post("/api/work", status_code=201)
    async def api_create(request: Request, body: ManualIn) -> dict:
        async with database(request).connection() as conn:
            return await work.create_manual(
                conn,
                cfg,
                title=body.title,
                actor=body.actor or cfg.default_actor,
                area=body.area,
                due_on=body.due_on,
                next_action=body.next_action,
                priority=body.priority,
                commit=body.commit,
            )

    @app.post("/api/work/{work_id}/events", status_code=201)
    async def api_event(request: Request, work_id: int, body: EventIn) -> dict:
        async with database(request).connection() as conn:
            event = await work.apply_event(
                conn, cfg, work_id, body.type, actor=body.actor or cfg.default_actor, payload=body.payload
            )
            item = await work.get_item(conn, work_id)
        return {"event": event, "item": item}

    @app.get("/api/views/needs-me")
    async def api_needs_me(request: Request) -> list[dict]:
        async with database(request).connection() as conn:
            return await work.needs_me(conn, cfg)

    @app.get("/api/views/done")
    async def api_done(request: Request, days: int = 7) -> list[dict]:
        async with database(request).connection() as conn:
            return await work.recently_done(conn, days=days)

    @app.post("/api/sync")
    async def api_sync(request: Request, full: bool = False) -> dict:
        return (await run_sync(database(request), cfg, full=full)).as_dict()

    # --- runs -------------------------------------------------------------

    @app.get("/api/runner-config")
    async def api_runner_config() -> dict:
        return {**dataclasses.asdict(cfg.runner), "me": cfg.default_actor}

    @app.post("/api/runs", status_code=201)
    async def api_run_create(request: Request, body: RunIn) -> dict:
        async with database(request).connection() as conn:
            return await runs.create_run(conn, cfg, **body.model_dump())

    @app.get("/api/runs")
    async def api_runs(
        request: Request,
        active: bool = False,
        work_item_id: int | None = None,
        since_hours: int | None = None,
        limit: int = Query(50, le=500),
    ) -> list[dict]:
        async with database(request).connection() as conn:
            return await runs.list_runs(
                conn, active=active, work_item_id=work_item_id, since_hours=since_hours, limit=limit
            )

    @app.get("/api/runs/{run_id}")
    async def api_run(request: Request, run_id: int) -> dict:
        async with database(request).connection() as conn:
            return await runs.get_run(conn, run_id)

    @app.get("/api/runs/{run_id}/events")
    async def api_run_events(request: Request, run_id: int, after: int = 0) -> list[dict]:
        async with database(request).connection() as conn:
            return await runs.run_events(conn, run_id, after_id=after)

    @app.post("/api/runs/{run_id}/events")
    async def api_run_record(request: Request, run_id: int, body: RunEventsIn) -> dict:
        async with database(request).connection() as conn:
            return await runs.record_events(
                conn, run_id, body.events, pid=body.pid, harness_session_id=body.harness_session_id
            )

    @app.post("/api/runs/{run_id}/finish")
    async def api_run_finish(request: Request, run_id: int, body: RunFinishIn) -> dict:
        async with database(request).connection() as conn:
            return await runs.finish_run(conn, cfg, run_id, **body.model_dump())

    @app.post("/api/runs/{run_id}/stop")
    async def api_run_stop(request: Request, run_id: int) -> dict:
        async with database(request).connection() as conn:
            return await runs.request_stop(conn, cfg, run_id)

    @app.get("/api/stream")
    async def api_stream(request: Request) -> StreamingResponse:
        """Server-sent events: one message per run change (created, events, finished)."""

        async def events() -> AsyncIterator[str]:
            async with database(request).hub.subscribe() as queue:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                    else:
                        yield f"data: {message}\n\n"

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    async def _agents_ctx(conn: db.Connection) -> dict[str, Any]:
        return {
            "active_runs": await runs.list_runs(conn, active=True),
            "recent_runs": [
                r for r in await runs.list_runs(conn, since_hours=72, limit=20)
                if r["live_state"] not in ("starting", "running", "lost")
            ],
            "run_stats": await runs.run_stats(conn),
        }

    @app.get("/fragments/agents")
    async def fragment_agents(request: Request):
        async with database(request).connection() as conn:
            ctx = await _agents_ctx(conn)
        return render(request, "_agents.html", **ctx)

    async def _run_ctx(conn: db.Connection, run_id: int) -> dict[str, Any]:
        run = await runs.get_run(conn, run_id)
        events = await runs.run_events(conn, run_id)
        return {"run": run, "events": [e for e in events if e["kind"] in ("tool_start", "tool_end", "turn_end", "agent_end")]}

    @app.get("/runs/{run_id}")
    async def run_page(request: Request, run_id: int):
        async with database(request).connection() as conn:
            ctx = await _run_ctx(conn, run_id)
        return render(request, "run.html", **ctx)

    @app.get("/fragments/run/{run_id}")
    async def fragment_run(request: Request, run_id: int):
        async with database(request).connection() as conn:
            ctx = await _run_ctx(conn, run_id)
        return render(request, "_run.html", **ctx)

    @app.post("/runs/{run_id}/stop")
    async def run_stop(request: Request, run_id: int, back: str = Form("")):
        async with database(request).connection() as conn:
            await runs.request_stop(conn, cfg, run_id)
        return _redirect(_local_path(back, f"/runs/{run_id}"), msg=f"stop requested for run {run_id}")

    # --- dashboard --------------------------------------------------------

    def render(request: Request, name: str, **ctx: Any):
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("error", request.query_params.get("error"))
        return templates.TemplateResponse(
            request,
            name,
            {"cfg": cfg, "today": cfg.today(), "Status": Status, **ctx},
        )

    @app.get("/")
    async def cockpit(request: Request):
        async with database(request).connection() as conn:
            summary = await work.summary(conn, cfg)
            needs = await work.needs_me(conn, cfg)
            inbox = await work.list_items(conn, statuses=[Status.INBOX.value], limit=50)
            committed = await work.list_items(
                conn, statuses=[s.value for s in COMMITTED], include_noise=True, limit=500
            )
            done = await work.recently_done(conn)
            agents = await _agents_ctx(conn)
        columns = {
            s: [i for i in committed if i["status"] == s.value]
            for s in (Status.READY, Status.WORKING, Status.REVIEW, Status.BLOCKED)
        }
        return render(
            request,
            "cockpit.html",
            summary=summary,
            needs=needs,
            inbox=inbox,
            columns=columns,
            done=done,
            **agents,
        )

    @app.get("/items")
    async def items_page(
        request: Request,
        status: str | None = None,
        area: str | None = None,
        repo: str | None = None,
        q: str | None = None,
        noise: bool = False,
    ):
        statuses = _parse_statuses(status) or [s.value for s in Status if s not in TERMINAL]
        async with database(request).connection() as conn:
            rows = await work.list_items(
                conn, statuses=statuses, area=area, repo=repo, query=q, include_noise=noise, limit=500
            )
            cur = await conn.execute(
                "SELECT DISTINCT area FROM work_view WHERE area IS NOT NULL ORDER BY area"
            )
            areas = [r["area"] for r in await cur.fetchall()]
        return render(
            request,
            "items.html",
            rows=rows,
            areas=areas,
            filters={"status": status or "", "area": area or "", "repo": repo or "", "q": q or "", "noise": noise},
        )

    @app.get("/items/{work_id}")
    async def item_page(request: Request, work_id: int):
        async with database(request).connection() as conn:
            item = await work.get_item(conn, work_id)
            events = await work.get_events(conn, work_id)
            item_runs = await runs.list_runs(conn, work_item_id=work_id, limit=20)
        return render(
            request,
            "item.html",
            item=item,
            events=events,
            commands=[(c, COMMANDS[c].help) for c in available_commands(Status(item["status"]))],
            item_runs=item_runs,
        )

    @app.post("/items/{work_id}/act")
    async def item_act(
        request: Request,
        work_id: int,
        type: str = Form(...),
        waiting_on: str = Form(""),
        reason: str = Form(""),
        text: str = Form(""),
        force: bool = Form(False),
        priority: str = Form(""),
        due_on: str = Form(""),
        next_action: str = Form(""),
        area: str = Form(""),
        back: str = Form(""),
    ):
        payload: dict[str, Any] = {}
        if force:
            payload["force"] = True
        if type == "block":
            payload["waiting_on"] = waiting_on
            if reason:
                payload["reason"] = reason
        elif type == "note":
            payload["text"] = text
        elif type == "update":
            payload = {"priority": priority, "due_on": due_on, "next_action": next_action, "area": area}
        elif reason:
            payload["reason"] = reason
        target = _local_path(back, f"/items/{work_id}")
        try:
            async with database(request).connection() as conn:
                await work.apply_event(conn, cfg, work_id, type, actor=cfg.default_actor, payload=payload)
        except tuple(_ERRORS) as exc:
            return _redirect(target, error=str(exc), retry=f"{work_id}:{type}")
        return _redirect(target, msg=f"#{work_id}: {type}")

    @app.post("/capture")
    async def capture(
        request: Request,
        title: str = Form(...),
        area: str = Form(""),
        due_on: str = Form(""),
        commit: bool = Form(False),
    ):
        try:
            async with database(request).connection() as conn:
                item = await work.create_manual(
                    conn,
                    cfg,
                    title=title,
                    actor=cfg.default_actor,
                    area=area,
                    due_on=dt.date.fromisoformat(due_on) if due_on else None,
                    commit=commit,
                )
        except (*_ERRORS, ValueError) as exc:
            return _redirect("/", error=str(exc))
        return _redirect("/", msg=f"captured #{item['id']} ({item['status']})")

    @app.post("/sync")
    async def sync_now(request: Request, full: bool = Form(False)):
        try:
            report = await run_sync(database(request), cfg, full=full)
        except Exception as exc:  # surface any sync failure on the page
            return _redirect("/", error=f"sync failed: {exc}")
        return _redirect(
            "/",
            msg=f"sync ({report.mode}): {report.seen} seen, {report.new} new, "
            f"{report.transitions} state changes",
        )

    return app


def _parse_statuses(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    if raw == "open":
        return [s.value for s in Status if s not in TERMINAL]
    if raw == "committed":
        return [s.value for s in COMMITTED]
    statuses = [s.strip() for s in raw.split(",") if s.strip()]
    bad = [s for s in statuses if s not in Status.__members__.values()]
    if bad:
        raise work.InvalidPayload(f"unknown status: {', '.join(bad)}")
    return statuses


def _redirect(path: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    clean = {k: v for k, v in params.items() if v}
    return RedirectResponse(f"{path}{sep}{urlencode(clean)}" if clean else path, status_code=303)


def _local_path(value: str, default: str) -> str:
    """A redirect target taken from a form field. Only paths on this server pass, so a crafted
    form cannot bounce the browser to another site."""
    if value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return default


class SameOriginPosts:
    """Refuse form posts that another website made your browser send.

    Browsers label a cross-site POST with an ``Origin`` header, so a POST whose origin is neither
    this server nor one of ``allowed_origins`` is refused. Requests without ``Origin`` (the CLI,
    the runner, curl) are not browser form posts and pass.
    """

    def __init__(self, app: ASGIApp, allowed: tuple[str, ...] = ()) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            headers = dict(scope["headers"])
            origin = headers.get(b"origin", b"").decode()
            host = headers.get(b"host", b"").decode()
            if origin and urlsplit(origin).netloc != host and origin not in self.allowed:
                refusal = JSONResponse(
                    {"error": "cross-site request refused", "kind": "CrossSite"}, status_code=403
                )
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)
