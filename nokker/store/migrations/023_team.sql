-- Équipe (lot X17, docs/LOKKAL.md §7.15, docs/TENANCY.md §4) : projets partagés entre comptes nokker.ai.
--
-- Un projet vit dans le locataire de son propriétaire : chaque ligne porte `tenant_id` = locataire du PROJET. RLS
-- activée ET forcée, avec la politique commune `tenant_isolation` (le locataire du projet voit et écrit tout), plus
-- des politiques d'équipe qui ouvrent les lignes d'un projet à un compte MEMBRE d'un autre locataire, sans jamais le
-- faire entrer dans le locataire du propriétaire :
--   - `app.account_id` / `app.account_email` : le compte qui agit, posés par nokker/team/store.py à côté de
--     `app.tenant_id` (vide ou absent : aucune politique d'équipe ne s'ouvre, NULLIF) ;
--   - `team_access` : l'index des adhésions ; un compte y lit SES lignes (n'importe quel locataire), rien d'autre ;
--   - `nokker_team_role(projet, locataire)` : le rôle du compte dans ce projet, lu dans `team_access` (plpgsql : jamais
--     déplié par le planificateur, donc pas de récursion de politique) ;
--   - lecture d'un projet : tout membre ; écriture : selon le rôle (viewer n'écrit rien, en base aussi) ;
--   - entrée dans un projet : seulement contre une invitation `pending`, non expirée, adressée à ce compte, au rôle
--     de l'invitation (jamais `owner`) ; sortie : un membre retire sa propre ligne (pas le propriétaire) ;
--   - effacement d'un compte (nokker/admin/erase.py) : ses adhésions et les invitations qui lui sont adressées partent
--     avec lui, même dans le locataire d'un autre (le compte retire ses propres lignes, rien d'autre).
-- Rejouée à chaque démarrage : chaque étape vérifie l'état avant d'agir (aucun ALTER une fois en place).

CREATE TABLE IF NOT EXISTS team_projects (
    project_id     TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    owner_account  TEXT NOT NULL,
    repos          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- clés `hôte/chemin` (mémoire de projet X1)
    secret_names   JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{name, description}] : des NOMS, jamais de valeur
    budget         JSONB,                                -- {monthly, currency} | null
    rules          JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {mode?, ask, deny} : restreindre seulement
    mirror         BOOLEAN NOT NULL DEFAULT false,       -- notes de projet en miroir pour les membres (§6.3)
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS team_projects_owner ON team_projects (owner_account);

-- Index des adhésions (lu par les politiques) et liste des membres (affichée aux membres) : deux tables, écrites
-- ensemble, pour qu'aucune politique ne lise sa propre table.
CREATE TABLE IF NOT EXISTS team_access (
    project_id  TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('owner', 'maintainer', 'member', 'viewer')),
    email       TEXT NOT NULL DEFAULT '',
    added_by    TEXT NOT NULL DEFAULT '',
    added_at    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, account_id)
);
CREATE INDEX IF NOT EXISTS team_access_account ON team_access (account_id);

CREATE TABLE IF NOT EXISTS team_members (
    project_id  TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('owner', 'maintainer', 'member', 'viewer')),
    email       TEXT NOT NULL DEFAULT '',
    added_by    TEXT NOT NULL DEFAULT '',
    added_at    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, account_id)
);

CREATE TABLE IF NOT EXISTS team_invites (
    invite_id        TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    tenant_id        TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    project_name     TEXT NOT NULL DEFAULT '',
    email            TEXT NOT NULL DEFAULT '',
    invitee_account  TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('maintainer', 'member', 'viewer')),
    invited_by       TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'accepted', 'declined', 'revoked')),
    created_at       DOUBLE PRECISION NOT NULL,
    expires_at       DOUBLE PRECISION NOT NULL,
    decided_at       DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS team_invites_project ON team_invites (project_id);
