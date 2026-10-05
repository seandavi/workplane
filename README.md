# workplane

One work queue over GitHub issues, PRs and manual tasks. GitHub stays the system
of record; workplane adds what GitHub doesn't have: an inbox for other people's
new items, a hard limit on what you've committed to, "waiting on whom", due dates
for non-code obligations, and an append-only event log behind every status.

Agents run locally through `work run`: one omp session per GitHub issue, in its own git
worktree, streamed live to the dashboard. The service itself never writes to GitHub; the
agents you start do (they push a branch and open a PR).

## Run it

```bash
echo "GITHUB_TOKEN=$(gh auth token)" > .env    # read-only use; .env is gitignored
docker compose up -d --build                    # Postgres 17 + app on :8642
open http://localhost:8642
```

The first sync fetches every open issue and PR in the repos listed in
`workplane.toml` (about 90 seconds for 339 repos and 905 open items). After that it runs every 10 minutes,
using issue search for anything updated since the last run. Press **Sync** or run
`work sync` to sync right away.

CLI, from this directory:

```bash
uv run work summary
uv tool install -e .     # optional: puts `work` on PATH
```

## Model

```mermaid
stateDiagram-v2
    [*] --> inbox: someone else's new issue/PR,<br/>or a captured task
    [*] --> backlog: filed by me, or older than 14 days
    inbox --> backlog: triage
    inbox --> ready: commit
    backlog --> ready: commit
    ready --> working: start
    working --> review: submit
    review --> working: start (changes)
    ready --> blocked: block
    working --> blocked: block
    review --> blocked: block
    blocked --> working: start
    blocked --> ready: unblock
    ready --> backlog: defer
    working --> backlog: defer
    review --> backlog: defer
    blocked --> backlog: defer
    working --> done: complete
    review --> done: complete
    done --> backlog: reopen
```

`complete` and `cancel` are allowed from any open status. GitHub closing, merging or reopening
an item is recorded as a fact (`github.closed`, `github.merged`, `github.reopened`) and moves the
item only if it is still open here. If you mark an item done, it stays done even while the
GitHub issue is still open.

| Concept | Rule |
|---|---|
| **Committed** | `ready`, `working`, `review`, `blocked`. `commit` is refused once `wip_limit` (default 15) is reached unless forced. Concurrent commits are serialized, so they can't push past the limit. |
| **Inbox** | Open issues and PRs from someone other than `me`, created in the last `inbox_window_days`, plus anything you capture. Triage moves an item to the backlog; commit moves it to ready. |
| **Needs you** | Blocked with `waiting_on` = you · in review and claimed by someone else · a GitHub PR requests your review · due within 2 days or overdue ("today" is in the configured `timezone`). |
| **Claim** | `start` claims the item for the actor. If someone else holds the claim, `start` fails unless forced. Unblocking, deferring, completing or cancelling releases the claim. |
| **Noise** | Authors ending in `[bot]`, plus title patterns in `[noise]` (e.g. systemd failure alerts). These items are kept but hidden from the inbox and the default views. |
| **Area** | From repo globs in `[areas]`; can be overridden per item. |

Every change goes through one function (`work.apply_event`). It locks the row, checks
the transition in `domain.decide`, appends to `work_events` and updates the
`work_items` projection, all in one transaction.

## CLI

```text
work summary                     counts, WIP vs limit, by area
work needs                       what's waiting on you, and why
work inbox | work ls [--status open|committed|inbox,…] [--area] [--repo] [-q]
work show bioc-edge#59           history + valid commands (also: 42, owner/repo#N)
work add "Reply to Bob re #48" --area bioc --due 2026-10-08 --commit
work commit|triage|start|submit|unblock|defer|done|cancel|reopen REF
work block REF --on bob --reason "needs their call on badges"
work set REF --priority 1 --due 2026-10-08 --next "draft reply"
work note REF "talked at Thursday meeting"
work sync [--full]
```

Global flags go before the subcommand: `work --as pi start bioc-edge#43`. They are `--as NAME`
(actor, default the first `me`; env `WORK_ACTOR`), `--json`, and `--api URL` (env `WORK_API_URL`).

## Agent runs (omp, local)

```bash
work run bioc-website#33          # worktree + prompt + omp, detached; prints the run id
work tail bioc-website#33 -f       # tool calls (with omp's intent), failures, turns, cost
work resume bioc-website#33 "use the build's robots.txt, not a Worker route"
work stop bioc-website#33          # runner stops omp at its next heartbeat (≤15s)
work runs                          # active + last 24h
```

`work run` runs in the foreground until the run is registered, so mistakes (WIP limit, a
claim held by someone else, git trouble) are reported in your terminal. Then a detached
runner starts `omp -p --mode json` and reports to the API.

- **Worktree:** `~/Documents/git/worktrees/workplane/<repo>/wp-<N>` on branch `wp/<N>-<slug>`,
  from `origin/<default>`. It uses your existing clone under `~/Documents/git/<repo>` if its
  origin matches; otherwise it clones into `…/_clones/`.
- **Prompt:** the issue, its last 20 comments, and the rules: smallest change, run the
  tests, push, open a PR with `Closes owner/repo#N`. Never merge, never push to the default
  branch, never close the issue. If stuck, stop with a message starting `BLOCKED:`.
- **What it does to the work item:** starting a run commits the item if needed (subject to the WIP limit), then
  `start`s it, claimed by `omp@<host>`. A run that ends with a PR does `submit`, so the item
  shows under **Needs you** as "ready for your review". A run that ends without a PR does
  `block` on you, with the agent's last message as the reason. `work resume` gives the
  agent one more turn in the same omp session (`omp --resume`). Merging the PR closes the
  issue, and the next sync marks the item done.
- **Visibility:** the cockpit's Agents panel and `/runs/<id>` update live over server-sent
  events (Postgres `LISTEN/NOTIFY`). Each run records turns, tool calls and failures,
  tokens, cost, the last action, its final message and its PR. A run whose heartbeat is
  more than 60s old shows as **lost**. Raw omp JSON is in `~/.local/state/workplane/runs/`.
- **Limits:** 2 concurrent runs per host and 45 minutes per run (`[runner]` in `workplane.toml`).
  To continue interactively, run `cd <worktree> && omp -r <session>`; the run page shows the command.

**Risks:** runs use `approval_mode = "yolo"` with your own gh token and your whole home
directory. A sandbox run went looking through `~/AGENTS.md`, `~/.claude/` and a local
memory database for an answer. Issue text is untrusted input; only run agents on issues
you've read. Isolation (containers or OpenShell, scoped tokens) is the next phase.

## API

The JSON API is the surface for the CLI, the runner and, later, agents: `GET /api/work`,
`GET /api/work/{id}`, `GET /api/resolve?ref=`, `POST /api/work` (capture),
`POST /api/work/{id}/events` (`{"type": "start", "actor": "pi", "payload": {}}`),
`GET /api/views/needs-me`, `GET /api/summary`, `GET /api/commands`, `POST /api/sync`.
Runs: `POST /api/runs`, `GET /api/runs[?active&work_item_id&since_hours]`, `GET /api/runs/{id}`,
`GET|POST /api/runs/{id}/events`, `POST /api/runs/{id}/finish`, `POST /api/runs/{id}/stop`,
`GET /api/runner-config`, `GET /api/stream` (SSE). Errors return `{"error", "kind"}` with
404/409/422.

## Develop

```bash
docker compose up -d db
uv run pytest            # uses a throwaway workplane_test database on :5433
```

The schema avoids arrays and enums (it uses JSONB and CHECK constraints) so it stays portable to
SQLite/D1.
