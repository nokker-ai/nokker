-- Foyer (lot X23, docs/LOKKAL.md §7.29, docs/TENANCY.md §4) : un foyer est un projet d'équipe (023) de type
-- `household`. Le payeur en est l'owner ; chaque membre garde SON locataire (aucune politique de 023 ne change : un
-- membre ne lit jamais les fils, connecteurs, secrets ni la mémoire de profil d'un autre). Ce qui s'ajoute :
--
-- 1. `team_projects.kind` (team | household) et `household_policy` (liste blanche des connecteurs et agents des
--    profils mineurs) ; `team_invites.kind` et `profile` (le profil déclaré par le payeur, recopié dans l'adhésion).
-- 2. `household_members` : profil (adult | teen | child), état (active | suspended), plafond par membre, payeur.
--    UN foyer par compte (index unique sur `account_id`, tous locataires confondus) ; 6 membres au plus, payeur compris
--    (trigger sous verrou consultatif du foyer : deux acceptations simultanées ne passent pas la borne).
-- 3. `parental_consents` : attestation du représentant légal (version = sha256 du texte affiché, horodatée,
--    révocable) ; locataire du foyer (celui du payeur), jamais lue par le mineur.
-- 4. `account_minors` (HORS RLS, comme `accounts`) : « ce compte est un profil mineur ». Lue avant de connaître le
--    locataire (connexion SSO, consentement d'entraînement, miroir) ; ne porte ni contenu ni locataire du compte.
-- 5. `data_consents` : un consentement d'entraînement accordé par un compte mineur est refusé EN BASE (trigger).
-- 6. `ledger_entries.household_id`, `household_tenant` : l'étiquette du foyer posée sur chaque appel d'un tour d'un
--    membre (nokker/ledger/attribution.py) ; le quota du foyer se compte dans le ledger, aucun second compteur.
--    `household_spend_read` : un membre lit les lignes attribuées à SON foyer (des comptes de tours, aucun contenu) ;
--    `household_spend_tag` (RESTRICTIVE) : une ligne ne s'attribue à un foyer que par un membre, à son nom.
--
-- Rejouée à chaque démarrage, et par le ledger seul : chaque étape vérifie l'état avant d'agir ; une table absente
-- (équipe ou consentements pas encore migrés) : l'étape attend le passage suivant.

-- ─────────────────────────────── 1. type de projet, invitations ───────────────────────────────
DO $$
DECLARE
    c RECORD;
BEGIN
    IF to_regclass('team_projects') IS NOT NULL THEN
        FOR c IN SELECT * FROM (VALUES
            ('team_projects', 'kind', 'TEXT NOT NULL DEFAULT ''team'''),
            ('team_projects', 'household_policy', 'JSONB NOT NULL DEFAULT ''{}''::jsonb'),
            ('team_invites', 'kind', 'TEXT NOT NULL DEFAULT ''team'''),
            ('team_invites', 'profile', 'TEXT NOT NULL DEFAULT ''adult''')) AS v(tbl, name, def) LOOP
            CONTINUE WHEN to_regclass(c.tbl) IS NULL;
            IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(c.tbl) AND attname = c.name
                           AND NOT attisdropped) THEN
                EXECUTE format('ALTER TABLE %I ADD COLUMN %I %s', c.tbl, c.name, c.def);
            END IF;
        END LOOP;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'team_projects_kind_check') THEN
            ALTER TABLE team_projects ADD CONSTRAINT team_projects_kind_check CHECK (kind IN ('team', 'household'));
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'team_invites_profile_check') THEN
            ALTER TABLE team_invites ADD CONSTRAINT team_invites_profile_check
                CHECK (profile IN ('adult', 'teen', 'child') AND (kind = 'household' OR profile = 'adult'));
        END IF;
    END IF;
END
$$;

-- ─────────────────────────────── 2. membres du foyer ───────────────────────────────
CREATE TABLE IF NOT EXISTS household_members (
    household_id  TEXT NOT NULL,                       -- pj_… du projet `household`
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),   -- locataire du FOYER (le payeur)
    account_id    TEXT NOT NULL,
    email         TEXT NOT NULL DEFAULT '',
    payer         BOOLEAN NOT NULL DEFAULT false,
    profile       TEXT NOT NULL DEFAULT 'adult' CHECK (profile IN ('adult', 'teen', 'child')),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    cap_day       INTEGER CHECK (cap_day IS NULL OR cap_day >= 0),   -- tours par jour ; NULL = défaut ou aucun
    cap_custom    BOOLEAN NOT NULL DEFAULT false,      -- posé par le payeur (sinon : défaut de la configuration)
    added_at      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (household_id, account_id),
    CHECK (NOT (payer AND profile <> 'adult'))        -- le payeur est le représentant : jamais un profil mineur
);
CREATE UNIQUE INDEX IF NOT EXISTS household_members_one_per_account ON household_members (account_id);

