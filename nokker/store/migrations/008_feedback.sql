-- Nokker — retours des testeurs de la bêta (`/retour` du bot, POST /v1/feedback). Contenu de locataire : tenant_id par
-- défaut = app.tenant_id, RLS activée ET forcée (docs/TENANCY.md §4). Rejouée à chaque démarrage, sans verrou inutile.
CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    channel     TEXT NOT NULL,                    -- telegram | web
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feedback_tenant_idx ON feedback (tenant_id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'feedback' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
        ALTER TABLE feedback FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'feedback' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON feedback
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
