-- Nokker — chantier « never clear » (docs/NEVER-CLEAR.md §7) : épisodes, résumés de jour, état de vue, réservoir.
-- Tables séparées de `turns` (append-only, jamais modifiée ici). Les horodatages sont des flottants epoch
-- (comme les dataclasses de nokker/context/types.py), pas des TIMESTAMPTZ : aller-retour exact.

CREATE TABLE IF NOT EXISTS nokker_episodes (
    id               TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    first_turn       INTEGER NOT NULL,
    last_turn        INTEGER,
    title            TEXT NOT NULL DEFAULT '',
    summary          TEXT NOT NULL DEFAULT '',
    keywords         JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_tokens       INTEGER NOT NULL DEFAULT 0,
    summary_tokens   INTEGER NOT NULL DEFAULT 0,
    opened_at        DOUBLE PRECISION NOT NULL DEFAULT 0,
    closed_at        DOUBLE PRECISION,
    closed_reason    TEXT,
    day              TEXT,
    turn_ids_sha256  TEXT NOT NULL DEFAULT '',
    refined          BOOLEAN NOT NULL DEFAULT FALSE,
    bubble_id        TEXT,
    version          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS nokker_episodes_thread_seq ON nokker_episodes (thread_id, seq);
CREATE INDEX IF NOT EXISTS nokker_episodes_open ON nokker_episodes (thread_id) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS nokker_day_summaries (
    thread_id    TEXT NOT NULL,
    day          TEXT NOT NULL,
    id           TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    episode_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    tokens       INTEGER NOT NULL DEFAULT 0,
    created_at   DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (thread_id, day)
);

CREATE TABLE IF NOT EXISTS nokker_view_state (
    thread_id                TEXT PRIMARY KEY,
    episode_id               TEXT,
    base_turn                INTEGER NOT NULL DEFAULT 0,
    last_turn_at             DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_turn_index          INTEGER NOT NULL DEFAULT -1,
    tokens_since_compaction  INTEGER NOT NULL DEFAULT 0,
    compaction_version       INTEGER NOT NULL DEFAULT 0,
    shed                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    pinned_episodes          JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at               DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS nokker_reservoir (
    id          TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    preview     TEXT NOT NULL DEFAULT '',
    tokens      INTEGER NOT NULL DEFAULT 0,
    turn_index  INTEGER,
    call_id     TEXT,
    created_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
    origin      JSONB
);
CREATE INDEX IF NOT EXISTS nokker_reservoir_thread ON nokker_reservoir (thread_id, kind, created_at, id);
CREATE INDEX IF NOT EXISTS nokker_reservoir_call ON nokker_reservoir (thread_id, turn_index, call_id);

-- 20.10.2026 (option A, revue) : plancher des en-têtes de tour rejoués (ViewState.heads_shed_before). Oublié à la création
-- du champ : sur le canari, la valeur posée par compact() était perdue à la relecture suivante — aucun en-tête n'était
-- jamais retiré de la fenêtre. Idempotent : rejoué à chaque connexion comme le reste du fichier.
ALTER TABLE nokker_view_state ADD COLUMN IF NOT EXISTS heads_shed_before INTEGER NOT NULL DEFAULT 0;
