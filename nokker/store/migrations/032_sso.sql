-- SSO OIDC des équipes (drapeau `sso`, docs/LOKKAL.md §7.24, docs/TENANCY.md §4, nokker/sso/).
--
-- Une **connexion** (`sc_…`) est le fournisseur OIDC d'une organisation, posé par un compte propriétaire dans SON
-- locataire : issuer, client_id, NOM du secret du client (la valeur vit au coffre du noyau, jamais ici), claim des
-- groupes, domaines e-mail prouvés par DNS. Un **lien** (`sso_bindings`) rattache une connexion à un projet du même
-- propriétaire avec la table groupes → rôles (jamais `owner`) et l'option « SSO obligatoire ».
--
-- Sous RLS forcée (`tenant_isolation`, comme le reste) : sso_connections, sso_domains, sso_bindings, sso_managed,
-- sso_audit. Les membres d'un projet (n'importe quel locataire) LISENT le lien de leur projet (politique `team_read`,
-- la même fonction `nokker_team_role` que 023), rien d'autre ; un compte lit et efface ses propres lignes
-- `sso_managed` (effacement d'un compte, nokker/admin/erase.py).
--
-- Hors RLS, par conception (lues avant de connaître le locataire, comme `account_identities` et `sessions`) :
--   - sso_routes        : connexion → locataire (la page publique de connexion ne connaît que `sc_…`) ;
--   - sso_domain_claims : domaine PROUVÉ → connexion (un domaine n'appartient qu'à une connexion) ;
--   - sso_flows         : `state` (empreinte), `nonce`, `code_verifier` d'une connexion en cours, 10 minutes, usage
--                         unique (DELETE … RETURNING) ;
--   - sso_identities    : (issuer, sub) → compte, et la date de la dernière connexion par connexion.
-- Accès seulement par nokker/sso/store.py.
--
-- Garde de plus sur l'équipe (023) : une ligne `owner` dans team_access/team_members n'est permise que pour le
-- créateur du projet (`team_projects.owner_account`). Le SSO, qui écrit dans le locataire du projet, ne peut donc pas
-- fabriquer un propriétaire, même par une erreur de code.
-- Rejouée à chaque démarrage : chaque étape vérifie l'état avant d'agir.

CREATE TABLE IF NOT EXISTS sso_connections (
    connection_id  TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    owner_account  TEXT NOT NULL,
    name           TEXT NOT NULL,
    issuer         TEXT NOT NULL,
    client_id      TEXT NOT NULL,
    secret_name    TEXT NOT NULL,                       -- NOM au coffre du noyau ; la valeur n'est jamais ici
    groups_claim   TEXT NOT NULL DEFAULT 'groups',
    token_auth     TEXT NOT NULL DEFAULT 'client_secret_basic'
                   CHECK (token_auth IN ('client_secret_basic', 'client_secret_post')),
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS sso_connections_owner ON sso_connections (owner_account);

CREATE TABLE IF NOT EXISTS sso_domains (
    connection_id  TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    domain         TEXT NOT NULL,
    token          TEXT NOT NULL,                       -- valeur publique du TXT à poser (pas un secret)
    verified_at    DOUBLE PRECISION,
    created_at     DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (connection_id, domain)
);

CREATE TABLE IF NOT EXISTS sso_bindings (
    project_id     TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    connection_id  TEXT NOT NULL,
    mappings       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{group, role}] ; role ∈ maintainer | member | viewer
    required       BOOLEAN NOT NULL DEFAULT false,
    max_age_s      INTEGER NOT NULL DEFAULT 43200,       -- « SSO obligatoire » : âge maximal de la connexion SSO
    updated_by     TEXT NOT NULL DEFAULT '',
    updated_at     DOUBLE PRECISION NOT NULL,
    CHECK (NOT (mappings @> '[{"role": "owner"}]'::jsonb))
);
CREATE INDEX IF NOT EXISTS sso_bindings_connection ON sso_bindings (connection_id);

-- Adhésions tenues par le SSO : un groupe retiré retire le rôle à la connexion suivante ; une adhésion venue d'une
-- invitation n'est touchée que si un groupe lui donne un rôle (elle devient alors tenue par le SSO).
CREATE TABLE IF NOT EXISTS sso_managed (
    project_id     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id     TEXT NOT NULL,
    connection_id  TEXT NOT NULL,
    since          DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, account_id)
);
CREATE INDEX IF NOT EXISTS sso_managed_account ON sso_managed (account_id);

