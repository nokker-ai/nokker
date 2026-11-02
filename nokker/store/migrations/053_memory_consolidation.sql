-- Consolidation nocturne de la mémoire (docs/CONSOLIDATION.md, nokker/memory/consolidation/, drapeau
-- `memory_consolidation`). NUMÉROTATION : 053 (061 sur la branche, renumérotée à l'intégration du train 0311), après 049 (mémoire longue, dont elle
-- dépend)  ; avant actor_tenant (la dernière, qui réécrit ses politiques). Rejouée à chaque démarrage : chaque étape vérifie l'état avant d'agir.
--
-- La consolidation s'applique DIRECTEMENT (pas de quarantaine) et rapporte : chaque opération est journalisée avec son
-- inverse exact, défaire est possible par opération et par nuit, et rien n'est jamais supprimé pour de bon (une note
-- retirée part dans `nokker_memory_archive`, d'où elle revient telle quelle).
--
-- Tout est rangé par COMPTE dans le locataire du compte (celui qui détient les notes : le compte pour sa mémoire de
-- profil, le payeur pour la mémoire de son foyer), sous RLS activée ET forcée : `tenant_isolation` = le locataire ET le
-- compte (`app.account_id`), comme `nokker_long_memory` (049). Effacement : `nokker erase` les couvre par `tenant_id`
-- et `account_id` ; une note du foyer écrite par un membre (son `author_account`) part aussi de l'archive et du journal
-- quand ce membre est effacé (politiques `consolidation_author_erase_*`, pendant l'effacement seulement).

CREATE TABLE IF NOT EXISTS nokker_consolidation_runs (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id    text NOT NULL,
    id            text NOT NULL,
    night         text NOT NULL,          -- AAAA-MM-JJ, date locale (fuseau du compte) de la nuit consolidée
    tz            text NOT NULL DEFAULT 'UTC',
    trigger       text NOT NULL DEFAULT 'manual' CHECK (trigger IN ('schedule', 'manual', 'cli')),
    status        text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    dry_run       boolean NOT NULL DEFAULT false,
    model         text,
    started_at    double precision NOT NULL,
    finished_at   double precision,
    counts        jsonb NOT NULL DEFAULT '{}'::jsonb,
    skipped       integer NOT NULL DEFAULT 0,   -- opérations au-delà du plafond de la nuit, reportées
    error         text,
    undone_at     double precision,
    notified      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, account_id, id)
);
-- une seule consolidation planifiée par nuit et par compte (deux pods, un redémarrage : la seconde est refusée)
CREATE UNIQUE INDEX IF NOT EXISTS nokker_consolidation_runs_night
    ON nokker_consolidation_runs (tenant_id, account_id, night) WHERE trigger = 'schedule' AND NOT dry_run;
CREATE INDEX IF NOT EXISTS nokker_consolidation_runs_recent
    ON nokker_consolidation_runs (tenant_id, account_id, started_at DESC);

CREATE TABLE IF NOT EXISTS nokker_consolidation_ops (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id    text NOT NULL,
    id            text NOT NULL,
    run_id        text NOT NULL,
    seq           integer NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('merge', 'correct', 'archive', 'summarize', 'lesson')),
    scope         text NOT NULL,
    household_id  text,
    reason        text NOT NULL DEFAULT '',
    evidence      jsonb NOT NULL DEFAULT '{}'::jsonb,
    changes       jsonb NOT NULL,         -- [{name, action, before, after, annotation?}] : l'inverse exact
    authors       text[] NOT NULL DEFAULT '{}',  -- auteurs des notes touchées (effacement d'un membre du foyer)
    at            double precision NOT NULL,
    undone_at     double precision,
    PRIMARY KEY (tenant_id, account_id, id)
);
CREATE INDEX IF NOT EXISTS nokker_consolidation_ops_run ON nokker_consolidation_ops (tenant_id, account_id, run_id, seq);
CREATE INDEX IF NOT EXISTS nokker_consolidation_ops_authors ON nokker_consolidation_ops USING gin (authors);

