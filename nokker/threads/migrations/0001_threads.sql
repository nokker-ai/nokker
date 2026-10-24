-- Fils permanents (P1, 18.10.2026, docs/BUBBLES.md §1) — table INDÉPENDANTE de `threads` du store F2
-- (nokker/store/migrations/001_init.sql, qui ne porte que l'id et la date des tours) : `nokker_threads`
-- porte titre, propriétaire, membres et état vivant. Pas de clé étrangère (même raison que
-- 0001_approvals.sql : piliers écrits en parallèle). Appliquée par `nokker/threads/pg.py::PgThreads`
-- (idempotent : IF NOT EXISTS partout).

CREATE TABLE IF NOT EXISTS nokker_threads (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    owner         TEXT,
    members       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at    DOUBLE PRECISION NOT NULL,
    updated_at    DOUBLE PRECISION NOT NULL,
    working       BOOLEAN NOT NULL DEFAULT false,
    last_snippet  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_nokker_threads_updated ON nokker_threads (updated_at DESC);

-- Boîte de contexte : blocs à injecter au PROCHAIN tour du fil (bulle reçue, message d'une session).
CREATE TABLE IF NOT EXISTS nokker_thread_inbox (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    block         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_nokker_thread_inbox_pending ON nokker_thread_inbox (thread_id) WHERE consumed_at IS NULL;
