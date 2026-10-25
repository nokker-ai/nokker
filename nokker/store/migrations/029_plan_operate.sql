-- Plan et incidents (lot X18, docs/LOKKAL.md §7.18) : sources d'alerte signées, signatures déjà reçues (anti-rejeu),
-- incidents ; cartes du plan, propositions de découpe, brief du matin. Toutes les tables sous RLS activée ET forcée par
-- tenant_id (comme 021, 024) ; pas de clé étrangère (piliers écrits en parallèle) ; `nokker erase` les efface par
-- tenant_id. Idempotente : rejouée à chaque démarrage (apply_migrations) et par les stores qui en dépendent.

-- ─────────────────────────────── incidents ───────────────────────────────

CREATE TABLE IF NOT EXISTS nokker_alert_sources (
    id            TEXT PRIMARY KEY,                       -- as_<12 hex>, dans l'URL /v1/alerts/<id>
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    name          TEXT NOT NULL,
    format        TEXT NOT NULL DEFAULT 'generic' CHECK (format IN ('alertmanager', 'generic')),
    key           TEXT,                                   -- clé HMAC : "enc:<fernet>" ou "raw:<clé>", jamais servie
    created_by    TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    last_seen_at  DOUBLE PRECISION
);

-- Signatures déjà reçues par source, gardées le temps de leur validité (± 5 min, donc 10 min) : un rejeu est refusé
-- même après un redémarrage du noyau.
CREATE TABLE IF NOT EXISTS nokker_alert_seen (
    source_id   TEXT NOT NULL,
    sig         TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    expires_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (source_id, sig)
);
CREATE INDEX IF NOT EXISTS nokker_alert_seen_exp ON nokker_alert_seen (expires_at);

CREATE TABLE IF NOT EXISTS nokker_incidents (
    id             TEXT PRIMARY KEY,                      -- in_<12 hex>
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    source_id      TEXT NOT NULL,
    source_name    TEXT NOT NULL DEFAULT '',
    fingerprint    TEXT NOT NULL,
    name           TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    severity       TEXT NOT NULL DEFAULT 'warning',
    state          TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'in_progress', 'resolved')),
    labels         JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary        TEXT NOT NULL DEFAULT '',
    occurrences    INTEGER NOT NULL DEFAULT 1,
    first_seen_at  DOUBLE PRECISION NOT NULL,
    last_seen_at   DOUBLE PRECISION NOT NULL,
    started_at     DOUBLE PRECISION,
    resolved_at    DOUBLE PRECISION,
    resolved_by    TEXT,
    acked_by       TEXT,
    runs           JSONB NOT NULL DEFAULT '[]'::jsonb,
    timeline       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL
);
-- Un seul incident vivant par (source, empreinte) : deux envois simultanés ne font pas deux incidents.
CREATE UNIQUE INDEX IF NOT EXISTS nokker_incidents_live_fp ON nokker_incidents (tenant_id, source_id, fingerprint)
    WHERE state <> 'resolved';
CREATE INDEX IF NOT EXISTS nokker_incidents_state ON nokker_incidents (state, created_at DESC);

-- ─────────────────────────────── plan ───────────────────────────────

CREATE TABLE IF NOT EXISTS nokker_plan_cards (
    id           TEXT PRIMARY KEY,                        -- pc_<12 hex>
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    parent_id    TEXT,
    project      TEXT,
    kind         TEXT NOT NULL DEFAULT 'task' CHECK (kind IN ('goal', 'milestone', 'task')),
    title        TEXT NOT NULL,
    intent       TEXT NOT NULL DEFAULT '',
    constraints  JSONB NOT NULL DEFAULT '[]'::jsonb,
    decisions    JSONB NOT NULL DEFAULT '[]'::jsonb,
    status       TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'blocked', 'done')),
    agents       JSONB NOT NULL DEFAULT '[]'::jsonb,
    threads      JSONB NOT NULL DEFAULT '[]'::jsonb,
    position     INTEGER NOT NULL DEFAULT 0,
    created_by   TEXT NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS nokker_plan_cards_parent ON nokker_plan_cards (parent_id);

CREATE TABLE IF NOT EXISTS nokker_plan_proposals (
    id           TEXT PRIMARY KEY,                        -- pp_<12 hex>
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    card_id      TEXT NOT NULL,
    children     JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason       TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed', 'accepted', 'refused', 'stale')),
    proposed_by  TEXT NOT NULL,
    thread_id    TEXT,
    bubble_id    TEXT,
    created      JSONB NOT NULL DEFAULT '[]'::jsonb,
    decided_by   TEXT,
    decided_at   DOUBLE PRECISION,
    created_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS nokker_plan_proposals_card ON nokker_plan_proposals (card_id, created_at DESC);

-- Brief du matin : l'agent Crew (X14) qui le porte, un par personne.
CREATE TABLE IF NOT EXISTS nokker_plan_briefs (
    owner       TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    agent_id    TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, owner)
);

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['nokker_alert_sources', 'nokker_alert_seen', 'nokker_incidents', 'nokker_plan_cards',
                             'nokker_plan_proposals', 'nokker_plan_briefs'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
END $$;
