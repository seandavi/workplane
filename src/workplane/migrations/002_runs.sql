-- A run is one agent invocation against one work item. Resuming starts a new
-- run that shares the harness session, worktree and branch of the previous one.

CREATE TABLE runs (
    id                 BIGSERIAL PRIMARY KEY,
    work_item_id       BIGINT NOT NULL REFERENCES work_items(id),
    resumed_from       BIGINT REFERENCES runs(id),
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
    stop_requested     BOOLEAN NOT NULL DEFAULT FALSE,
    turns              INTEGER NOT NULL DEFAULT 0,
    tool_calls         INTEGER NOT NULL DEFAULT 0,
    tool_errors        INTEGER NOT NULL DEFAULT 0,
    tokens_in          BIGINT NOT NULL DEFAULT 0,
    tokens_out         BIGINT NOT NULL DEFAULT 0,
    tokens_cache_read  BIGINT NOT NULL DEFAULT 0,
    tokens_cache_write BIGINT NOT NULL DEFAULT 0,
    cost_usd           NUMERIC(12, 6) NOT NULL DEFAULT 0,
    last_activity      TEXT,
    last_event_at      TIMESTAMPTZ,
    heartbeat_at       TIMESTAMPTZ,
    final_message      TEXT,
    pr_url             TEXT,
    exit_code          INTEGER,
    error              TEXT,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at           TIMESTAMPTZ
);

CREATE INDEX runs_item_idx ON runs (work_item_id, started_at DESC);
CREATE INDEX runs_active_idx ON runs (state) WHERE state IN ('starting', 'running');

CREATE TABLE run_events (
    id           BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL REFERENCES runs(id),
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind         TEXT NOT NULL,
    summary      TEXT,
    payload      JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX run_events_run_idx ON run_events (run_id, id);
