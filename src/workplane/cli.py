"""``work``: the command-line front end. Talks to the HTTP API, never to the database."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

app = typer.Typer(help="One work queue over GitHub issues and manual tasks.", no_args_is_help=True)

Ref = Annotated[str, typer.Argument(help="Work id, repo#N or owner/repo#N")]


class Api:
    def __init__(self, base: str, actor: str | None, as_json: bool) -> None:
        self.http = httpx.Client(base_url=base, timeout=httpx.Timeout(600.0))
        self.actor = actor
        self.as_json = as_json

    def call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = self.http.request(method, path, **kw)
        except httpx.ConnectError:
            typer.secho(f"cannot reach {self.http.base_url}; is the server up?", fg="red", err=True)
            raise typer.Exit(2) from None
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error") or resp.json().get("detail")
            except ValueError:
                detail = resp.text
            typer.secho(f"error ({resp.status_code}): {detail}", fg="red", err=True)
            raise typer.Exit(1)
        return resp.json()

    def resolve(self, ref: str) -> int:
        if ref.isdigit():
            return int(ref)
        return self.call("GET", "/api/resolve", params={"ref": ref})["id"]

    def event(self, ref: str, type_: str, **payload: Any) -> None:
        work_id = self.resolve(ref)
        body = {"type": type_, "payload": {k: v for k, v in payload.items() if v is not None}}
        if self.actor:
            body["actor"] = self.actor
        out = self.call("POST", f"/api/work/{work_id}/events", json=body)
        if self.as_json:
            _dump(out)
            return
        ev, item = out["event"], out["item"]
        change = f"{ev['from_status']} → {ev['to_status']}" if ev["from_status"] != ev["to_status"] else "recorded"
        typer.echo(f"{_ref(item)}  {type_}: {change}")


def _api(ctx: typer.Context) -> Api:
    return ctx.obj


def _dump(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _ref(i: dict) -> str:
    return f"{i['repo'].split('/')[1]}#{i['number']}" if i.get("repo") else f"#{i['id']}"


def _line(i: dict, extra: str = "") -> str:
    tags = []
    if i.get("area"):
        tags.append(i["area"])
    if i.get("priority") is not None:
        tags.append(f"P{i['priority']}")
    if i.get("due_on"):
        tags.append(f"due {i['due_on']}")
    if i.get("waiting_on"):
        tags.append(f"waiting on {i['waiting_on']}")
    if i.get("claimed_by"):
        tags.append(f"@{i['claimed_by']}")
    tail = f"  [{', '.join(tags)}]" if tags else ""
    return f"{i['id']:>5}  {i['status']:<9} {_ref(i):<28} {extra}{i['title']}{tail}"


def _print_items(api: Api, rows: list[dict], *, why: bool = False) -> None:
    if api.as_json:
        _dump(rows)
        return
    if not rows:
        typer.echo("(none)")
    for i in rows:
        typer.echo(_line(i, f"{i['why']}: " if why else ""))


@app.callback()
def main(
    ctx: typer.Context,
    api_url: Annotated[str, typer.Option("--api", envvar="WORK_API_URL")] = "http://localhost:8642",
    actor: Annotated[str | None, typer.Option("--as", envvar="WORK_ACTOR", help="Who is acting (default: first `me`)")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    ctx.obj = Api(api_url, actor, as_json)


@app.command()
def summary(ctx: typer.Context) -> None:
    """Counts: needs-you, inbox, committed vs. WIP limit, by area."""
    api = _api(ctx)
    s = api.call("GET", "/api/summary")
    if api.as_json:
        _dump(s)
        return
    limit = f"/{s['wip_limit']}" if s["wip_limit"] else ""
    typer.echo(f"needs you  {s['needs_me']}")
    typer.echo(f"inbox      {s['by_status']['inbox']}")
    typer.echo(f"committed  {s['committed']}{limit}  ({s['stale_committed']} untouched {s['stale_days']}d+)")
    typer.echo(f"backlog    {s['by_status']['backlog']}   (+{s['open_noise']} bot/alert items hidden)")
    typer.echo("by area    " + ", ".join(f"{k} {v}" for k, v in s["by_area"].items()))
    typer.echo(f"last sync  {s['last_sync'] or 'never'}")


@app.command("ls")
def list_(
    ctx: typer.Context,
    status: Annotated[str, typer.Option(help="committed | open | inbox,backlog,…")] = "committed",
    area: str | None = None,
    repo: str | None = None,
    q: Annotated[str | None, typer.Option("-q", help="Search titles")] = None,
    noise: Annotated[bool, typer.Option(help="Include bot/alert items")] = False,
    limit: int = 200,
) -> None:
    """List work items (default: what you've committed to)."""
    api = _api(ctx)
    params = {"status": status, "area": area, "repo": repo, "q": q, "noise": noise, "limit": limit}
    _print_items(api, api.call("GET", "/api/work", params={k: v for k, v in params.items() if v is not None}))


@app.command()
def inbox(ctx: typer.Context, limit: int = 100) -> None:
    """New items from other people, and things you captured."""
    api = _api(ctx)
    _print_items(api, api.call("GET", "/api/work", params={"status": "inbox", "limit": limit}))


@app.command()
def needs(ctx: typer.Context) -> None:
    """Items waiting on you, with the reason."""
    api = _api(ctx)
    _print_items(api, api.call("GET", "/api/views/needs-me"), why=True)


@app.command()
def show(ctx: typer.Context, ref: Ref) -> None:
    """One item with its history and the commands valid now."""
    api = _api(ctx)
    item = api.call("GET", f"/api/work/{api.resolve(ref)}")
    if api.as_json:
        _dump(item)
        return
    typer.echo(_line(item))
    if item.get("url"):
        typer.echo(f"       {item['url']}")
    if item.get("next_action"):
        typer.echo(f"       next: {item['next_action']}")
    typer.echo(f"       can: {', '.join(item['commands'])}")
    for e in item["events"]:
        change = f"{e['from_status'] or ''} → {e['to_status']}" if e["from_status"] != e["to_status"] else ""
        extra = " ".join(f"{k}={v}" for k, v in e["payload"].items() if k != "url" and v not in (None, ""))
        typer.echo(f"       {e['occurred_at'][:16]}  {e['actor']:<10} {e['event_type']:<16} {change} {extra}".rstrip())


@app.command()
def add(
    ctx: typer.Context,
    title: str,
    area: str | None = None,
    due: Annotated[str | None, typer.Option(help="YYYY-MM-DD")] = None,
    next_action: Annotated[str | None, typer.Option("--next")] = None,
    priority: Annotated[int | None, typer.Option(min=0, max=3)] = None,
    commit: Annotated[bool, typer.Option(help="Commit to it now instead of leaving it in the inbox")] = False,
) -> None:
    """Capture a task that isn't a GitHub issue."""
    api = _api(ctx)
    body = {"title": title, "area": area, "due_on": due, "next_action": next_action, "priority": priority, "commit": commit}
    if api.actor:
        body["actor"] = api.actor
    item = api.call("POST", "/api/work", json=body)
    _dump(item) if api.as_json else typer.echo(_line(item))


def _simple(name: str, doc: str) -> None:
    def cmd(ctx: typer.Context, ref: Ref, reason: Annotated[str | None, typer.Option()] = None) -> None:
        _api(ctx).event(ref, name, reason=reason)

    cmd.__doc__ = doc
    app.command(name)(cmd)


for _name, _doc in [
    ("triage", "Inbox → backlog: seen it, not committing."),
    ("submit", "Working → review."),
    ("unblock", "Blocked → ready."),
    ("defer", "Un-commit: back to the backlog."),
    ("cancel", "Won't do."),
    ("reopen", "Done/cancelled → backlog."),
]:
    _simple(_name, _doc)


@app.command("commit")
def commit_(ctx: typer.Context, ref: Ref, force: Annotated[bool, typer.Option(help="Ignore the WIP limit")] = False) -> None:
    """Commit to an item (inbox/backlog → ready). Enforces the WIP limit."""
    _api(ctx).event(ref, "commit", force=force or None)


@app.command()
def start(ctx: typer.Context, ref: Ref, force: Annotated[bool, typer.Option(help="Take over someone else's claim")] = False) -> None:
    """Start or resume work; claims the item."""
    _api(ctx).event(ref, "start", force=force or None)


@app.command()
def block(
    ctx: typer.Context,
    ref: Ref,
    on: Annotated[str, typer.Option("--on", help="Who it's waiting on")],
    reason: str | None = None,
) -> None:
    """Mark an item as waiting on someone."""
    _api(ctx).event(ref, "block", waiting_on=on, reason=reason)


@app.command()
def done(ctx: typer.Context, ref: Ref) -> None:
    """Mark complete."""
    _api(ctx).event(ref, "complete")


@app.command("set")
def set_(
    ctx: typer.Context,
    ref: Ref,
    priority: Annotated[str | None, typer.Option(help="0-3, or '' to clear")] = None,
    due: Annotated[str | None, typer.Option(help="YYYY-MM-DD, or '' to clear")] = None,
    next_action: Annotated[str | None, typer.Option("--next")] = None,
    area: str | None = None,
    title: str | None = None,
) -> None:
    """Change fields (priority, due date, next action, area; title for manual items)."""
    payload = {"priority": priority, "due_on": due, "next_action": next_action, "area": area, "title": title}
    payload = {k: v for k, v in payload.items() if v is not None}
    if not payload:
        typer.secho("nothing to set", fg="yellow", err=True)
        raise typer.Exit(1)
    _api(ctx).event(ref, "update", **payload)


@app.command()
def note(ctx: typer.Context, ref: Ref, text: str) -> None:
    """Append a note to the item's history."""
    _api(ctx).event(ref, "note", text=text)


@app.command()
def sync(ctx: typer.Context, full: Annotated[bool, typer.Option(help="Re-fetch every open item")] = False) -> None:
    """Pull changes from GitHub now."""
    api = _api(ctx)
    report = api.call("POST", "/api/sync", params={"full": full})
    if api.as_json:
        _dump(report)
        return
    typer.echo(
        f"{report['mode']}: {report['repos']} repos, {report['seen']} items seen, "
        f"{report['new']} new, {report['transitions']} state changes"
    )
    for err in report["errors"][:10]:
        typer.secho(f"  {err}", fg="yellow")


# --- agent runs ------------------------------------------------------------


def _run_line(r: dict) -> str:
    state = r["live_state"] + (f" ({r['outcome']})" if r.get("outcome") else "")
    mins, secs = divmod(r.get("elapsed_s") or 0, 60)
    tail = r.get("pr_url") or r.get("last_activity") or ""
    return (
        f"{r['id']:>5}  {state:<20} {_ref(r):<28} {mins:>3}m{secs:02d}s  ${float(r['cost_usd']):6.2f}  "
        f"{r['tool_calls']:>3} tools  {tail}"
    )


def _start_run(
    api: Api,
    ref: str,
    *,
    resume_message: str | None,
    model: str | None,
    thinking: str | None,
    max_time: str | None,
    force: bool,
    foreground: bool,
) -> None:
    from . import runner  # the runner pulls in asyncio/subprocess machinery the other commands don't need

    base = str(api.http.base_url).rstrip("/")
    work_id = api.resolve(ref)

    async def prepare() -> dict:
        client = runner.Api(base)
        try:
            return await runner.prepare(client, work_id, resume_message=resume_message, model=model, force=force)
        finally:
            await client.aclose()

    try:
        run = asyncio.run(prepare())
    except runner.RunnerError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    if api.as_json:
        _dump(run)
    else:
        verb = "resumed" if resume_message is not None else "started"
        typer.echo(f"run {run['id']} {verb} on {_ref(run)}  ({run['actor']})")
        typer.echo(f"  worktree  {run['worktree']}  [{run['branch']}]")
        typer.echo(f"  watch     work tail {_ref(run)} -f   or   {base}/runs/{run['id']}")
    if foreground:
        code = asyncio.run(runner.execute(base, run["id"], thinking=thinking, max_time=max_time))
        final = api.call("GET", f"/api/runs/{run['id']}")
        typer.echo(_run_line(final))
        raise typer.Exit(1 if code else 0)
    runner.spawn(base, run["id"], state_dir=Path(run["log_path"]).parent.parent, thinking=thinking, max_time=max_time)


@app.command("run")
def run_(
    ctx: typer.Context,
    ref: Ref,
    model: Annotated[str | None, typer.Option(help="omp model (fuzzy match), default from config")] = None,
    thinking: Annotated[str | None, typer.Option(help="omp thinking level")] = None,
    max_time: Annotated[str | None, typer.Option(help="Stop the agent after this long, e.g. 45m")] = None,
    force: Annotated[bool, typer.Option(help="Ignore WIP/concurrency limits and other claims")] = False,
    foreground: Annotated[bool, typer.Option(help="Run in this terminal instead of detaching")] = False,
) -> None:
    """Start an omp agent on a GitHub issue in its own worktree. It ends with a PR or a question."""
    _start_run(_api(ctx), ref, resume_message=None, model=model, thinking=thinking,
               max_time=max_time, force=force, foreground=foreground)


@app.command()
def resume(
    ctx: typer.Context,
    ref: Ref,
    message: Annotated[str, typer.Argument(help="Your reply: an answer, review feedback, or a nudge")],
    model: Annotated[str | None, typer.Option(help="omp model (fuzzy match)")] = None,
    thinking: str | None = None,
    max_time: str | None = None,
    force: bool = False,
    foreground: bool = False,
) -> None:
    """Give the agent one more unattended turn in the same omp session and worktree."""
    _start_run(_api(ctx), ref, resume_message=message, model=model, thinking=thinking,
               max_time=max_time, force=force, foreground=foreground)


def _target_run(api: Api, ref: str | None, run_id: int | None, *, active: bool = False) -> dict:
    if run_id is not None:
        return api.call("GET", f"/api/runs/{run_id}")
    if ref is None:
        typer.secho("give a work reference or --run", fg="red", err=True)
        raise typer.Exit(1)
    params: dict[str, Any] = {"work_item_id": api.resolve(ref), "limit": 1}
    if active:
        params["active"] = True
    found = api.call("GET", "/api/runs", params=params)
    if not found:
        typer.secho(f"no {'active ' if active else ''}run for {ref}", fg="red", err=True)
        raise typer.Exit(1)
    return found[0]


@app.command()
def stop(
    ctx: typer.Context,
    ref: Annotated[str | None, typer.Argument(help="Work reference")] = None,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id")] = None,
) -> None:
    """Stop an active run. The runner acts on its next heartbeat (≤15s); lost runs close at once."""
    api = _api(ctx)
    run = _target_run(api, ref, run_id, active=run_id is None)
    out = api.call("POST", f"/api/runs/{run['id']}/stop")
    _dump(out) if api.as_json else typer.echo(_run_line(out))


@app.command()
def runs(ctx: typer.Context, hours: Annotated[int, typer.Option(help="Show runs started in the last N hours")] = 24) -> None:
    """Active runs, then recent ones."""
    api = _api(ctx)
    rows = api.call("GET", "/api/runs", params={"since_hours": hours, "limit": 100})
    active = api.call("GET", "/api/runs", params={"active": True})
    seen = {r["id"] for r in active}
    rows = active + [r for r in rows if r["id"] not in seen]
    if api.as_json:
        _dump(rows)
        return
    for r in rows:
        typer.echo(_run_line(r))
    if not rows:
        typer.echo("(no runs)")


def _event_line(e: dict) -> str:
    t = e["occurred_at"][11:19]
    p = e.get("payload") or {}
    if e["kind"] == "tool_start":
        return f"{t}  {p.get('tool', ''):<10} {e.get('summary') or ''}"
    if e["kind"] == "tool_end":
        return f"{t}  {p.get('tool', ''):<10} FAILED {p.get('error', '')[:200]}" if p.get("is_error") else ""
    if e["kind"] == "turn_end":
        cost = ((p.get("usage") or {}).get("cost") or {}).get("total", 0)
        text = f"  {e['summary']}" if e.get("summary") else ""
        return f"{t}  {'turn':<10} ${cost:.3f}{text}"
    if e["kind"] == "session":
        return f"{t}  {'session':<10} {e.get('summary')}"
    return ""


@app.command()
def tail(
    ctx: typer.Context,
    ref: Annotated[str | None, typer.Argument(help="Work reference (its latest run)")] = None,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id")] = None,
    follow: Annotated[bool, typer.Option("-f", "--follow", help="Keep printing until the run ends")] = False,
) -> None:
    """Print a run's activity: tool calls with their intent, failures, turns and cost."""
    api = _api(ctx)
    run = _target_run(api, ref, run_id)
    after = 0
    while True:
        for e in api.call("GET", f"/api/runs/{run['id']}/events", params={"after": after}):
            after = e["id"]
            line = _event_line(e)
            if line:
                typer.echo(line)
        run = api.call("GET", f"/api/runs/{run['id']}")
        if not follow or run["live_state"] not in ("starting", "running"):
            break
        time.sleep(2)
    typer.echo(_run_line(run))
    if run.get("final_message") or run.get("error"):
        typer.echo("\n" + (run.get("error") or run["final_message"]))


if __name__ == "__main__":
    app()
