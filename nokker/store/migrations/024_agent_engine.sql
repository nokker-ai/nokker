-- Moteur Crew (lot X14, docs/BUBBLES.md §9.5) : les champs d'un agent qui tourne, ses passages, et l'origine des fils
-- de passage. Appliquée par apply_migrations (après les piliers) ET par les stores qui en dépendent
-- (nokker/agents/pg.py, nokker/threads/pg.py) : chaque étape vérifie l'état avant d'agir (aucun ALTER, donc aucun
-- verrou exclusif, une fois les colonnes en place) ; une table pas encore créée est traitée au passage suivant.
DO $$
DECLARE
    col  RECORD;
BEGIN
    IF to_regclass('nokker_agents') IS NOT NULL THEN
        FOR col IN SELECT * FROM (VALUES
            ('instructions',    'TEXT NOT NULL DEFAULT '''''),
            ('trigger',         'JSONB NOT NULL DEFAULT ''{"kind": "manual"}''::jsonb'),
            ('next_run_at',     'DOUBLE PRECISION'),
            ('last_run',        'JSONB'),
            ('secrets',         'JSONB NOT NULL DEFAULT ''[]''::jsonb'),
            ('budget',          'JSONB'),
            ('auto_approve',    'JSONB NOT NULL DEFAULT ''[]''::jsonb'),
            ('placement',       'JSONB'),
            ('project',         'TEXT'),
            ('armed',           'BOOLEAN NOT NULL DEFAULT true'),
            ('approved_rules',  'TEXT'),
            ('arm_approval_id', 'TEXT'),
            ('webhook_key',     'TEXT')
        ) AS c(name, ddl) LOOP
            IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_agents'::regclass
                           AND attname = col.name AND NOT attisdropped) THEN
                EXECUTE format('ALTER TABLE nokker_agents ADD COLUMN %I %s', col.name, col.ddl);
            END IF;
        END LOOP;
    END IF;
    -- Fils de passage : {kind: "agent", agent_id, run_id} (les autres fils : NULL, `origin` absent du JSON).
    IF to_regclass('nokker_threads') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_threads'::regclass
                       AND attname = 'origin' AND NOT attisdropped) THEN
        ALTER TABLE nokker_threads ADD COLUMN origin JSONB;
    END IF;
END
$$;

-- Passages : une ligne par passage (y compris `skipped`). Sous RLS activée ET forcée par tenant_id, comme 021 ; pas de
-- clé étrangère (piliers écrits en parallèle) ; `nokker erase` l'efface par tenant_id.
CREATE TABLE IF NOT EXISTS nokker_agent_runs (
    id             TEXT PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    trigger        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled',
                                     'skipped')),
    scheduled_for  DOUBLE PRECISION,
    started_at     DOUBLE PRECISION,
    ended_at       DOUBLE PRECISION,
    thread_id      TEXT,
    cost           JSONB,
    summary        TEXT,
    catch_up       BOOLEAN NOT NULL DEFAULT false,
    event          TEXT,
    input          TEXT NOT NULL DEFAULT '',  -- données non fiables du déclencheur, déjà caviardées
    approvals      JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS nokker_agent_runs_agent ON nokker_agent_runs (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS nokker_agent_runs_live ON nokker_agent_runs (status)
    WHERE status IN ('queued', 'running', 'waiting_approval');
CREATE INDEX IF NOT EXISTS nokker_agent_runs_thread ON nokker_agent_runs (thread_id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'nokker_agent_runs' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE nokker_agent_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE nokker_agent_runs FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'nokker_agent_runs'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nokker_agent_runs
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
