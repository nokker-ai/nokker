-- Nokker — plusieurs boîtes mail et agendas par compte, partage contrôlé dans le foyer (docs/CONNECTORS-MULTI.md).
-- Idempotente : rejouée à chaque démarrage, chaque étape vérifie l'état avant d'agir.
--
-- 1. `connector_credentials` (005) : plusieurs connecteurs par (compte, nature). Chaque ligne gagne un `id` stable
--    (`mb_…` mail, `ca_…` agenda), un `label` et `is_default` (un seul par compte et par nature). La clé devient
--    (account_id, id). Une ligne existante devient le connecteur par défaut, libellée par son adresse (mail) ou
--    « Agenda ». RLS inchangée (006) : AUCUNE politique ajoutée ici, le secret ne s'ouvre à personne d'autre.
-- 2. `connector_grants` : un accès (`level`, actif) ou une demande (`pending_level`, carte forte en attente) accordé par le PROPRIÉTAIRE d'un connecteur à un autre membre de son foyer. La ligne
--    vit dans le locataire du propriétaire (tenant_isolation) ; le bénéficiaire la LIT (jamais le secret) tant que
--    les deux comptes sont membres du même foyer (politique `grant_grantee_read`, nokker_actor() de 059). Le noyau
--    vérifie à nouveau, puis construit la boîte dans le locataire du propriétaire.
-- 3. `connector_grant_events` : journal, ajout seul, locataire du propriétaire ; jamais d'adresse ni de secret.

ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS id TEXT;
ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false;

DO $$
DECLARE
    r record;
BEGIN
    -- lignes d'avant 068 : un id, un libellé, et le défaut de leur nature (il n'y en avait qu'une)
    FOR r IN SELECT ctid, account_id, kind, settings FROM connector_credentials WHERE id IS NULL LOOP
        UPDATE connector_credentials
           SET id = CASE WHEN r.kind = 'mail' THEN 'mb_' ELSE 'ca_' END
                    || substr(md5(random()::text || clock_timestamp()::text || r.account_id), 1, 10),
               label = COALESCE(NULLIF(label, ''), CASE WHEN r.kind = 'mail'
                                THEN COALESCE(NULLIF(r.settings->>'address', ''), 'Mail') ELSE 'Agenda' END),
               is_default = true
         WHERE ctid = r.ctid;
    END LOOP;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'connector_credentials'
               AND column_name = 'id' AND is_nullable = 'YES') THEN
        ALTER TABLE connector_credentials ALTER COLUMN id SET NOT NULL;
        ALTER TABLE connector_credentials ALTER COLUMN label SET NOT NULL;
    END IF;
    -- clé (account_id, kind) → (account_id, id)
    IF EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
               WHERE c.conrelid = 'connector_credentials'::regclass AND c.contype = 'p' AND a.attname = 'kind') THEN
        ALTER TABLE connector_credentials DROP CONSTRAINT connector_credentials_pkey;
        ALTER TABLE connector_credentials ADD CONSTRAINT connector_credentials_pkey PRIMARY KEY (account_id, id);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS connector_credentials_one_default
    ON connector_credentials (account_id, kind) WHERE is_default;
CREATE UNIQUE INDEX IF NOT EXISTS connector_credentials_label
    ON connector_credentials (account_id, kind, lower(label));
CREATE UNIQUE INDEX IF NOT EXISTS connector_credentials_id ON connector_credentials (id);

CREATE TABLE IF NOT EXISTS connector_grants (
    tenant_id         TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),   -- locataire du PROPRIÉTAIRE
    connector_id      TEXT NOT NULL,
    owner_account     TEXT NOT NULL,
    grantee_account   TEXT NOT NULL,
    household_id      TEXT NOT NULL,
    household_tenant  TEXT NOT NULL,                                            -- locataire du foyer (payeur)
    kind              TEXT NOT NULL CHECK (kind IN ('mail', 'calendar')),
    level             TEXT CHECK (level IN ('read', 'send', 'write')),          -- accès ACTIF ; NULL = rien encore
    granted_at        TIMESTAMPTZ,
    pending_level     TEXT CHECK (pending_level IN ('read', 'send', 'write')),  -- en attente de la carte du propriétaire
    approval_id       TEXT,                                                     -- carte en attente
    PRIMARY KEY (connector_id, grantee_account),
    CHECK (owner_account <> grantee_account),
    CHECK (level IS NOT NULL OR pending_level IS NOT NULL),
    CHECK ((kind = 'mail' AND COALESCE(level, 'read') IN ('read', 'send') AND COALESCE(pending_level, 'read') IN ('read', 'send'))
        OR (kind = 'calendar' AND COALESCE(level, 'read') IN ('read', 'write') AND COALESCE(pending_level, 'read') IN ('read', 'write')))
);
CREATE INDEX IF NOT EXISTS connector_grants_grantee ON connector_grants (grantee_account);

CREATE TABLE IF NOT EXISTS connector_grant_events (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    connector_id      TEXT NOT NULL,
    owner_account     TEXT NOT NULL,
    grantee_account   TEXT NOT NULL,
    actor_account     TEXT NOT NULL,
    event             TEXT NOT NULL CHECK (event IN ('granted', 'revoked', 'dropped')),
    level             TEXT,
    reason            TEXT NOT NULL DEFAULT '',
    at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION nokker_grant_events_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.tenant_id = current_setting('nokker.erasing_tenant', true) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'connector_grant_events est append-only : UPDATE/DELETE interdits';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['connector_grants', 'connector_grant_events'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(t) AND tgname = t || '_tenant_immutable') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION nokker_tenant_immutable()',
                           t || '_tenant_immutable', t);
        END IF;
    END LOOP;
    -- le bénéficiaire lit SA ligne de partage, tant que lui ET le propriétaire sont membres du foyer de la ligne
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'connector_grants' AND policyname = 'grant_grantee_read') THEN
        CREATE POLICY grant_grantee_read ON connector_grants FOR SELECT USING (
            grantee_account = nokker_actor() AND level IS NOT NULL
            AND nokker_team_role(household_id, household_tenant) IS NOT NULL
            AND EXISTS (SELECT 1 FROM household_members m WHERE m.household_id = connector_grants.household_id
                        AND m.tenant_id = connector_grants.household_tenant
                        AND m.account_id = connector_grants.owner_account));
    END IF;
    -- journal : ajout seul (trigger, comme `turns` en 013) : ni UPDATE ni DELETE, sauf effacement du locataire
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'connector_grant_events'::regclass
                   AND tgname = 'connector_grant_events_append_only') THEN
        CREATE TRIGGER connector_grant_events_append_only BEFORE UPDATE OR DELETE ON connector_grant_events
            FOR EACH ROW EXECUTE FUNCTION nokker_grant_events_append_only();
    END IF;
END $$;
