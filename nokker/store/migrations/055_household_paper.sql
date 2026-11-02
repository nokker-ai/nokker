-- Papiers du foyer (drapeau `household_paper`, docs/HOUSEHOLD-PAPER.md, nokker/household/paper/). Rejouée à chaque
-- démarrage (idempotente). Numéro 055 (049 sur la branche, renumérotée à l'intégration du train 0311) ; avant actor_tenant (la dernière).
-- (048 était la dernière posée sur p2/train-0211-v2 au 01.11.2026.)
--
-- Trois tables de contenu de locataire : tenant_id par défaut = app.tenant_id, RLS activée ET forcée, politique
-- `tenant_isolation` (docs/TENANCY.md §4). Aucune politique croisée : un membre du foyer ne lit JAMAIS les papiers d'un
-- autre ; un partage est une COPIE écrite dans le locataire du destinataire par le noyau, après une carte.
-- L'effacement du compte (`nokker erase`) les couvre par tenant_id (découverte par information_schema).
CREATE TABLE IF NOT EXISTS paper_documents (
    doc_id      TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    filename    TEXT NOT NULL DEFAULT '',
    media_type  TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL,
    content     BYTEA NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'inconnu',
    person      TEXT NOT NULL DEFAULT '',
    year        INTEGER,
    qr          JSONB,
    tax         JSONB,
    shared_by   TEXT,
    title       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS paper_documents_account_idx ON paper_documents (tenant_id, account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS paper_documents_sha_idx ON paper_documents (tenant_id, account_id, sha256);

CREATE TABLE IF NOT EXISTS paper_exports (
    export_id   TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('pain001', 'ics')),
    status      TEXT NOT NULL CHECK (status IN ('pending', 'ready')),
    key         TEXT NOT NULL,
    content     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    summary     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS paper_exports_key_idx ON paper_exports (tenant_id, account_id, key);

CREATE TABLE IF NOT EXISTS paper_settings (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    debtor_name TEXT NOT NULL DEFAULT '',
    debtor_iban TEXT NOT NULL DEFAULT '',
    debtor_bic  TEXT NOT NULL DEFAULT '',
    people      JSONB NOT NULL DEFAULT '[]'::jsonb,
    tax_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (tenant_id, account_id)
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['paper_documents', 'paper_exports', 'paper_settings'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
END $$;
