-- Export des données d'un compte (« Mon compte », nokker/account/export.py, docs/CLIENT-API.md) : un fichier JSON
-- compressé, gardé 24 h, une demande par 24 h. Contenu de locataire : tenant_id par défaut = app.tenant_id, RLS
-- activée ET forcée (docs/TENANCY.md §4) ; l'effacement du compte (`nokker erase`) la couvre par tenant_id.
-- Rejouée à chaque démarrage.
CREATE TABLE IF NOT EXISTS account_exports (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    expires_at  DOUBLE PRECISION NOT NULL,
    body        BYTEA NOT NULL
);
CREATE INDEX IF NOT EXISTS account_exports_account_idx ON account_exports (tenant_id, account_id, created_at DESC);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'account_exports' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE account_exports ENABLE ROW LEVEL SECURITY;
        ALTER TABLE account_exports FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'account_exports' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON account_exports
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
