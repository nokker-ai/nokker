-- Nokker — connecteurs OAuth (docs/CONNECTORS-OAUTH.md). Idempotente : rejouée à chaque démarrage.
--
-- 1. `oauth_accounts` : un compte chez un fournisseur (Microsoft, Google…) relié par un profil. Jetons d'accès et de
--    rafraîchissement CHIFFRÉS (Fernet, comme les secrets des connecteurs) ; locataire du profil (tenant_isolation).
--    Les connecteurs (068) qui l'utilisent rangent la référence `oauth:<id>`, jamais un jeton.
-- 2. `oauth_flows` : connexions en cours (10 min, usage unique), ouvertes au callback par l'EMPREINTE du `state`
--    (politique par clé de 058, comme `sso_flows`), écrites dans le locataire du profil.
-- 3. Natures de connecteurs : mail, calendar (068) + contacts, files, tasks, pages.

CREATE TABLE IF NOT EXISTS oauth_accounts (
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    id            TEXT PRIMARY KEY,                       -- oa_ + 10
    account_id    TEXT NOT NULL,                          -- profil propriétaire
    provider      TEXT NOT NULL,
    grant_name    TEXT NOT NULL DEFAULT 'main',           -- `drive` : grant séparé du sélecteur Google (drive.file seul)
    subject       TEXT NOT NULL,                          -- id du compte chez le fournisseur
    email         TEXT NOT NULL DEFAULT '',
    scopes        TEXT[] NOT NULL DEFAULT '{}',
    services      TEXT[] NOT NULL DEFAULT '{}',
    token_enc     BYTEA NOT NULL,
    expires_at    TIMESTAMPTZ,
    state         TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok', 'reconnect')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    refreshed_at  TIMESTAMPTZ,
    UNIQUE (account_id, provider, grant_name, subject)
);

CREATE TABLE IF NOT EXISTS oauth_flows (
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    state_sha256   TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    household_id   TEXT NOT NULL DEFAULT '',
    provider       TEXT NOT NULL,
    grant_name     TEXT NOT NULL DEFAULT 'main',
    services       TEXT[] NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    oauth_account  TEXT NOT NULL DEFAULT '',             -- ajout de services à un compte déjà relié
    code_verifier  TEXT NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL,
    expires_at     DOUBLE PRECISION NOT NULL
);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['oauth_accounts', 'oauth_flows'] LOOP
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
    -- callback : la seule ligne qui porte l'empreinte présentée, lue et supprimée (usage unique), jamais écrite par clé
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'oauth_flows' AND policyname = 'key_lookup_select') THEN
        CREATE POLICY key_lookup_select ON oauth_flows FOR SELECT USING (state_sha256 = nokker_lookup_key());
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'oauth_flows' AND policyname = 'key_lookup_delete') THEN
        CREATE POLICY key_lookup_delete ON oauth_flows FOR DELETE USING (state_sha256 = nokker_lookup_key());
    END IF;
END $$;

-- nouvelles natures (005 et 068 n'acceptaient que mail et calendar)
DO $$
DECLARE
    c record;
BEGIN
    FOR c IN SELECT conname, conrelid::regclass::text AS tbl FROM pg_constraint
             WHERE contype = 'c' AND conrelid IN ('connector_credentials'::regclass, 'connector_grants'::regclass)
               AND pg_get_constraintdef(oid) LIKE '%kind%' AND pg_get_constraintdef(oid) NOT LIKE '%contacts%' LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', c.tbl, c.conname);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connector_credentials_kind_v2') THEN
        ALTER TABLE connector_credentials ADD CONSTRAINT connector_credentials_kind_v2
            CHECK (kind IN ('mail', 'calendar', 'contacts', 'files', 'tasks', 'pages'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connector_grants_kind_v2') THEN
        ALTER TABLE connector_grants ADD CONSTRAINT connector_grants_kind_v2 CHECK (
            kind IN ('mail', 'calendar', 'contacts', 'files', 'tasks', 'pages')
            AND ((kind = 'mail' AND COALESCE(level, 'read') IN ('read', 'send') AND COALESCE(pending_level, 'read') IN ('read', 'send'))
                 OR (kind <> 'mail' AND COALESCE(level, 'read') IN ('read', 'write') AND COALESCE(pending_level, 'read') IN ('read', 'write'))));
    END IF;
END $$;
