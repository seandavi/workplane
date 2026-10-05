"""``work``: the command-line front end. Talks to the HTTP API, never to the database."""

from __future__ import annotations

import json
import sys
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


if __name__ == "__main__":
    app()
