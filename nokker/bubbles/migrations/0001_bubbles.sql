-- Bulles (P1, 18.10.2026, docs/BUBBLES.md §2) — `thread_id` NULL pour une bulle `notify` sans fil (§4).
-- Pas de clé étrangère vers nokker_threads (même raison que 0001_approvals.sql). Appliquée par
-- `nokker/bubbles/pg.py::PgBubbles` (idempotent).

CREATE TABLE IF NOT EXISTS nokker_bubbles (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'idle',
    state           JSONB NOT NULL DEFAULT '{}'::jsonb,
    actions         JSONB NOT NULL DEFAULT '[]'::jsonb,
    origin          JSONB,
    shared_members  JSONB NOT NULL DEFAULT '[]'::jsonb,
    shared_threads  JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by      TEXT NOT NULL DEFAULT 'nina',
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL,
    closed_at       DOUBLE PRECISION,
    version         INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_nokker_bubbles_thread ON nokker_bubbles (thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nokker_bubbles_open_notify ON nokker_bubbles (kind) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nokker_bubbles_shared_threads ON nokker_bubbles USING gin (shared_threads);