CREATE INDEX IF NOT EXISTS team_invites_invitee ON team_invites (invitee_account) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS team_invites_email ON team_invites (email) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS team_skills (
    project_id      TEXT NOT NULL,
    tenant_id       TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    name            TEXT NOT NULL,
    source          TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    requires_local  BOOLEAN NOT NULL DEFAULT false,
    added_by        TEXT NOT NULL DEFAULT '',
    added_at        DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, name)
);

CREATE SEQUENCE IF NOT EXISTS team_notes_seq;
CREATE TABLE IF NOT EXISTS team_notes (
    project_id      TEXT NOT NULL,
    tenant_id       TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'fact' CHECK (kind IN ('rule', 'feedback', 'fact')),
    title           TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    sha             TEXT NOT NULL DEFAULT '',
    updated_at      DOUBLE PRECISION NOT NULL,
    author_account  TEXT NOT NULL DEFAULT '',
    author_machine  TEXT NOT NULL DEFAULT '',
    origin          TEXT NOT NULL DEFAULT '',
    derived_from    TEXT NOT NULL DEFAULT '',
    links           JSONB NOT NULL DEFAULT '[]'::jsonb,
    deleted         BOOLEAN NOT NULL DEFAULT false,
    seq             BIGINT NOT NULL DEFAULT nextval('team_notes_seq'),
    PRIMARY KEY (project_id, name)
);
CREATE INDEX IF NOT EXISTS team_notes_seq_idx ON team_notes (project_id, seq);

-- Conflit du miroir (§6.3) : la plus récente gagne, l'autre version attend ici une relecture.
CREATE TABLE IF NOT EXISTS team_note_quarantine (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT NOT NULL,
    tenant_id       TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    sha             TEXT NOT NULL DEFAULT '',
    updated_at      DOUBLE PRECISION NOT NULL,
    author_account  TEXT NOT NULL DEFAULT '',
    author_machine  TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    quarantined_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS team_note_quarantine_project ON team_note_quarantine (project_id);

-- Rôle du compte courant (`app.account_id`) dans un projet, ou NULL.
CREATE OR REPLACE FUNCTION nokker_team_role(p_project text, p_tenant text) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r text;
BEGIN
    SELECT role INTO r FROM team_access
     WHERE project_id = p_project AND tenant_id = p_tenant
       AND account_id = NULLIF(current_setting('app.account_id', true), '');
    RETURN r;
END;
$$;

-- Une invitation `pending`, non expirée, au rôle donné, adressée au compte courant (par id ou par e-mail).
CREATE OR REPLACE FUNCTION nokker_team_invited(p_project text, p_tenant text, p_role text) RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM team_invites i
         WHERE i.project_id = p_project AND i.tenant_id = p_tenant AND i.role = p_role AND i.state = 'pending'
           AND i.expires_at > extract(epoch FROM now())
           AND (i.invitee_account = NULLIF(current_setting('app.account_id', true), '')
                OR (i.email <> '' AND i.email = NULLIF(current_setting('app.account_email', true), ''))));
END;
$$;

