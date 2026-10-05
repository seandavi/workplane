-- A run is one agent invocation against one work item. Resuming starts a new
-- run that shares the harness session, worktree and branch of the previous one.

CREATE TABLE runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id       INTEGER NOT NULL REFERENCES work_items(id),
    resumed_from       INTEGER REFERENCES runs(id),
    harness            TEXT NOT NULL,
    host               TEXT NOT NULL,
    actor              TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'starting'
                         CHECK (state IN ('starting', 'running', 'finished', 'failed', 'stopped')),
    outcome            TEXT CHECK (outcome IN ('pr', 'blocked', 'no_pr', 'error', 'stopped')),
    harness_session_id TEXT,
    model              TEXT,
    worktree           TEXT NOT NULL,
    branch             TEXT NOT NULL,
    base_ref           TEXT,
    session_dir        TEXT,
    log_path           TEXT,
    pid                INTEGER,
    prompt             TEXT NOT NULL,
    stop_requested     BOOLEAN NOT NULL DEFAULT 0,
    turns              INTEGER NOT NULL DEFAULT 0,
    tool_calls         INTEGER NOT NULL DEFAULT 0,
    tool_errors        INTEGER NOT NULL DEFAULT 0,
    tokens_in          INTEGER NOT NULL DEFAULT 0,
    tokens_out         INTEGER NOT NULL DEFAULT 0,
    tokens_cache_read  INTEGER NOT NULL DEFAULT 0,
    tokens_cache_write INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    last_activity      TEXT,
    last_event_at      TIMESTAMP,
    heartbeat_at       TIMESTAMP,
    final_message      TEXT,
    pr_url             TEXT,
    exit_code          INTEGER,
    error              TEXT,
    started_at         TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at           TIMESTAMP
);

CREATE INDEX runs_item_idx ON runs (work_item_id, started_at DESC);
CREATE INDEX runs_active_idx ON runs (state) WHERE state IN ('starting', 'running');

CREATE TABLE run_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    occurred_at  TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind         TEXT NOT NULL,
    summary      TEXT,
    payload      JSON NOT NULL DEFAULT '{}'
);

CREATE INDEX run_events_run_idx ON run_events (run_id, id);
