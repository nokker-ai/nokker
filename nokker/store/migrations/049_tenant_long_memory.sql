-- Mémoire longue multi-locataire (docs/MEMORY-TENANCY.md, nokker/memory/tenant_long.py, drapeau `tenant_long_memory`).
-- NUMÉROTATION : 049 (060 sur la branche p2/memory-tenancy, renumérotée à l'intégration dans le train). Dépend
-- de 023 (équipe : nokker_team_role), 039 (foyer : household_members, account_minors) et 046 (nokker_memory_pending).
-- Rejouée à chaque démarrage : chaque étape vérifie l'état avant d'agir.
--
-- 1. `nokker_long_memory` : une note durable par ligne, au format des notes CortHeXis (nom, type, titre, texte).
--    - portée `user` : la mémoire de profil d'un compte, dans SON locataire, lue et écrite par lui seul ;
--    - portée `household` : la mémoire commune d'un foyer, dans le locataire du FOYER (celui du payeur), détenue par
--      le payeur (`account_id`), écrite par lui (directement, ou en approuvant la proposition d'un membre), lue par
--      chaque membre ACTIF ; `audience = adults` la retire aux profils mineurs ; `author_account` = qui l'a proposée.
--    UN index pgvector partagé par tous les locataires, filtré par la RLS : colonne `embedding vector` (sans dimension
--    figée : le modèle d'embedding est noté par ligne, `embed_model`), recherche EXACTE dans la partition visible du
--    compte (index btree (tenant_id, account_id) et (tenant_id, household_id)) — pas d'index ANN en bêta : un HNSW
--    global filtré après coup par la RLS perdrait les notes d'un petit locataire (voir la doc, §3).
--    Sans l'extension `vector` dans la base du noyau : la table existe sans `embedding`, le rappel est lexical ; la
--    colonne est ajoutée au passage suivant, dès que l'extension est là (rien d'autre ne change).
-- 2. RLS activée ET forcée :
--    - `tenant_isolation` : le locataire ET le compte (`app.account_id`) — plus strict que la politique commune : un
--      second compte d'un même locataire ne lirait pas la mémoire de profil du premier ;
--    - `long_memory_household_read` : un membre actif du foyer (adhésion d'équipe + ligne `household_members`
--      active), adultes seulement pour `audience = adults` ;
--    - `long_memory_author_delete` : l'auteur retire sa propre contribution au foyer tant qu'il la voit, ou pendant
--      l'effacement de son compte (`nokker.erasing_tenant`) ;
--    - une ligne ne change ni de locataire, ni de compte, ni de portée, ni de foyer, ni d'auteur (trigger).
-- 3. Foyer supprimé : ses notes communes partent avec lui (trigger sur `team_projects`).
-- 4. Quarantaine (046) réutilisée pour les propositions des membres : `nokker_memory_pending` gagne `author_account`
--    et `audience` ; un membre insère une proposition `source = household` DANS LE LOCATAIRE DU FOYER, au nom du
--    payeur (qui la lit et la décide par /v1/memory/pending, inchangé), jamais lue par le membre ensuite ; l'effacement
--    du compte de l'auteur retire ses propositions.

-- ─────────────────────────────── 1. table ───────────────────────────────
CREATE TABLE IF NOT EXISTS nokker_long_memory (
    tenant_id       text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id      text NOT NULL,
    name            text NOT NULL,
    scope           text NOT NULL DEFAULT 'user' CHECK (scope IN ('user', 'household')),
    household_id    text,
    audience        text NOT NULL DEFAULT 'all' CHECK (audience IN ('all', 'adults')),
    kind            text NOT NULL DEFAULT 'fact' CHECK (kind IN ('rule', 'feedback', 'fact')),
    title           text NOT NULL,
    body            text NOT NULL,
    author_account  text NOT NULL,
    origin          text NOT NULL DEFAULT '',
    embed_model     text,
    created_at      double precision NOT NULL,
    updated_at      double precision NOT NULL,
    PRIMARY KEY (tenant_id, account_id, name),
    CONSTRAINT nokker_long_memory_scope_shape CHECK (
        (scope = 'user' AND household_id IS NULL AND audience = 'all' AND author_account = account_id)
        OR (scope = 'household' AND household_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS nokker_long_memory_household ON nokker_long_memory (tenant_id, household_id)
    WHERE scope = 'household';
CREATE INDEX IF NOT EXISTS nokker_long_memory_author ON nokker_long_memory (author_account);

-- pgvector : extension créée si le rôle le peut (sinon par l'administrateur de la base), colonne ajoutée dès qu'elle
-- est là. Jamais une erreur de migration pour autant : le rappel reste lexical en attendant.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
            AND EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN insufficient_privilege OR feature_not_supported THEN
            RAISE NOTICE 'nokker_long_memory : extension vector non créée (droits) — rappel lexical';
        END;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
            AND NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_long_memory'::regclass
                            AND attname = 'embedding' AND NOT attisdropped) THEN
        ALTER TABLE nokker_long_memory ADD COLUMN embedding vector;
    END IF;
END
$$;

-- ─────────────────────────────── 2. visibilité du foyer ───────────────────────────────
-- Membre ACTIF du foyer, au profil qui convient à l'audience. plpgsql : jamais déplié par le planificateur (pas de
-- récursion de politique) ; lit `team_access` (via nokker_team_role) et `household_members` sous la RLS de l'appelant,
-- qui n'y voit que son foyer.
CREATE OR REPLACE FUNCTION nokker_long_memory_visible(p_household text, p_tenant text, p_audience text) RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
DECLARE
    acc   text := NULLIF(current_setting('app.account_id', true), '');
    prof  text;
    st    text;
BEGIN
    IF acc IS NULL OR p_household IS NULL OR p_tenant IS NULL THEN
        RETURN false;
    END IF;
    IF nokker_team_role(p_household, p_tenant) IS NULL THEN
        RETURN false;
    END IF;
    SELECT m.profile, m.status INTO prof, st FROM household_members m
     WHERE m.household_id = p_household AND m.tenant_id = p_tenant AND m.account_id = acc;
    IF NOT FOUND OR st <> 'active' THEN
        RETURN false;
    END IF;
    IF p_audience <> 'all' AND (prof <> 'adult' OR EXISTS (SELECT 1 FROM account_minors WHERE account_id = acc)) THEN
        RETURN false;
    END IF;
    RETURN true;
END;
$$;

-- Payeur d'un foyer (lu par un membre : `household_read` lui ouvre les adhésions de SON foyer).
CREATE OR REPLACE FUNCTION nokker_long_memory_payer(p_household text, p_tenant text) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r text;
BEGIN
    SELECT m.account_id INTO r FROM household_members m
     WHERE m.household_id = p_household AND m.tenant_id = p_tenant AND m.payer;
    RETURN r;
END;
$$;

CREATE OR REPLACE FUNCTION nokker_long_memory_frozen_keys() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.account_id IS DISTINCT FROM OLD.account_id
            OR NEW.scope IS DISTINCT FROM OLD.scope OR NEW.household_id IS DISTINCT FROM OLD.household_id
            OR NEW.author_account IS DISTINCT FROM OLD.author_account THEN
        RAISE EXCEPTION 'mémoire longue : une note ne change ni de locataire, ni de compte, ni de portée, ni de foyer'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

-- Foyer supprimé (par son payeur, ou effacement) : ses notes communes partent avec lui.
CREATE OR REPLACE FUNCTION nokker_long_memory_household_gone() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.kind = 'household' THEN
        DELETE FROM nokker_long_memory WHERE tenant_id = OLD.tenant_id AND household_id = OLD.project_id
                                         AND scope = 'household';
    END IF;
    RETURN OLD;
END;
$$;

-- ─────────────────────────────── 3. quarantaine (046) ───────────────────────────────
DO $$
BEGIN
    IF to_regclass('nokker_memory_pending') IS NOT NULL THEN
        ALTER TABLE nokker_memory_pending ADD COLUMN IF NOT EXISTS author_account text;
        ALTER TABLE nokker_memory_pending ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'all';
        CREATE INDEX IF NOT EXISTS nokker_memory_pending_author ON nokker_memory_pending (author_account)
            WHERE author_account IS NOT NULL;
    END IF;
END
$$;

-- ─────────────────────────────── RLS ───────────────────────────────
DO $$
DECLARE
    pol RECORD;
BEGIN
    IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'nokker_long_memory'::regclass) THEN
        ALTER TABLE nokker_long_memory ENABLE ROW LEVEL SECURITY;
        ALTER TABLE nokker_long_memory FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'nokker_long_memory'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nokker_long_memory
            USING (tenant_id = current_setting('app.tenant_id', true)
                   AND account_id = NULLIF(current_setting('app.account_id', true), ''))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
                        AND account_id = NULLIF(current_setting('app.account_id', true), ''));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'nokker_long_memory'::regclass
                   AND tgname = 'nokker_long_memory_frozen_keys') THEN
        CREATE TRIGGER nokker_long_memory_frozen_keys BEFORE UPDATE ON nokker_long_memory
            FOR EACH ROW EXECUTE FUNCTION nokker_long_memory_frozen_keys();
    END IF;
    IF to_regprocedure('nokker_team_role(text, text)') IS NULL OR to_regclass('household_members') IS NULL THEN
        RETURN;  -- équipe ou foyer pas encore migrés : rien ne s'ouvre au foyer d'ici le passage suivant
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'team_projects'::regclass
                   AND tgname = 'team_projects_long_memory_gone') THEN
        CREATE TRIGGER team_projects_long_memory_gone AFTER DELETE ON team_projects
            FOR EACH ROW EXECUTE FUNCTION nokker_long_memory_household_gone();
    END IF;
    FOR pol IN SELECT * FROM (VALUES
        -- (table, nom, commande, USING, WITH CHECK)
        ('nokker_long_memory', 'long_memory_household_read', 'SELECT',
         'scope = ''household'' AND nokker_long_memory_visible(household_id, tenant_id, audience)', NULL),
        ('nokker_long_memory', 'long_memory_author_delete', 'DELETE',
         'scope = ''household'' AND author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND (nokker_long_memory_visible(household_id, tenant_id, audience) '
         'OR NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL)', NULL),
        -- un DELETE avec WHERE exige aussi une politique SELECT : la même condition, pendant l'effacement seulement
        ('nokker_long_memory', 'long_memory_author_erase_read', 'SELECT',
         'author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL', NULL),
        -- proposition d'un membre au foyer : dans le locataire du foyer, au nom du payeur, à son nom d'auteur
        ('nokker_memory_pending', 'long_memory_household_submit', 'INSERT', NULL,
         'scope = ''household'' AND source = ''household'' AND status = ''pending'' '
         'AND author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND nokker_long_memory_visible(project, tenant_id, ''all'') '
         'AND account_id = nokker_long_memory_payer(project, tenant_id)'),
        ('nokker_memory_pending', 'long_memory_author_erase_read', 'SELECT',
         'author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL', NULL),
        ('nokker_memory_pending', 'long_memory_author_erase', 'DELETE',
         'author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL', NULL)
    ) AS v(tbl, name, cmd, using_expr, check_expr) LOOP
        CONTINUE WHEN to_regclass(pol.tbl) IS NULL;
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                              AND tablename = pol.tbl AND policyname = pol.name);
        EXECUTE format('CREATE POLICY %I ON %I FOR %s', pol.name, pol.tbl, pol.cmd)
             || CASE WHEN pol.using_expr IS NOT NULL THEN ' USING (' || pol.using_expr || ')' ELSE '' END
             || CASE WHEN pol.check_expr IS NOT NULL THEN ' WITH CHECK (' || pol.check_expr || ')' ELSE '' END;
    END LOOP;
END
$$;
