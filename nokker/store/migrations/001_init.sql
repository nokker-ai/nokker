-- Nokker — schéma initial du noyau (PLAN-P1 §2.7 : Postgres dès le jour 1, D3).
-- Un tour sérialisé en JSONB (nokker/store/pg.py:turn_to_dict). Les tours sont APPEND-ONLY : aucune
-- UPDATE ni DELETE n'est permise sur `turns` (trigger ci-dessous), la correction d'un fait passe par un
-- tour suivant, jamais par une réécriture du passé (cache amont, réservoir, journal d'audit).
--
-- `verdicts` et `llm_calls` sont dénormalisées hors du JSONB du tour pour que le Ledger (chantier F6,
-- nokker/ledger/) puisse les interroger sans parser le tour entier à chaque requête de coût.

CREATE TABLE IF NOT EXISTS threads (
    thread_id   TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS turns (
    thread_id   TEXT NOT NULL REFERENCES threads(thread_id),
    turn_index  INTEGER NOT NULL,
    turn        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_turns_thread ON turns (thread_id, turn_index);

-- Append-only : toute UPDATE/DELETE sur turns échoue explicitement plutôt que de réussir en silence.
CREATE OR REPLACE FUNCTION turns_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'turns est append-only (fil %, tour %) : UPDATE/DELETE interdits (PLAN-P1 §2.7)',
        OLD.thread_id, OLD.turn_index;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_turns_append_only ON turns;
CREATE TRIGGER trg_turns_append_only
    BEFORE UPDATE OR DELETE ON turns
    FOR EACH ROW EXECUTE FUNCTION turns_append_only();

CREATE TABLE IF NOT EXISTS verdicts (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    verdict     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_verdicts_thread ON verdicts (thread_id, turn_index);

CREATE TABLE IF NOT EXISTS llm_calls (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    call        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_thread ON llm_calls (thread_id, turn_index);
