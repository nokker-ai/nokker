-- Crew minimal (P1, 18.10.2026, docs/BUBBLES.md §9.4) — pas d'exécution réelle (P2, hors scope) :
-- `status` reste `armed` à la création. Pas de clé étrangère (même raison que les autres migrations
-- de ce chantier : piliers écrits en parallèle). Appliquée par `nokker/agents/pg.py::PgAgents`
-- (idempotent).

CREATE TABLE IF NOT EXISTS nokker_agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    tools       JSONB NOT NULL DEFAULT '[]'::jsonb,
    status      TEXT NOT NULL DEFAULT 'armed',
    created_by  TEXT NOT NULL DEFAULT 'nina',
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nokker_agents_created ON nokker_agents (created_at);
