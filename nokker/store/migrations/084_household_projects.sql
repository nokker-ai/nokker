-- Projets du foyer (lot F2, drapeau `family_projects`, docs/FAMILY-PROJECTS.md, nokker/household/projects/). Rejouée à
-- chaque démarrage (idempotente). Numéro 084 (renuméroté à l'intégration r48 : 083 = wall_screens V7, 085 réservé à
-- account_persona V6, r48.1). Ne dépend d'aucune autre table ; n'utilise que `app.tenant_id`.
--
-- Quatre tables de contenu de locataire : tenant_id par défaut = app.tenant_id, RLS activée ET forcée, politique
-- `tenant_isolation` (docs/TENANCY.md §4). Aucune politique croisée :
--   - brouillons : locataire de l'AUTEUR (personne d'autre ne les lit) ;
--   - projets, tâches, réglages : locataire du FOYER (celui du payeur) ; un membre y entre par le service, qui vérifie
--     ses droits à chaque appel (profil mineur : ses tâches seulement, aucun projet réservé aux parents).
-- L'effacement du compte (`nokker erase`) les couvre par tenant_id (découverte par information_schema).
-- Aucun montant n'est payé : `budget` est indicatif ({amount, currency}).
CREATE TABLE IF NOT EXISTS household_project_drafts (
    draft_id      TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id    TEXT NOT NULL,
    household_id  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('create', 'replan')),
    created_at    DOUBLE PRECISION NOT NULL,
    updated_at    DOUBLE PRECISION NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL CHECK (status IN ('open', 'committed')),
    project_id    TEXT NOT NULL DEFAULT '',
    header_raw    JSONB NOT NULL DEFAULT '{}'::jsonb,
    lines         JSONB NOT NULL DEFAULT '[]'::jsonb,
    result        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS household_project_drafts_account_idx
    ON household_project_drafts (tenant_id, account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS household_projects (
    project_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    household_id  TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    title         TEXT NOT NULL,
    deadline      TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    updated_at    DOUBLE PRECISION NOT NULL,
    goal          TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'autre',
    members       JSONB NOT NULL DEFAULT '[]'::jsonb,
    visibility    TEXT NOT NULL CHECK (visibility IN ('household', 'parents')),
    budget        JSONB,
    milestones    JSONB NOT NULL DEFAULT '[]'::jsonb,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done', 'archived'))
);
CREATE INDEX IF NOT EXISTS household_projects_household_idx ON household_projects (tenant_id, household_id, deadline);

CREATE TABLE IF NOT EXISTS household_tasks (
    task_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    household_id    TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    title           TEXT NOT NULL,
    assignee        TEXT NOT NULL DEFAULT '',
    due             TEXT NOT NULL DEFAULT '',
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL,
    milestone       TEXT NOT NULL DEFAULT '',
    document_id     TEXT NOT NULL DEFAULT '',
    document_title  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL CHECK (status IN ('todo', 'done')),
    done_at         DOUBLE PRECISION,
    done_by         TEXT NOT NULL DEFAULT '',
    nudged_at       DOUBLE PRECISION,
    nudged_by       TEXT NOT NULL DEFAULT '',
    nudge_text      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS household_tasks_household_idx ON household_tasks (tenant_id, household_id, due);
CREATE INDEX IF NOT EXISTS household_tasks_assignee_idx ON household_tasks (tenant_id, household_id, assignee);

CREATE TABLE IF NOT EXISTS household_project_settings (
    tenant_id          TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    household_id       TEXT NOT NULL,
    overdue_by_member  BOOLEAN NOT NULL DEFAULT false,
    -- mur F1 (GET /v1/glance/household) : mode invité jusqu'à (epoch, 0 = éteint), articles de courses cochés depuis
    -- le mur (ids `sh_…`), entités Home Assistant choisies pour le mur
    guest_until        DOUBLE PRECISION NOT NULL DEFAULT 0,
    shopping_done      JSONB NOT NULL DEFAULT '[]'::jsonb,
    wall_ha            JSONB NOT NULL DEFAULT '[]'::jsonb,
    wall_place         JSONB,  -- {label, lat, lon, stops: [id]} : météo et départs du mur (pack Essentiels)
    PRIMARY KEY (tenant_id, household_id)
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['household_project_drafts', 'household_projects', 'household_tasks',
                             'household_project_settings'] LOOP
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

-- Effacement d'un compte (`nokker erase`, nokker/admin/erase.py `_CROSS_SCRUB`) : ses références dans le foyer d'un
-- AUTRE locataire (assigné, relance, coche, membre, auteur) sont retirées, comme à sa sortie du foyer (`member_gone`) ;
-- les tâches et projets du foyer restent. Ouvert SEULEMENT pendant la transaction d'effacement
-- (`nokker.erasing_tenant` posé) et pour ce compte (`app.account_id`) ; un déclencheur refuse, hors du locataire
-- courant, toute autre modification que ces colonnes.
CREATE OR REPLACE FUNCTION nokker_household_erase_only() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE allowed TEXT[];
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '') THEN
        allowed := CASE TG_TABLE_NAME
            WHEN 'household_tasks' THEN ARRAY['assignee', 'nudged_at', 'nudged_by', 'nudge_text', 'done_by', 'updated_at']
            ELSE ARRAY['members', 'created_by', 'updated_at'] END;
        IF (to_jsonb(NEW) - allowed) IS DISTINCT FROM (to_jsonb(OLD) - allowed) THEN
            RAISE EXCEPTION '%: hors de son locataire, seul l''effacement d''un compte retire ses références', TG_TABLE_NAME;
        END IF;
        -- La ligne nettoyée ne cite plus le compte : sans ce repère (transaction seulement), la vérification de la
        -- NOUVELLE ligne par les politiques SELECT la refuserait. Il ne désigne que la ligne en cours.
        PERFORM set_config('nokker.household_scrub_id', TG_TABLE_NAME || ':'
                           || COALESCE(to_jsonb(NEW) ->> 'task_id', to_jsonb(NEW) ->> 'project_id'), true);
    END IF;
    RETURN NEW;
END $f$;

DO $$
DECLARE t TEXT;
DECLARE pol RECORD;
BEGIN
    FOREACH t IN ARRAY ARRAY['household_tasks', 'household_projects'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(t) AND tgname = t || '_erase_only') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION nokker_household_erase_only()',
                           t || '_erase_only', t);
        END IF;
    END LOOP;
    FOR pol IN SELECT * FROM (VALUES
        ('household_tasks', 'household_task_erase_read', 'SELECT',
         'NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL '
         'AND (NULLIF(current_setting(''app.account_id'', true), '''') IN (assignee, nudged_by, done_by) '
         'OR ''household_tasks:'' || task_id = current_setting(''nokker.household_scrub_id'', true))', NULL),
        ('household_tasks', 'household_task_erase', 'UPDATE',
         'NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL '
         'AND NULLIF(current_setting(''app.account_id'', true), '''') IN (assignee, nudged_by, done_by)',
         'NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL '
         'AND NULLIF(current_setting(''app.account_id'', true), '''') NOT IN (assignee, nudged_by, done_by)'),
        ('household_projects', 'household_project_erase_read', 'SELECT',
         'NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL '
         'AND (members ? NULLIF(current_setting(''app.account_id'', true), '''') '
         'OR created_by = NULLIF(current_setting(''app.account_id'', true), '''') '
         'OR ''household_projects:'' || project_id = current_setting(''nokker.household_scrub_id'', true))', NULL),
        ('household_projects', 'household_project_erase', 'UPDATE',
         'NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL '
         'AND (members ? NULLIF(current_setting(''app.account_id'', true), '''') '
         'OR created_by = NULLIF(current_setting(''app.account_id'', true), ''''))',
         'NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL '
         'AND NOT (members ? NULLIF(current_setting(''app.account_id'', true), '''')) '
         'AND created_by IS DISTINCT FROM NULLIF(current_setting(''app.account_id'', true), '''')')
    ) AS v(tbl, name, cmd, using_expr, check_expr) LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                              AND tablename = pol.tbl AND policyname = pol.name);
        EXECUTE format('CREATE POLICY %I ON %I FOR %s', pol.name, pol.tbl, pol.cmd)
             || CASE WHEN pol.using_expr IS NOT NULL THEN ' USING (' || pol.using_expr || ')' ELSE '' END
             || CASE WHEN pol.check_expr IS NOT NULL THEN ' WITH CHECK (' || pol.check_expr || ')' ELSE '' END;
    END LOOP;
END $$;
