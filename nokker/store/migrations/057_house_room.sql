-- Salle Maison (chantier p2/house-room, docs/HOUSE-ROOM.md §4). Numéro 057 (070 sur la branche, renumérotée à l'intégration du train 0311) ; avant actor_tenant (la dernière).
-- Demande 039 (foyer) et 049 (nokker_long_memory_visible). Idempotente : rejouable.
--
-- Un journal append-only par foyer, au locataire du FOYER (le payeur). Un membre actif y lit (sauf `adults` pour un
-- profil mineur) et y écrit à son nom ; personne ne modifie une ligne ; le foyer supprimé emporte sa salle.

CREATE TABLE IF NOT EXISTS house_room_messages (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),   -- locataire du FOYER
    household_id    TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('member', 'agent', 'event')),
    text            TEXT NOT NULL CHECK (length(text) <= 8000),
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    author_account  TEXT,
    author_label    TEXT NOT NULL DEFAULT '',
    agent           TEXT,
    audience        TEXT NOT NULL DEFAULT 'all' CHECK (audience IN ('all', 'adults')),
    origin          JSONB NOT NULL DEFAULT '{"kind": "room"}'::jsonb,
    reply_to        BIGINT,
    event           TEXT,
    approvals       TEXT[] NOT NULL DEFAULT '{}',
    CHECK ((kind = 'event') = (author_account IS NULL))
);
CREATE INDEX IF NOT EXISTS house_room_messages_hh ON house_room_messages (tenant_id, household_id, id);
CREATE INDEX IF NOT EXISTS house_room_messages_author ON house_room_messages (author_account);

-- Le compte posé (`app.account_id`) est bien celui du locataire posé (`app.tenant_id`) : un compte d'un autre locataire
-- ne s'ouvre pas la salle en posant l'identifiant d'un membre (défense en profondeur ; `accounts` est hors RLS).
CREATE OR REPLACE FUNCTION nokker_house_room_actor_ok() RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
DECLARE
    acc text := NULLIF(current_setting('app.account_id', true), '');
    tid text := NULLIF(current_setting('app.tenant_id', true), '');
BEGIN
    IF acc IS NULL OR tid IS NULL THEN
        RETURN false;
    END IF;
    RETURN EXISTS (SELECT 1 FROM accounts a WHERE a.id = acc AND a.tenant_id = tid);
END;
$$;

-- Append-only : aucune ligne ne change.
CREATE OR REPLACE FUNCTION nokker_house_room_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'salle Maison : un message ne change pas (journal append-only)' USING ERRCODE = 'insufficient_privilege';
END;
$$;

-- Foyer supprimé : sa salle part avec lui.
CREATE OR REPLACE FUNCTION nokker_house_room_gone() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.kind = 'household' THEN
        DELETE FROM house_room_messages WHERE tenant_id = OLD.tenant_id AND household_id = OLD.project_id;
    END IF;
    RETURN OLD;
END;
$$;

DO $$
DECLARE
    pol record;
BEGIN
    ALTER TABLE house_room_messages ENABLE ROW LEVEL SECURITY;
    ALTER TABLE house_room_messages FORCE ROW LEVEL SECURITY;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                   AND tablename = 'house_room_messages' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON house_room_messages
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'house_room_messages'::regclass
                   AND tgname = 'house_room_messages_frozen') THEN
        CREATE TRIGGER house_room_messages_frozen BEFORE UPDATE ON house_room_messages
            FOR EACH ROW EXECUTE FUNCTION nokker_house_room_frozen();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'team_projects'::regclass
                   AND tgname = 'team_projects_house_room_gone') THEN
        CREATE TRIGGER team_projects_house_room_gone AFTER DELETE ON team_projects
            FOR EACH ROW EXECUTE FUNCTION nokker_house_room_gone();
    END IF;
    FOR pol IN SELECT * FROM (VALUES
        -- un membre ACTIF lit la salle de SON foyer ; `adults` jamais pour un profil mineur (lu en direct)
        ('house_room_read', 'SELECT',
         'nokker_house_room_actor_ok() AND nokker_long_memory_visible(household_id, tenant_id, audience)', NULL),
        -- un membre actif écrit à SON nom (son message, ou la réponse d'un agent qu'il a fait parler)
        ('house_room_write', 'INSERT', NULL,
         'kind IN (''member'', ''agent'') '
         'AND author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND nokker_house_room_actor_ok() AND nokker_long_memory_visible(household_id, tenant_id, audience)'),
        -- effacement de son compte (nokker erase) : ses messages dans le foyer d'un autre partent avec lui
        ('house_room_author_erase_read', 'SELECT',
         'author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL', NULL),
        ('house_room_author_erase', 'DELETE',
         'author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL', NULL)
    ) AS v(name, cmd, using_expr, check_expr) LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                              AND tablename = 'house_room_messages' AND policyname = pol.name);
        EXECUTE format('CREATE POLICY %I ON house_room_messages FOR %s', pol.name, pol.cmd)
             || CASE WHEN pol.using_expr IS NOT NULL THEN ' USING (' || pol.using_expr || ')' ELSE '' END
             || CASE WHEN pol.check_expr IS NOT NULL THEN ' WITH CHECK (' || pol.check_expr || ')' ELSE '' END;
    END LOOP;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
        GRANT SELECT, INSERT, DELETE ON house_room_messages TO nokker_app;
        GRANT USAGE, SELECT ON SEQUENCE house_room_messages_id_seq TO nokker_app;
    END IF;
END
$$;
