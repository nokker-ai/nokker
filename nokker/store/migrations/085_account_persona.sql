-- Nokker — « Ton Nokker » (V6, docs/AVATAR.md, nokker/persona.py) : le nom, la voix et l'avatar de l'assistant d'un
-- compte, choisis à l'onboarding. Une ligne par compte, valeurs tirées de catalogues fermés (contrôlés par le noyau).
-- Additive. Sous RLS du locataire ; `nokker erase` la couvre (tenant_id), l'export du compte aussi (clé `persona`).

CREATE TABLE IF NOT EXISTS account_persona (
    tenant_id       TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 24),
    voice_id        TEXT NOT NULL CHECK (voice_id ~ '^[a-z0-9][a-z0-9-]{0,39}$'),
    avatar_id       TEXT NOT NULL CHECK (avatar_id ~ '^[a-z0-9][a-z0-9-]{0,39}$'),
    avatar_version  INTEGER NOT NULL CHECK (avatar_version >= 1),
    updated_at      DOUBLE PRECISION NOT NULL
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'account_persona' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE account_persona ENABLE ROW LEVEL SECURITY;
        ALTER TABLE account_persona FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'account_persona' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON account_persona USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('account_persona')
                   AND tgname = 'account_persona_tenant_immutable') THEN
        CREATE TRIGGER account_persona_tenant_immutable BEFORE UPDATE OF tenant_id ON account_persona
            FOR EACH ROW EXECUTE FUNCTION nokker_tenant_immutable();
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON account_persona TO nokker_app;
    END IF;
END $$;
