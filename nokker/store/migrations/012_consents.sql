-- Nokker — consentements du programme bêta (usage des conversations pour améliorer les modèles). Ajout seul : chaque
-- changement (accord à l'inscription, retrait depuis « Mon compte ») est une ligne ; l'état courant est la dernière.
-- Contenu de locataire : tenant_id par défaut = app.tenant_id, RLS activée ET forcée (docs/TENANCY.md §4). Pas de clé
-- étrangère : l'effacement du compte doit pouvoir supprimer ces lignes. Rejouée à chaque démarrage.
CREATE TABLE IF NOT EXISTS data_consents (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id        TEXT NOT NULL,
    scope             TEXT NOT NULL CHECK (scope IN ('training')),
    granted           BOOLEAN NOT NULL,
    source            TEXT NOT NULL CHECK (source IN ('signup', 'account')),
    terms_sha256      TEXT,                       -- version du règlement affiché au moment du choix
    ip                TEXT,
    at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS data_consents_account_idx ON data_consents (tenant_id, account_id, scope, id DESC);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'data_consents' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE data_consents ENABLE ROW LEVEL SECURITY;
        ALTER TABLE data_consents FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'data_consents' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON data_consents
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