-- 6 membres au plus, payeur compris (décision produit du 25.10) : la configuration peut baisser la borne, jamais la
-- dépasser. Compté sous le verrou du foyer, avec les lignes que voit l'écrivain (payeur : son locataire ; invité : la
-- politique `household_read`, ouverte par son adhésion `team_access` écrite juste avant dans la même transaction).
CREATE OR REPLACE FUNCTION nokker_household_full() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('household:' || NEW.household_id, 0));
    IF (SELECT count(*) FROM household_members WHERE household_id = NEW.household_id
                                                 AND tenant_id = NEW.tenant_id) >= 6 THEN
        RAISE EXCEPTION 'household_full : 6 membres au plus, payeur compris' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

-- Profil d'une invitation `pending` du foyer adressée au compte courant (l'invité ne choisit pas son profil).
CREATE OR REPLACE FUNCTION nokker_household_invited_profile(p_household text, p_tenant text) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r text;
BEGIN
    SELECT i.profile INTO r FROM team_invites i
     WHERE i.project_id = p_household AND i.tenant_id = p_tenant AND i.kind = 'household' AND i.state = 'pending'
       AND i.expires_at > extract(epoch FROM now())
       AND (i.invitee_account = NULLIF(current_setting('app.account_id', true), '')
            OR (i.email <> '' AND i.email = NULLIF(current_setting('app.account_email', true), '')))
     ORDER BY i.created_at DESC LIMIT 1;
    RETURN r;
END;
$$;

-- ─────────────────────────────── 3. consentement parental ───────────────────────────────
CREATE TABLE IF NOT EXISTS parental_consents (
    id                BIGSERIAL PRIMARY KEY,
    household_id      TEXT NOT NULL,
    tenant_id         TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    guardian_account  TEXT NOT NULL,               -- le payeur, qui atteste être le représentant légal
    minor_email       TEXT NOT NULL,               -- l'adresse du compte du mineur (invitation ou membre déclaré)
    invite_id         TEXT,
    profile           TEXT NOT NULL CHECK (profile IN ('teen', 'child')),
    version_sha       TEXT NOT NULL,               -- sha256 des octets du texte d'attestation affiché
    lang              TEXT NOT NULL DEFAULT 'fr',
    attested          BOOLEAN NOT NULL CHECK (attested),
    granted_at        DOUBLE PRECISION NOT NULL,
    revoked_at        DOUBLE PRECISION,
    revoked_by        TEXT,
    ip                TEXT
);
CREATE INDEX IF NOT EXISTS parental_consents_minor ON parental_consents (household_id, minor_email, id DESC);

-- ─────────────────────────────── 4. profils mineurs (hors RLS) ───────────────────────────────
CREATE TABLE IF NOT EXISTS account_minors (
    account_id        TEXT PRIMARY KEY,
    profile           TEXT NOT NULL CHECK (profile IN ('teen', 'child')),
    household_id      TEXT NOT NULL,
    household_tenant  TEXT NOT NULL,
    guardian_account  TEXT NOT NULL,
    declared_at       DOUBLE PRECISION NOT NULL
);

-- ─────────────────────────────── 5. jamais de consentement d'entraînement pour un mineur ───────────────────────────────
CREATE OR REPLACE FUNCTION nokker_minor_no_training() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.scope = 'training' AND NEW.granted
            AND EXISTS (SELECT 1 FROM account_minors WHERE account_id = NEW.account_id) THEN
        RAISE EXCEPTION 'profil mineur : aucun consentement d''entraînement' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF to_regclass('data_consents') IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'data_consents'::regclass
                            AND tgname = 'data_consents_minor_no_training') THEN
        CREATE TRIGGER data_consents_minor_no_training BEFORE INSERT OR UPDATE ON data_consents
            FOR EACH ROW EXECUTE FUNCTION nokker_minor_no_training();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'household_members'::regclass
                   AND tgname = 'household_members_full') THEN
        CREATE TRIGGER household_members_full BEFORE INSERT ON household_members
            FOR EACH ROW EXECUTE FUNCTION nokker_household_full();
    END IF;
END
$$;

-- ─────────────────────────────── 6. ledger ───────────────────────────────
DO $$
DECLARE
    c TEXT;
