-- Calendrier familial (drapeau `family_calendar`, docs/FAMILY-CALENDAR.md, nokker/household/famcal/). Rejouée à chaque
-- démarrage (idempotente). Numéro 061 (060 sur la branche p2/family-calendar, renumérotée à l'intégration dans le
-- train). Ne dépend d'aucune autre table ; n'utilise que `app.tenant_id`.
--
-- Deux tables de contenu de locataire : tenant_id par défaut = app.tenant_id, RLS activée ET forcée, politique
-- `tenant_isolation` (docs/TENANCY.md §4). Aucune politique croisée : un foyer ne lit ni ne modifie jamais le brouillon
-- d'un autre. Le texte du document n'est pas rangé (lignes extraites et relues seulement).
-- L'effacement du compte (`nokker erase`) les couvre par tenant_id (découverte par information_schema).
CREATE TABLE IF NOT EXISTS family_calendar_drafts (
    draft_id    TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL CHECK (status IN ('open', 'committed')),
    origin      TEXT NOT NULL DEFAULT '',
    lines       JSONB NOT NULL DEFAULT '[]'::jsonb,
    result      JSONB NOT NULL DEFAULT '{}'::jsonb,
    timezone    TEXT NOT NULL DEFAULT 'Europe/Zurich'
);
CREATE INDEX IF NOT EXISTS family_calendar_drafts_account_idx
    ON family_calendar_drafts (tenant_id, account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS family_calendar_settings (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    people      JSONB NOT NULL DEFAULT '[]'::jsonb,
    timezone    TEXT NOT NULL DEFAULT 'Europe/Zurich',
    PRIMARY KEY (tenant_id, account_id)
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['family_calendar_drafts', 'family_calendar_settings'] LOOP
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
