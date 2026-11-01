-- Liens de conversation partageables et révocables (docs/CLIENT-API.md « Export et partage d'un fil »,
-- nokker/threads/shares.py). Rejouée à chaque démarrage. Numérotée 044 : 034 et 042 sont réservées par d'autres
-- branches, 043 est la dernière posée (vérifié sur la forge et les worktrees le 01.11.2026).
--
-- 1. `thread_shares` : l'INSTANTANÉ figé du fil au moment du partage (pas un miroir vivant) et son état. Contenu de
--    locataire : tenant_id par défaut = app.tenant_id, RLS activée ET forcée (docs/TENANCY.md §4) ; l'effacement du
--    compte (`nokker erase`) la couvre par tenant_id. Le jeton n'y est jamais, même haché.
CREATE TABLE IF NOT EXISTS thread_shares (
    share_id       TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    thread_id      TEXT NOT NULL,
    owner          TEXT NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL,
    expires_at     DOUBLE PRECISION NOT NULL,
    revoked_at     DOUBLE PRECISION,
    include_tools  BOOLEAN NOT NULL DEFAULT false,
    snapshot       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS thread_shares_thread_idx ON thread_shares (tenant_id, thread_id, created_at DESC);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'thread_shares' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE thread_shares ENABLE ROW LEVEL SECURITY;
        ALTER TABLE thread_shares FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'thread_shares' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON thread_shares
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;

-- 2. `thread_share_tokens` : EMPREINTE sha256 du jeton public → partage et locataire. HORS RLS par conception, comme
--    lokkal_gateway_codes (019) : la lecture publique arrive sans locataire, elle le retrouve ici par l'empreinte puis
--    lit l'instantané DANS ce locataire (RLS). Ni contenu ni jeton en clair ; l'état (révoqué, expiré) y est recopié
--    pour refuser sans ouvrir le locataire. Un partage = une empreinte.
CREATE TABLE IF NOT EXISTS thread_share_tokens (
    token_sha256  TEXT PRIMARY KEY,
    share_id      TEXT NOT NULL UNIQUE,
    tenant_id     TEXT NOT NULL,
    expires_at    DOUBLE PRECISION NOT NULL,
    revoked_at    DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS thread_share_tokens_exp ON thread_share_tokens (expires_at);