BEGIN
    IF to_regclass('ledger_entries') IS NULL THEN
        RETURN;
    END IF;
    FOREACH c IN ARRAY ARRAY['household_id', 'household_tenant', 'account'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'ledger_entries'::regclass AND attname = c
                       AND NOT attisdropped) THEN
            EXECUTE format('ALTER TABLE ledger_entries ADD COLUMN %I TEXT', c);
        END IF;
    END LOOP;
    IF to_regclass('ledger_entries_household_idx') IS NULL THEN
        CREATE INDEX IF NOT EXISTS ledger_entries_household_idx
            ON ledger_entries (household_id, household_tenant, created_at) WHERE household_id IS NOT NULL;
    END IF;
    IF to_regprocedure('nokker_team_role(text, text)') IS NULL THEN
        RETURN;  -- équipe pas encore migrée (023) : les politiques au passage suivant
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'ledger_entries'
                   AND policyname = 'household_spend_read') THEN
        CREATE POLICY household_spend_read ON ledger_entries FOR SELECT
            USING (household_id IS NOT NULL AND nokker_team_role(household_id, household_tenant) IS NOT NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'ledger_entries'
                   AND policyname = 'household_spend_tag') THEN
        CREATE POLICY household_spend_tag ON ledger_entries AS RESTRICTIVE FOR INSERT
            WITH CHECK (household_id IS NULL OR (
                account = NULLIF(current_setting('app.account_id', true), '')
                AND nokker_team_role(household_id, household_tenant) IS NOT NULL));
    END IF;
END
$$;

-- ─────────────────────────────── RLS ───────────────────────────────
-- Même verrou que les tables de l'équipe (023) : une ligne ne change ni de foyer ni de locataire.
CREATE OR REPLACE FUNCTION nokker_household_frozen_keys() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.household_id IS DISTINCT FROM OLD.household_id THEN
        RAISE EXCEPTION 'foyer : une ligne ne change ni de foyer ni de locataire (%)', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    t    TEXT;
    pol  RECORD;
BEGIN
    FOREACH t IN ARRAY ARRAY['household_members', 'parental_consents'] LOOP
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = to_regclass(t)) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = t
                       AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
    IF to_regprocedure('nokker_team_role(text, text)') IS NULL OR to_regclass('team_invites') IS NULL THEN
        RETURN;  -- équipe pas encore migrée : politiques d'équipe au passage suivant (rien ne s'ouvre d'ici là)
    END IF;
    FOREACH t IN ARRAY ARRAY['household_members', 'parental_consents'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(t) AND tgname = t || '_frozen_keys') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW '
                           'EXECUTE FUNCTION nokker_household_frozen_keys()', t || '_frozen_keys', t);
        END IF;
    END LOOP;
    FOR pol IN SELECT * FROM (VALUES
        -- (table, nom, commande, USING, WITH CHECK)
        -- les membres du foyer se voient entre eux (profil, état, plafond : aucun contenu)
        ('household_members', 'household_read', 'SELECT',
         'nokker_team_role(household_id, tenant_id) IS NOT NULL', NULL),
        ('household_members', 'household_self_read', 'SELECT',
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''')', NULL),
        -- entrée : l'invité, à son nom, au profil de SON invitation, jamais payeur, après son adhésion d'équipe
        ('household_members', 'household_join', 'INSERT', NULL,
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''') AND NOT payer '
         'AND nokker_team_role(household_id, tenant_id) = ''member'' '
         'AND profile = nokker_household_invited_profile(household_id, tenant_id) '
         'AND status = ''active'' AND NOT cap_custom'),
        -- sortie : un adulte quitte seul ; un mineur, seulement par l'effacement de son compte (nokker erase)
        ('household_members', 'household_self_leave', 'DELETE',
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''') AND NOT payer '
         'AND (profile = ''adult'' OR NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL)',
         NULL),
        -- effacement du compte d'un mineur : ses attestations partent avec lui (nokker erase)
        -- (un DELETE avec WHERE exige aussi une politique SELECT : la même condition, pendant l'effacement seulement)
        ('parental_consents', 'household_minor_erase_read', 'SELECT',
         'minor_email = NULLIF(current_setting(''app.account_email'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL', NULL),
        ('parental_consents', 'household_minor_erase', 'DELETE',
         'minor_email = NULLIF(current_setting(''app.account_email'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL', NULL)
    ) AS v(tbl, name, cmd, using_expr, check_expr) LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                              AND tablename = pol.tbl AND policyname = pol.name);
        EXECUTE format('CREATE POLICY %I ON %I FOR %s', pol.name, pol.tbl, pol.cmd)
             || CASE WHEN pol.using_expr IS NOT NULL THEN ' USING (' || pol.using_expr || ')' ELSE '' END
             || CASE WHEN pol.check_expr IS NOT NULL THEN ' WITH CHECK (' || pol.check_expr || ')' ELSE '' END;
    END LOOP;
END
$$;
