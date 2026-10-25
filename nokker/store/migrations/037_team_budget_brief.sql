-- Équipe, suite (docs/LOKKAL.md §7.15, §7.18, §7.23, §7.27) : budget de projet décompté sur le ledger, journal du
-- projet, restauration d'une note en quarantaine, livraison du brief du matin hors de l'app.
--
-- 1. `ledger_entries` porte le projet d'un appel (`project_id`, `project_tenant` = locataire du PROJET, `account` = le
--    compte qui a dépensé). Pas de second compteur : le budget se lit dans le ledger. La ligne reste dans le locataire
--    du compte (tenant_isolation) ; deux politiques de plus :
--      - `team_spend_read` (SELECT) : un MEMBRE du projet lit les lignes attribuées à CE projet, et aucune autre ligne
--        d'un autre locataire (`nokker_team_role` de 023, compte = `app.account_id`) ;
--      - `team_spend_tag` (RESTRICTIVE, INSERT) : une ligne ne s'attribue à un projet que par un compte qui y est
--        `member` au moins, à son propre nom. Sans cela, une erreur de code ferait passer les dépenses d'un locataire
--        dans la vue d'un projet qui n'est pas le sien.
-- 2. `team_audit` : journal du projet (budget posé, note restaurée, agent désarmé par l'owner), locataire du projet ;
--    lu et écrit par owner et maintainer seulement, jamais modifié.
-- 3. `team_note_quarantine` garde aussi origine, dérivation et liens de la version, et sa restauration
--    (`restored_at`, `restored_by`) ; un maintainer d'un autre locataire peut marquer une ligne restaurée.
-- 4. `nokker_brief_delivery` : choix de livraison du brief par personne (Telegram, e-mail), armement de l'e-mail par
--    carte forte, état de la dernière livraison (jamais son contenu). RLS par locataire.
--
-- Rejouée à chaque démarrage (et par le ledger, avant l'équipe) : chaque étape vérifie l'état avant d'agir ; une
-- table absente ou la fonction de 023 absente (équipe pas encore migrée) : l'étape attend le passage suivant.

-- ─────────────────────────────── 1. ledger ───────────────────────────────
DO $$
DECLARE
    c TEXT;
BEGIN
    IF to_regclass('ledger_entries') IS NULL THEN
        RETURN;
    END IF;
    FOREACH c IN ARRAY ARRAY['project_id', 'project_tenant', 'account'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'ledger_entries'::regclass AND attname = c
                       AND NOT attisdropped) THEN
            EXECUTE format('ALTER TABLE ledger_entries ADD COLUMN %I TEXT', c);
        END IF;
    END LOOP;
    IF to_regclass('ledger_entries_project_idx') IS NULL THEN
        CREATE INDEX IF NOT EXISTS ledger_entries_project_idx ON ledger_entries (project_id, project_tenant, created_at)
            WHERE project_id IS NOT NULL;
    END IF;
    IF to_regprocedure('nokker_team_role(text, text)') IS NULL THEN
        RETURN;  -- équipe pas encore migrée (023) : les politiques au passage suivant
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'ledger_entries'
                   AND policyname = 'team_spend_read') THEN
        CREATE POLICY team_spend_read ON ledger_entries FOR SELECT
            USING (project_id IS NOT NULL AND nokker_team_role(project_id, project_tenant) IS NOT NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'ledger_entries'
                   AND policyname = 'team_spend_tag') THEN
        CREATE POLICY team_spend_tag ON ledger_entries AS RESTRICTIVE FOR INSERT
            WITH CHECK (project_id IS NULL OR (
                account = NULLIF(current_setting('app.account_id', true), '')
                AND nokker_team_role(project_id, project_tenant) IN ('owner', 'maintainer', 'member')));
    END IF;
END
$$;

-- ─────────────────────────────── 2. journal du projet ───────────────────────────────
CREATE TABLE IF NOT EXISTS team_audit (
    id             BIGSERIAL PRIMARY KEY,
    project_id     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    at             DOUBLE PRECISION NOT NULL,
    actor_account  TEXT NOT NULL,
    event          TEXT NOT NULL,                        -- budget_set | budget_removed | note_restored | agent_disarmed
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb    -- jamais un texte de note, un secret, une entrée d'agent
);
CREATE INDEX IF NOT EXISTS team_audit_project ON team_audit (project_id, at DESC);

-- ─────────────────────────────── 3. quarantaine ───────────────────────────────
DO $$
DECLARE
    c   RECORD;
BEGIN
    IF to_regclass('team_note_quarantine') IS NULL THEN
        RETURN;
    END IF;
    FOR c IN SELECT * FROM (VALUES
        ('origin', 'TEXT NOT NULL DEFAULT '''''), ('derived_from', 'TEXT NOT NULL DEFAULT '''''),
        ('links', 'JSONB NOT NULL DEFAULT ''[]''::jsonb'), ('restored_at', 'DOUBLE PRECISION'),
        ('restored_by', 'TEXT')) AS v(name, def) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'team_note_quarantine'::regclass
                       AND attname = c.name AND NOT attisdropped) THEN
            EXECUTE format('ALTER TABLE team_note_quarantine ADD COLUMN %I %s', c.name, c.def);
        END IF;
    END LOOP;
END
$$;

-- ─────────────────────────────── 4. livraison du brief ───────────────────────────────
CREATE TABLE IF NOT EXISTS nokker_brief_delivery (
    owner            TEXT NOT NULL,
    tenant_id        TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    telegram         BOOLEAN NOT NULL DEFAULT false,
    email            TEXT,                -- adresse choisie ; NULL = pas d'e-mail
    email_armed      TEXT,                -- empreinte (personne, adresse, boîte d'envoi) signée par la carte forte
    arm_approval_id  TEXT,                -- carte forte en attente
    armed_by         TEXT,
    armed_at         DOUBLE PRECISION,
    last             JSONB,               -- {at, telegram, email} : états de la dernière livraison, jamais le texte
    updated_at       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, owner)
);

-- ─────────────────────────────── RLS ───────────────────────────────
DO $$
DECLARE
    t    TEXT;
    pol  RECORD;
BEGIN
    FOREACH t IN ARRAY ARRAY['team_audit', 'nokker_brief_delivery'] LOOP
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
    IF to_regprocedure('nokker_team_role(text, text)') IS NULL THEN
        RETURN;
    END IF;
    IF to_regprocedure('nokker_team_frozen_keys()') IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'team_audit'::regclass
                            AND tgname = 'team_audit_frozen_keys') THEN
        CREATE TRIGGER team_audit_frozen_keys BEFORE UPDATE ON team_audit
            FOR EACH ROW EXECUTE FUNCTION nokker_team_frozen_keys();
    END IF;
    FOR pol IN SELECT * FROM (VALUES
        ('team_audit', 'team_read', 'SELECT',
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')', NULL),
        ('team_audit', 'team_insert', 'INSERT', NULL,
         'actor_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')'),
        ('team_note_quarantine', 'team_update', 'UPDATE',
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')',
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')')
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
