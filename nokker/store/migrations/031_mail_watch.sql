-- Relève du courrier du déclencheur `mail:<filtre>` (docs/BUBBLES.md §9.5, nokker/agents/mailwatch.py) : une ligne par
-- agent armé sur ce déclencheur — son curseur (UIDVALIDITY, dernier UID vu) et la santé de sa relève. Jamais
-- d'identifiant, jamais de contenu de message. Écrite dans le locataire de l'agent : RLS activée ET forcée par
-- tenant_id, comme nokker_agent_runs (024) ; pas de clé étrangère (piliers écrits en parallèle) ; `nokker erase`
-- l'efface par tenant_id. Rejouée à chaque démarrage (apply_migrations ET PgWatchStore) : IF NOT EXISTS partout, état
-- vérifié avant d'agir.
CREATE TABLE IF NOT EXISTS nokker_mail_watch (
    agent_id      TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    filter        TEXT NOT NULL,
    uidvalidity   BIGINT,
    last_uid      BIGINT,                      -- NULL = pas encore de ligne de base
    last_poll_at  DOUBLE PRECISION,
    last_ok_at    DOUBLE PRECISION,
    next_poll_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    error         TEXT,                        -- code et message sans secret
    pending       INTEGER NOT NULL DEFAULT 0,  -- messages nouveaux qui attendent (passage vivant, plafond horaire)
    held          TEXT,
    resync_at     DOUBLE PRECISION,
    last_run_id   TEXT,
    updated_at    DOUBLE PRECISION NOT NULL,
    inflight_run_id TEXT,                      -- curseur « en vol » : passage choisi d'avance (p2/mail-local)
    inflight_uid    BIGINT
);
-- Colonnes du curseur « en vol » (ajoutées après la première version de 031) : sur une table déjà créée, ajoutées une
-- fois ; aucun ALTER (donc aucun verrou exclusif) quand elles existent.
DO $$
DECLARE
    col  RECORD;
BEGIN
    FOR col IN SELECT * FROM (VALUES ('inflight_run_id', 'TEXT'), ('inflight_uid', 'BIGINT')) AS c(name, ddl) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_mail_watch'::regclass
                       AND attname = col.name AND NOT attisdropped) THEN
            EXECUTE format('ALTER TABLE nokker_mail_watch ADD COLUMN %I %s', col.name, col.ddl);
        END IF;
    END LOOP;
END
$$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'nokker_mail_watch' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE nokker_mail_watch ENABLE ROW LEVEL SECURITY;
        ALTER TABLE nokker_mail_watch FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'nokker_mail_watch'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nokker_mail_watch
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
