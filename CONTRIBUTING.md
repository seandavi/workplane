# Contributing

Issues and pull requests are welcome. For anything bigger than a bug fix, open an issue first so we can agree on the shape.

## Set up

```bash
uv sync
uv run pytest        # each test gets its own temporary SQLite file; nothing to start first
uv run ruff check
```

CI runs the same two commands.

## Conventions the code relies on

- **Every change to a work item goes through `work.apply_event`.** It checks the transition with `domain.decide`, appends to `work_events` and updates the `work_items` projection in one transaction. Don't write `work_items.status` anywhere else.
- **Timestamps are UTC text in one format.** In SQL write `db.NOW`; in Python pass an aware `datetime` (it is converted by `db.ts`). Never use `datetime()`, `date()` or `CURRENT_TIMESTAMP`: they produce another format, and comparing the two as strings gives wrong answers without any error.
- **Transactions use `async with conn.transaction()`.** The outermost one is `BEGIN IMMEDIATE`, which is what makes check-then-write sequences (the WIP limit, run claims) safe; nested ones are savepoints. `conn.notify(...)` messages go out to the dashboard only after the outermost commit.
- **Migrations are append-only.** Add `src/workplane/migrations/NNN_name.sql`; never edit one that has shipped.
- **One server process.** The sync lock and the live-update hub live in memory, so `workplane-server` runs with a single worker.
- **Tests run against a real SQLite file, not mocks.** Test behavior a user could see.

## Pull requests

Keep them small, one concern each, with tests for the behavior they change. CI must pass.
