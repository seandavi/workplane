-- GitHub is the external system of record; work_items + work_events are ours.
--
-- Conventions (the code that relies on them is workplane/db.py):
--  * Timestamps are UTC text in ONE format, 2026-10-05T13:30:07.252Z, so comparing two of them as
--    strings is comparing them in time. Write them with db.NOW / db.ts(), never with datetime(),
--    date() or CURRENT_TIMESTAMP, which produce a different format.
--  * Columns declared TIMESTAMP, DATE, JSON and BOOLEAN come back from the driver as datetime,
--    date, list/dict and bool.

CREATE TABLE repositories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name   TEXT NOT NULL UNIQUE,
    is_private  BOOLEAN NOT NULL DEFAULT 0,
    area        TEXT,
    synced_at   TIMESTAMP
);

CREATE TABLE github_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id         TEXT NOT NULL UNIQUE,
    repository_id   INTEGER NOT NULL REFERENCES repositories(id),
    number          INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('issue', 'pr')),
    title           TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('open', 'closed', 'merged')),
    state_reason    TEXT,
    author          TEXT,
    labels          JSON NOT NULL DEFAULT '[]',
    assignees       JSON NOT NULL DEFAULT '[]',
    review_requests JSON NOT NULL DEFAULT '[]',
    comments_count  INTEGER NOT NULL DEFAULT 0,
    is_draft        BOOLEAN NOT NULL DEFAULT 0,
    is_noise        BOOLEAN NOT NULL DEFAULT 0,
    url             TEXT NOT NULL,
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    closed_at       TIMESTAMP,
    UNIQUE (repository_id, number)
);

CREATE TABLE work_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL CHECK (source IN ('github', 'manual')),
    github_item_id    INTEGER UNIQUE REFERENCES github_items(id),
    title             TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
                        ('inbox', 'backlog', 'ready', 'working', 'review', 'blocked', 'done', 'cancelled')),
    area              TEXT,             -- override; github items fall back to repositories.area
    priority          INTEGER CHECK (priority BETWEEN 0 AND 3),
    due_on            DATE,
    next_action       TEXT,
    waiting_on        TEXT,
    claimed_by        TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at        TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status_changed_at TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK ((source = 'github') = (github_item_id IS NOT NULL)),
    CHECK ((status = 'blocked') = (waiting_on IS NOT NULL))
);

CREATE INDEX work_items_status_idx ON work_items (status);

CREATE TABLE work_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id  INTEGER NOT NULL REFERENCES work_items(id),
    occurred_at   TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actor         TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT,
    payload       JSON NOT NULL DEFAULT '{}',
    dedupe_key    TEXT UNIQUE
);

CREATE INDEX work_events_item_idx ON work_events (work_item_id, occurred_at);

CREATE TABLE sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
    COALESCE(g.is_noise, 0) AS is_noise,
    g.url,
    g.created_at AS github_created_at,
    g.updated_at AS github_updated_at
FROM work_items w
LEFT JOIN github_items g ON g.id = w.github_item_id
LEFT JOIN repositories r ON r.id = g.repository_id;
