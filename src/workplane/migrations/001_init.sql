-- GitHub is the external system of record; work_items + work_events are ours.
-- Kept to portable SQL (no arrays, no enums) so the schema can move to SQLite/D1.

CREATE TABLE repositories (
    id          BIGSERIAL PRIMARY KEY,
    full_name   TEXT NOT NULL UNIQUE,
    is_private  BOOLEAN NOT NULL DEFAULT FALSE,
    area        TEXT,
    synced_at   TIMESTAMPTZ
);

CREATE TABLE github_items (
    id              BIGSERIAL PRIMARY KEY,
    node_id         TEXT NOT NULL UNIQUE,
    repository_id   BIGINT NOT NULL REFERENCES repositories(id),
    number          INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('issue', 'pr')),
    title           TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('open', 'closed', 'merged')),
    state_reason    TEXT,
    author          TEXT,
    labels          JSONB NOT NULL DEFAULT '[]',
    assignees       JSONB NOT NULL DEFAULT '[]',
    review_requests JSONB NOT NULL DEFAULT '[]',
    comments_count  INTEGER NOT NULL DEFAULT 0,
    is_draft        BOOLEAN NOT NULL DEFAULT FALSE,
    is_noise        BOOLEAN NOT NULL DEFAULT FALSE,
    url             TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    UNIQUE (repository_id, number)
);

CREATE TABLE work_items (
    id                BIGSERIAL PRIMARY KEY,
    source            TEXT NOT NULL CHECK (source IN ('github', 'manual')),
    github_item_id    BIGINT UNIQUE REFERENCES github_items(id),
    title             TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
                        ('inbox', 'backlog', 'ready', 'working', 'review', 'blocked', 'done', 'cancelled')),
    area              TEXT,             -- override; github items fall back to repositories.area
    priority          SMALLINT CHECK (priority BETWEEN 0 AND 3),
    due_on            DATE,
    next_action       TEXT,
    waiting_on        TEXT,
    claimed_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((source = 'github') = (github_item_id IS NOT NULL)),
    CHECK ((status = 'blocked') = (waiting_on IS NOT NULL))
);

CREATE INDEX work_items_status_idx ON work_items (status);

CREATE TABLE work_events (
    id            BIGSERIAL PRIMARY KEY,
    work_item_id  BIGINT NOT NULL REFERENCES work_items(id),
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor         TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT,
    payload       JSONB NOT NULL DEFAULT '{}',
    dedupe_key    TEXT UNIQUE
);

CREATE INDEX work_events_item_idx ON work_events (work_item_id, occurred_at);

CREATE TABLE sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per work item with its GitHub context resolved.
CREATE VIEW work_view AS
SELECT
    w.id,
    w.source,
    w.title,
    w.status,
    COALESCE(w.area, r.area) AS area,
    w.priority,
    w.due_on,
    w.next_action,
    w.waiting_on,
    w.claimed_by,
    w.created_at,
    w.updated_at,
    w.status_changed_at,
    r.full_name AS repo,
    g.number,
    g.kind,
    g.state AS github_state,
    g.author,
    g.labels,
    g.assignees,
    g.review_requests,
    g.comments_count,
    g.is_draft,
    COALESCE(g.is_noise, FALSE) AS is_noise,
    g.url,
    g.created_at AS github_created_at,
    g.updated_at AS github_updated_at
FROM work_items w
LEFT JOIN github_items g ON g.id = w.github_item_id
LEFT JOIN repositories r ON r.id = g.repository_id;
