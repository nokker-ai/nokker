-- Nokker — écrans du mur (lot V7, docs/SCREENS.md) : un écran composé par le modèle depuis le catalogue de composants,
-- brouillon puis publié sur le mur sous carte forte (wall:publish). Le noyau rend le HTML lui-même ; on ne garde que la
-- description validée (spec). Locataire du compte, RLS forcée comme ambient_boards (054). Additive, idempotente.
-- Numéro 083 (r48 ; 084 = household_projects F2, 085 réservé à account_persona V6, r48.1).

CREATE TABLE IF NOT EXISTS wall_screens (
    tenant_id          text NOT NULL DEFAULT current_setting('app.tenant_id'),
    screen_id          text NOT NULL,
    account_id         text NOT NULL,
    thread_id          text NOT NULL DEFAULT '',
    title              text NOT NULL DEFAULT '',
    spec               jsonb NOT NULL DEFAULT '{}'::jsonb,
    version            integer NOT NULL DEFAULT 1,
    status             text NOT NULL DEFAULT 'draft',
    created_at         double precision NOT NULL DEFAULT extract(epoch from now()),
    updated_at         double precision NOT NULL DEFAULT extract(epoch from now()),
    published_version  integer,
    published_sha256   text,
    published_at       double precision,
    PRIMARY KEY (tenant_id, screen_id),
    CONSTRAINT wall_screens_status_check CHECK (status IN ('draft', 'published'))
);
CREATE INDEX IF NOT EXISTS wall_screens_account ON wall_screens (tenant_id, account_id, status);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'wall_screens' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE wall_screens ENABLE ROW LEVEL SECURITY;
        ALTER TABLE wall_screens FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'wall_screens' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON wall_screens
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