CREATE TABLE IF NOT EXISTS nokker_memory_archive (
    tenant_id       text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id      text NOT NULL,
    name            text NOT NULL,
    op_id           text NOT NULL,
    scope           text NOT NULL,
    household_id    text,
    author_account  text NOT NULL,
    note            jsonb NOT NULL,       -- la ligne complète de nokker_long_memory (plongement compris, en texte)
    reason          text NOT NULL DEFAULT '',
    archived_at     double precision NOT NULL,
    restored_at     double precision,
    PRIMARY KEY (tenant_id, account_id, op_id, name)
);
CREATE INDEX IF NOT EXISTS nokker_memory_archive_author ON nokker_memory_archive (author_account);

CREATE TABLE IF NOT EXISTS nokker_memory_annotations (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id    text NOT NULL,
    id            text NOT NULL,
    name          text NOT NULL,
    run_id        text NOT NULL,
    op_id         text NOT NULL,
    text          text NOT NULL,
    at            double precision NOT NULL,
    undone_at     double precision,
    PRIMARY KEY (tenant_id, account_id, id)
);
CREATE INDEX IF NOT EXISTS nokker_memory_annotations_note ON nokker_memory_annotations (tenant_id, account_id, name);

-- Notes épinglées par l'utilisateur : la consolidation n'y touche jamais.
CREATE TABLE IF NOT EXISTS nokker_memory_pins (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id    text NOT NULL,
    name          text NOT NULL,
    pinned_at     double precision NOT NULL,
    PRIMARY KEY (tenant_id, account_id, name)
);

-- Réglages par compte : heure locale de la consolidation (cron à 5 champs + fuseau), ou consolidation coupée.
CREATE TABLE IF NOT EXISTS nokker_consolidation_settings (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id    text NOT NULL,
    enabled       boolean NOT NULL DEFAULT true,
    tz            text,
    cron          text,
    updated_at    double precision NOT NULL,
    PRIMARY KEY (tenant_id, account_id)
);

DO $$
DECLARE
    t   text;
    pol RECORD;
BEGIN
    FOREACH t IN ARRAY ARRAY['nokker_consolidation_runs', 'nokker_consolidation_ops', 'nokker_memory_archive',
                             'nokker_memory_annotations', 'nokker_memory_pins', 'nokker_consolidation_settings'] LOOP
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = to_regclass(t)) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = t
                       AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(''app.tenant_id'', true) '
                           'AND account_id = NULLIF(current_setting(''app.account_id'', true), '''')) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true) '
                           'AND account_id = NULLIF(current_setting(''app.account_id'', true), ''''))', t);
        END IF;
    END LOOP;
    -- effacement d'un membre du foyer : ses contributions archivées et le journal qui les cite partent avec lui
    FOR pol IN SELECT * FROM (VALUES
        ('nokker_memory_archive', 'consolidation_author_erase_read', 'SELECT',
         'author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL'),
        ('nokker_memory_archive', 'consolidation_author_erase', 'DELETE',
         'author_account = NULLIF(current_setting(''app.account_id'', true), '''') '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL'),
        ('nokker_consolidation_ops', 'consolidation_author_erase_read', 'SELECT',
         'NULLIF(current_setting(''app.account_id'', true), '''') = ANY (authors) '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL'),
        ('nokker_consolidation_ops', 'consolidation_author_erase', 'DELETE',
         'NULLIF(current_setting(''app.account_id'', true), '''') = ANY (authors) '
         'AND NULLIF(current_setting(''nokker.erasing_tenant'', true), '''') IS NOT NULL')
    ) AS v(tbl, name, cmd, using_expr) LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                              AND tablename = pol.tbl AND policyname = pol.name);
        EXECUTE format('CREATE POLICY %I ON %I FOR %s USING (%s)', pol.name, pol.tbl, pol.cmd, pol.using_expr);
    END LOOP;
END
$$;
