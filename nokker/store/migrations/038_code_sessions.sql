-- Sessions de code multi-utilisateur (docs/LOKKAL.md §7.28, nokker/code_sessions/) : une ligne par session ouverte par
-- un compte dans SON locataire — sa racine relative au volume des espaces de travail, son fil, son mode, le dépôt
-- cloné (URL propre) et le NOM de l'identifiant de forge (jamais sa valeur). RLS activée ET forcée par tenant_id ;
-- l'utilisateur est filtré en plus par le code (deux utilisateurs d'un même locataire ne se voient pas). Rejouée à
-- chaque démarrage : IF NOT EXISTS partout, état vérifié avant d'agir.
CREATE TABLE IF NOT EXISTS nokker_code_sessions (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    user_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL,
    root_rel      TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'accept_edits',
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'idle', 'closed')),
    repo_url      TEXT,
    repo_ref      TEXT,
    credential    TEXT,
    created_at    DOUBLE PRECISION NOT NULL,
    last_used_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
    closed_at     DOUBLE PRECISION
);
CREATE UNIQUE INDEX IF NOT EXISTS nokker_code_sessions_thread ON nokker_code_sessions (thread_id);
CREATE INDEX IF NOT EXISTS nokker_code_sessions_user ON nokker_code_sessions (tenant_id, user_id, status);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'nokker_code_sessions' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE nokker_code_sessions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE nokker_code_sessions FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'nokker_code_sessions'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nokker_code_sessions
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
