-- F6 (ledger) — tables propres au ledger, INDÉPENDANTES du store de F2 (nokker/store/pg.py : threads,
-- turns append-only, épisodes, réservoir, verdicts, quarantaine). thread_id/turn_index sont de simples
-- colonnes (pas de clé étrangère) : F6 et F2 sont écrits en parallèle (PLAN-P1 §5.1) et l'intégration
-- (vue jointe, FK éventuelle) se fait au merge, pas ici.
--
-- Deux tables : un appel LLM (LLMCall, T5/A3/A5) et un verdict de porte (Verdict, pilier b). psycopg 3,
-- appliquée par `PostgresLedger.migrate()` (idempotent : IF NOT EXISTS partout).

CREATE TABLE IF NOT EXISTS ledger_entries (
    id                 BIGSERIAL PRIMARY KEY,
    thread_id          TEXT NOT NULL,
    turn_index         INTEGER NOT NULL,
    tier               TEXT NOT NULL,                      -- local | ship | deep (nokker.core.turn.Tier)
    model              TEXT NOT NULL,
    reasoning          BOOLEAN NOT NULL DEFAULT FALSE,
    prefix_sha256      TEXT NOT NULL DEFAULT '',
    layer_tokens       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- tokens par couche du Context Engine (A3/A4)
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,          -- total (neufs + cache), comme usage.prompt_tokens
    cached_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    cached_reported    BOOLEAN NOT NULL DEFAULT FALSE,      -- False = amont muet sur le cache (A5, jamais déduire 0 %)
    latency_s          DOUBLE PRECISION NOT NULL DEFAULT 0,
    route_reason       TEXT NOT NULL DEFAULT '',
    chf                DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_source        TEXT NOT NULL DEFAULT 'catalog',     -- router (usage.cost du Router) | catalog (grille) | unknown
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ledger_entries_thread_idx ON ledger_entries (thread_id, turn_index);
CREATE INDEX IF NOT EXISTS ledger_entries_day_idx ON ledger_entries (((created_at AT TIME ZONE 'UTC')::date));
CREATE INDEX IF NOT EXISTS ledger_entries_tier_idx ON ledger_entries (tier);
CREATE INDEX IF NOT EXISTS ledger_entries_model_idx ON ledger_entries (model);

CREATE TABLE IF NOT EXISTS ledger_verdicts (
    id             BIGSERIAL PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    turn_index     INTEGER NOT NULL,
    gate           TEXT NOT NULL,       -- action | switch | deliverable (nokker.core.turn.Gate)
    decision       TEXT NOT NULL,       -- allow | redo_deep | hitl | escalate | deny
    layer          INTEGER NOT NULL,    -- 1 = invariant déterministe, 2 = juge
    rule           TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    probability    DOUBLE PRECISION,    -- NULL si le backend n'a pas de logprobs (D13)
    judge_model    TEXT,
    latency_s      DOUBLE PRECISION NOT NULL DEFAULT 0,
    subject        TEXT,                -- id de l'appel d'outil ou « reply »
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ledger_verdicts_thread_idx ON ledger_verdicts (thread_id, turn_index);
CREATE INDEX IF NOT EXISTS ledger_verdicts_day_idx ON ledger_verdicts (((created_at AT TIME ZONE 'UTC')::date));
CREATE INDEX IF NOT EXISTS ledger_verdicts_decision_idx ON ledger_verdicts (decision);

-- 20.10.2026 (chantier cache, scope v1 §3.1) : préfixe réutilisable mesuré par le noyau (LLMCall.reusable_tokens).
ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS reusable_tokens INTEGER;
