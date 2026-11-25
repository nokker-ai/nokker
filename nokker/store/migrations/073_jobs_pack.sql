-- Nokker — pack Emploi (docs/JOBS-PACK.md §4.1, §5.3). Idempotente : rejouée à chaque démarrage.
-- Numéro 073 : 070, 071 et 072 sont pris sur d'autres branches (house-room, actor-tenant, member-first-name).
--
-- 1. `jobs_profiles` : profil emploi d'UN membre, dans son locataire (tenant_isolation) : activé ou non, identifiant
--    opaque du compte partenaire chez le service emploi, CV de base, effacement prévu 30 jours après désactivation.
-- 2. `jobs_docs` : versions retouchées chez Nokker (CV `cvl_`, lettre `lt_`), document JSON, PDF et son empreinte.

CREATE TABLE IF NOT EXISTS jobs_profiles (
    tenant_id           TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    id                  TEXT PRIMARY KEY,                     -- em_ + 10
    account_id          TEXT NOT NULL UNIQUE,
    active              BOOLEAN NOT NULL DEFAULT false,
    partner_account_id  TEXT NOT NULL DEFAULT '',
    base_cv_id          TEXT NOT NULL DEFAULT '',
    cv_name             TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at        TIMESTAMPTZ,
    deactivated_at      TIMESTAMPTZ,
    erase_after         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS jobs_docs (
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    id           TEXT PRIMARY KEY,                            -- cvl_ ou lt_ + 10
    profile_id   TEXT NOT NULL REFERENCES jobs_profiles (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('cv', 'letter')),
    parent       TEXT NOT NULL DEFAULT '',
    offer        TEXT NOT NULL DEFAULT '',
    data         JSONB NOT NULL DEFAULT '{}',
    pdf          BYTEA NOT NULL DEFAULT '',
    pdf_sha256   TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_docs_profile ON jobs_docs (profile_id);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['jobs_profiles', 'jobs_docs'] LOOP
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
END $$;
