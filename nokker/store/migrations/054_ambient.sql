-- Ambiant du foyer : écrans e-ink et tableau commun (docs/AMBIENT.md, nokker/ambient/). Rejouée à chaque démarrage :
-- idempotente (IF NOT EXISTS, états vérifiés). NUMÉRO 054 (049 sur la branche, renumérotée à l'intégration du train 0311) ; avant actor_tenant (la dernière),
-- aucune dépendance d'ordre avec les migrations voisines.
--
-- 1. `ambient_displays` : HORS RLS, comme `channel_pairing` (048) et `devices` : l'écran présente son jeton AVANT que
--    le locataire soit connu (c'est le jeton qui le donne). Aucun contenu ici : l'empreinte SHA-256 du jeton (jamais le
--    jeton), la taille de l'écran, le foyer et son locataire. Accès seulement par nokker/ambient/displays.py, qui
--    filtre par locataire ET foyer pour toute liste ou révocation. Effacé avec le locataire (`nokker erase`, colonne
--    tenant_id) et avec le compte qui l'a créé (colonne account_id).
--    `kind` : `eink` (écran du foyer) ou `glance` (vue lunettes en lecture seule d'un compte, docs/AMBIENT.md §6).
CREATE TABLE IF NOT EXISTS ambient_displays (
    id             text PRIMARY KEY,                -- dp_ + 16 hex
    kind           text NOT NULL DEFAULT 'eink' CHECK (kind IN ('eink', 'glance')),
    tenant_id      text NOT NULL,                   -- locataire du FOYER (celui du payeur) ; glance hors foyer : du compte
    household_id   text NOT NULL,                   -- '' : vue glance d'un compte sans foyer
    account_id     text NOT NULL,                   -- e-ink : adulte qui l'a enregistré ; glance : compte dont il lit les données
    name           text NOT NULL,
    model          text NOT NULL,
    width          integer NOT NULL,
    height         integer NOT NULL,
    depth          integer NOT NULL CHECK (depth IN (1, 2)),
    refresh_s      integer NOT NULL,
    token_sha256   text,                            -- NULL : en attente d'appairage (TRMNL /api/setup, code glance)
    pair_sha256    text,                            -- empreinte de la MAC (TRMNL) ou du code court (glance)
    setup_until    double precision,                -- fin de la fenêtre d'appairage (epoch)
    created_at     double precision NOT NULL,
    revoked_at     double precision,
    last_seen_at   double precision
);
CREATE UNIQUE INDEX IF NOT EXISTS ambient_displays_token ON ambient_displays (token_sha256)
    WHERE token_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS ambient_displays_household ON ambient_displays (tenant_id, household_id);
CREATE INDEX IF NOT EXISTS ambient_displays_pair ON ambient_displays (pair_sha256) WHERE pair_sha256 IS NOT NULL;

-- 2. `ambient_boards` : le tableau COMMUN d'un foyer (brief du jour, agenda, courses, météo), au locataire du foyer,
--    sous RLS activée ET forcée (docs/TENANCY.md §4). Une ligne par foyer ; `version` croît à chaque écriture.
CREATE TABLE IF NOT EXISTS ambient_boards (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    household_id  text NOT NULL,
    content       jsonb NOT NULL DEFAULT '{}'::jsonb,
    version       integer NOT NULL DEFAULT 0,
    updated_at    double precision NOT NULL DEFAULT 0,
    updated_by    text NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, household_id)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ambient_boards' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE ambient_boards ENABLE ROW LEVEL SECURITY;
        ALTER TABLE ambient_boards FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'ambient_boards' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON ambient_boards
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