-- Une ligne ne change jamais de projet ni de locataire : les politiques permissives se cumulent (la WITH CHECK
-- `tenant_isolation` d'un membre accepterait SON locataire), d'où ce verrou, sur toutes les tables de l'équipe.
CREATE OR REPLACE FUNCTION nokker_team_frozen_keys() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
        RAISE EXCEPTION 'équipe : une ligne ne change ni de projet ni de locataire (%)', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    t      TEXT;
    pol    RECORD;
BEGIN
    FOREACH t IN ARRAY ARRAY['team_projects', 'team_access', 'team_members', 'team_invites', 'team_skills',
                             'team_notes', 'team_note_quarantine'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(t) AND tgname = t || '_frozen_keys') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION nokker_team_frozen_keys()',
                           t || '_frozen_keys', t);
        END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = to_regclass(t)) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
    END LOOP;

    FOR pol IN SELECT * FROM (VALUES
        -- (table, nom, commande, USING, WITH CHECK)
        ('team_projects', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('team_projects', 'team_read', 'SELECT', 'nokker_team_role(project_id, tenant_id) IS NOT NULL', NULL),
        ('team_projects', 'team_update', 'UPDATE', 'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')',
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')'),

        ('team_access', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('team_access', 'team_self_read', 'SELECT', 'account_id = NULLIF(current_setting(''app.account_id'', true), '''')', NULL),
        ('team_access', 'team_join', 'INSERT', NULL,
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''') AND role <> ''owner'' '
         'AND nokker_team_invited(project_id, tenant_id, role)'),
        ('team_access', 'team_self_leave', 'DELETE',
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''') AND role <> ''owner''', NULL),

        ('team_members', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('team_members', 'team_read', 'SELECT', 'nokker_team_role(project_id, tenant_id) IS NOT NULL', NULL),
        ('team_members', 'team_self_read', 'SELECT', 'account_id = NULLIF(current_setting(''app.account_id'', true), '''')', NULL),
        ('team_members', 'team_join', 'INSERT', NULL,
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''') AND role <> ''owner'' '
         'AND nokker_team_invited(project_id, tenant_id, role)'),
        ('team_members', 'team_self_leave', 'DELETE',
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''') AND role <> ''owner''', NULL),

        ('team_invites', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('team_invites', 'team_invitee_read', 'SELECT',
         '(invitee_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'OR (email <> '''' AND email = NULLIF(current_setting(''app.account_email'', true), '''')))', NULL),
        ('team_invites', 'team_invitee_decide', 'UPDATE',
         'state = ''pending'' AND (invitee_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'OR (email <> '''' AND email = NULLIF(current_setting(''app.account_email'', true), '''')))',
         'state IN (''accepted'', ''declined'') AND (invitee_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'OR (email <> '''' AND email = NULLIF(current_setting(''app.account_email'', true), '''')))'),

        ('team_invites', 'team_invitee_erase', 'DELETE',
         '(invitee_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'OR (email <> '''' AND email = NULLIF(current_setting(''app.account_email'', true), '''')))', NULL),

        ('team_skills', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('team_skills', 'team_read', 'SELECT', 'nokker_team_role(project_id, tenant_id) IS NOT NULL', NULL),
        ('team_skills', 'team_insert', 'INSERT', NULL, 'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')'),
        ('team_skills', 'team_update', 'UPDATE', 'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')',
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')'),
        ('team_skills', 'team_delete', 'DELETE', 'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')', NULL),

        ('team_notes', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('team_notes', 'team_read', 'SELECT', 'nokker_team_role(project_id, tenant_id) IS NOT NULL', NULL),
        ('team_notes', 'team_insert', 'INSERT', NULL,
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'', ''member'')'),
        ('team_notes', 'team_update', 'UPDATE', 'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'', ''member'')',
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'', ''member'')'),
        ('team_notes', 'team_delete', 'DELETE', 'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')', NULL),

        ('team_note_quarantine', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('team_note_quarantine', 'team_read', 'SELECT', 'nokker_team_role(project_id, tenant_id) IS NOT NULL', NULL),
        ('team_note_quarantine', 'team_insert', 'INSERT', NULL,
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'', ''member'')'),
        ('team_note_quarantine', 'team_delete', 'DELETE',
         'nokker_team_role(project_id, tenant_id) IN (''owner'', ''maintainer'')', NULL)
    ) AS v(tbl, name, cmd, using_expr, check_expr) LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                              AND tablename = pol.tbl AND policyname = pol.name);
        EXECUTE format('CREATE POLICY %I ON %I FOR %s', pol.name, pol.tbl, pol.cmd)
             || CASE WHEN pol.using_expr IS NOT NULL THEN ' USING (' || pol.using_expr || ')' ELSE '' END
             || CASE WHEN pol.check_expr IS NOT NULL THEN ' WITH CHECK (' || pol.check_expr || ')' ELSE '' END;
    END LOOP;
END
$$;
