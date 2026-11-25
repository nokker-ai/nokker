-- Nokker — zones de l'app par membre (docs/LOKKAL-PHONE.md §4.2, nokker/zones/). Additive et idempotente.
--
-- Les zones vivent SUR L'APPAREIL (centre et rayon) ; le noyau ne connaît que leur id, leur nom et les scénarios à
-- lancer : JAMAIS de coordonnées ici. Locataire du membre (tenant_isolation), propriétaire = le compte.
-- 1. `member_zones` : 18 zones au plus par membre (contrôlé par le service), dernière transition connue (`state`,
--    `state_at`) pour la présence et l'ordre des événements.
-- 2. `zone_events` : identifiants d'événement vus (idempotence) et résultat rendu, gardés 7 jours ; ni historique des
--    déplacements au-delà, ni position.
-- Numéro 075 : 074 laissé libre (branches en cours).

CREATE TABLE IF NOT EXISTS member_zones (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    zone_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    on_enter    TEXT[] NOT NULL DEFAULT '{}',
    on_exit     TEXT[] NOT NULL DEFAULT '{}',
    is_home     BOOLEAN NOT NULL DEFAULT false,              -- LA zone maison du membre (une seule, contrôlé au service)
    position    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT '' CHECK (state IN ('', 'inside', 'outside')),
    state_at    TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, zone_id)
);

ALTER TABLE member_zones ADD COLUMN IF NOT EXISTS is_home BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS zone_events (
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id   TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    zone_id      TEXT NOT NULL,
    event        TEXT NOT NULL CHECK (event IN ('enter', 'exit')),
    at           TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    result       JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (account_id, event_id)
);
CREATE INDEX IF NOT EXISTS zone_events_received ON zone_events (account_id, received_at);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['member_zones', 'zone_events'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(t) AND tgname = t || '_tenant_immutable') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OF tenant_id ON %I FOR EACH ROW '
                           'EXECUTE FUNCTION nokker_tenant_immutable()', t || '_tenant_immutable', t);
        END IF;
    END LOOP;
END $$;