-- Journal des connexions SSO et des changements de rôle : ni jeton, ni code, ni secret (nokker/sso/service.py).
CREATE TABLE IF NOT EXISTS sso_audit (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    connection_id  TEXT NOT NULL,
    at             DOUBLE PRECISION NOT NULL,
    event          TEXT NOT NULL,
    actor          TEXT NOT NULL DEFAULT '',             -- compte concerné (`us_…`) ou propriétaire qui configure
    email          TEXT NOT NULL DEFAULT '',
    project_id     TEXT NOT NULL DEFAULT '',
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS sso_audit_connection ON sso_audit (connection_id, at);

CREATE TABLE IF NOT EXISTS sso_routes (
    connection_id  TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sso_domain_claims (
    domain         TEXT PRIMARY KEY,                    -- un domaine prouvé n'appartient qu'à UNE connexion
    connection_id  TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    claimed_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS sso_domain_claims_connection ON sso_domain_claims (connection_id);

CREATE TABLE IF NOT EXISTS sso_flows (
    state_sha256     TEXT PRIMARY KEY,                  -- empreinte du `state` : le `state` lui-même n'est pas gardé
    connection_id    TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    nonce            TEXT NOT NULL,
    code_verifier    TEXT NOT NULL,
    public_key_spki  TEXT NOT NULL,                     -- clé de l'appareil du navigateur qui a lancé la connexion
    device_name      TEXT NOT NULL DEFAULT '',
    lang             TEXT NOT NULL DEFAULT 'fr',
    created_at       DOUBLE PRECISION NOT NULL,
    expires_at       DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS sso_flows_expires ON sso_flows (expires_at);

CREATE TABLE IF NOT EXISTS sso_identities (
    issuer         TEXT NOT NULL,
    sub            TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    connection_id  TEXT NOT NULL,                       -- dernière connexion utilisée
    email          TEXT NOT NULL DEFAULT '',
    created_at     DOUBLE PRECISION NOT NULL,
    last_login_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (issuer, sub)
);
CREATE INDEX IF NOT EXISTS sso_identities_account ON sso_identities (account_id);

-- Un propriétaire, c'est le créateur du projet, et personne d'autre.
CREATE OR REPLACE FUNCTION nokker_team_owner_is_creator() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role = 'owner' AND NOT EXISTS (
        SELECT 1 FROM team_projects p
         WHERE p.project_id = NEW.project_id AND p.tenant_id = NEW.tenant_id AND p.owner_account = NEW.account_id) THEN
        RAISE EXCEPTION 'équipe : le rôle owner est celui du créateur du projet (%)', TG_TABLE_NAME
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
    FOREACH t IN ARRAY ARRAY['team_access', 'team_members'] LOOP
        CONTINUE WHEN to_regclass(t) IS NULL;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(t) AND tgname = t || '_owner_is_creator') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON %I FOR EACH ROW '
                           'EXECUTE FUNCTION nokker_team_owner_is_creator()', t || '_owner_is_creator', t);
        END IF;
    END LOOP;

    FOREACH t IN ARRAY ARRAY['sso_connections', 'sso_domains', 'sso_bindings', 'sso_managed', 'sso_audit'] LOOP
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = to_regclass(t)) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
    END LOOP;

    FOR pol IN SELECT * FROM (VALUES
        ('sso_connections', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('sso_domains', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('sso_bindings', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        ('sso_bindings', 'team_read', 'SELECT', 'nokker_team_role(project_id, tenant_id) IS NOT NULL', NULL),
        ('sso_managed', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)'),
        -- un DELETE avec WHERE demande aussi une politique SELECT : le compte voit ses propres lignes, rien d'autre
        ('sso_managed', 'sso_self_read', 'SELECT',
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''')', NULL),
        ('sso_managed', 'sso_self_erase', 'DELETE',
         'account_id = NULLIF(current_setting(''app.account_id'', true), '''')', NULL),
        ('sso_audit', 'tenant_isolation', 'ALL', 'tenant_id = current_setting(''app.tenant_id'', true)',
         'tenant_id = current_setting(''app.tenant_id'', true)')
    ) AS v(tbl, name, cmd, using_expr, check_expr) LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                              AND tablename = pol.tbl AND policyname = pol.name);
        EXECUTE format('CREATE POLICY %I ON %I FOR %s', pol.name, pol.tbl, pol.cmd)
             || CASE WHEN pol.using_expr IS NOT NULL THEN ' USING (' || pol.using_expr || ')' ELSE '' END
             || CASE WHEN pol.check_expr IS NOT NULL THEN ' WITH CHECK (' || pol.check_expr || ')' ELSE '' END;
    END LOOP;
END
$$;
