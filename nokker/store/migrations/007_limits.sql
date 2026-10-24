-- Nokker — plafonds de la bêta (docs/TENANCY.md §5, nokker/tenancy/limits.py) : tours et appels Deep par compte et par
-- jour (Europe/Zurich, calculé par le noyau). Le locataire interne n'est jamais compté. Même forme que les tables de
-- contenu (§4) : tenant_id par défaut = app.tenant_id, RLS activée ET forcée.

CREATE TABLE IF NOT EXISTS tenant_usage (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account     TEXT NOT NULL,                          -- Turn.sender (us_…)
    day         DATE NOT NULL,
    turns       INTEGER NOT NULL DEFAULT 0,
    deep        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, account, day)
);

-- Rejouée à chaque démarrage : on ne touche à la table que si l'état n'y est pas (pas de verrou exclusif à chaque fois).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'tenant_usage' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE tenant_usage ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_usage FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'tenant_usage' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON tenant_usage
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
