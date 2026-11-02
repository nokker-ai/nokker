-- Guide (chantier p2/guide, docs/GUIDE.md) : compteurs d'usage PAR CODE de fonction, jamais de contenu. Rejouée à
-- chaque démarrage : idempotente. Numéro 062 (gardé à l'intégration dans le train). Table créée même
-- drapeau `guide` éteint (aucun effet sans lui).
--
-- Contenu de locataire : tenant_id par défaut = app.tenant_id, RLS activée ET forcée (docs/TENANCY.md §4) ; effacé
-- par `nokker erase` (colonne tenant_id). Une ligne par (locataire, fonction) : nombre d'usages, premier et dernier.
-- La ligne `guide.intro` note que le tour d'accueil a été vu.
CREATE TABLE IF NOT EXISTS nokker_guide_usage (
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    feature_id   TEXT NOT NULL,
    uses         INTEGER NOT NULL DEFAULT 0,
    first_at     DOUBLE PRECISION NOT NULL,
    last_at      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, feature_id)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'nokker_guide_usage' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE nokker_guide_usage ENABLE ROW LEVEL SECURITY;
        ALTER TABLE nokker_guide_usage FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'nokker_guide_usage' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nokker_guide_usage USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
