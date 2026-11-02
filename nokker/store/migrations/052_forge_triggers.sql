-- Déclencheurs de forge des sessions de code (docs/LOKKAL.md §7.28.9, drapeau `code.forge_triggers`).
-- Numéro 052 (050 sur la branche, renumérotée à l'intégration du train 0311 ; avant actor_tenant, la dernière). Rejouée à chaque démarrage (IF NOT EXISTS, politiques vérifiées).
-- Contenu d'un compte : RLS par `tenant_id` (locataire de la connexion), effacé avec le locataire.
CREATE TABLE IF NOT EXISTS forge_hooks (
    id          text NOT NULL,
    tenant_id   text NOT NULL DEFAULT current_setting('app.tenant_id'),
    user_id     text NOT NULL,
    provider    text NOT NULL CHECK (provider IN ('forgejo', 'github')),
    repo_url    text NOT NULL,
    credential  text,
    mention     text NOT NULL,
    secret      text NOT NULL,
    mode        text NOT NULL,
    created_at  double precision NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS forge_pr_sessions (
    tenant_id   text NOT NULL DEFAULT current_setting('app.tenant_id'),
    hook_id     text NOT NULL,
    number      integer NOT NULL,
    session_id  text NOT NULL,
    thread_id   text NOT NULL,
    head_ref    text NOT NULL,
    head_sha    text NOT NULL DEFAULT '',
    base_ref    text NOT NULL DEFAULT '',
    status      text NOT NULL CHECK (status IN ('open', 'closed')),
    created_at  double precision NOT NULL,
    updated_at  double precision NOT NULL,
    PRIMARY KEY (tenant_id, hook_id, number)
);
CREATE INDEX IF NOT EXISTS forge_pr_sessions_thread_idx ON forge_pr_sessions (thread_id);
CREATE TABLE IF NOT EXISTS forge_deliveries (
    tenant_id   text NOT NULL DEFAULT current_setting('app.tenant_id'),
    hook_id     text NOT NULL,
    key         text NOT NULL,
    at          double precision NOT NULL,
    PRIMARY KEY (tenant_id, hook_id, key)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['forge_hooks', 'forge_pr_sessions', 'forge_deliveries'] LOOP
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = t::regclass) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = t
                       AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
END $$;
