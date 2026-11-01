-- Apprentissage sous contrôle humain chez nokker.ai (docs/LEARNING.md, nokker/learning/) : skills appris et versionnés,
-- écritures de mémoire en attente de validation (quarantaine A-3), journal d'apprentissage. Rejouée à chaque démarrage
-- (IF NOT EXISTS, états vérifiés). Pas de clé étrangère.
-- NUMÉROTATION : 046, après 044 (thread_shares) et 045 (trace_calls) ; aucune dépendance d'ordre avec elles.
--
-- Tout est rangé par COMPTE dans le locataire du compte, sous RLS activée ET forcée par tenant_id (docs/TENANCY.md
-- §4) ; effacé par `nokker erase` (colonnes tenant_id et account_id, lues dans le catalogue). Rien n'est mis à jour en
-- place sauf la décision d'une note en attente : une version de skill annulée reste, une note refusée reste au journal.

CREATE TABLE IF NOT EXISTS nokker_learned_skills (
    tenant_id    text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id   text NOT NULL,
    name         text NOT NULL,
    version      integer NOT NULL,
    content      text NOT NULL,          -- SKILL.md complet ; vide pour une désactivation
    sha256       text NOT NULL,
    op           text NOT NULL,          -- create | update | revert | disable (vérifié par le code)
    active       boolean NOT NULL DEFAULT true,
    reverted_to  integer,
    thread_id    text NOT NULL DEFAULT '',
    turn_index   integer,
    actor        text NOT NULL DEFAULT 'agent',
    approval_id  text,
    at           text NOT NULL,
    PRIMARY KEY (tenant_id, account_id, name, version)
);

CREATE TABLE IF NOT EXISTS nokker_memory_pending (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id    text NOT NULL,
    id            text NOT NULL,
    source        text NOT NULL,         -- memory_note | propose
    kind          text NOT NULL,
    title         text NOT NULL,
    body          text NOT NULL,
    scope         text NOT NULL DEFAULT 'project',
    derived_from  text NOT NULL DEFAULT 'agent',
    project       text NOT NULL DEFAULT '',
    links         jsonb NOT NULL DEFAULT '[]'::jsonb,
    thread_id     text NOT NULL DEFAULT '',
    turn_index    integer,
    at            text NOT NULL,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_at    text,
    decided_by    text,
    result        text,
    PRIMARY KEY (tenant_id, account_id, id)
);
CREATE INDEX IF NOT EXISTS nokker_memory_pending_status ON nokker_memory_pending (tenant_id, account_id, status, at);

CREATE TABLE IF NOT EXISTS nokker_learning_journal (
    seq          bigserial,
    tenant_id    text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id   text NOT NULL,
    id           text NOT NULL,
    kind         text NOT NULL,
    subject      text NOT NULL,
    at           text NOT NULL,
    actor        text NOT NULL DEFAULT 'agent',
    version      integer,
    thread_id    text NOT NULL DEFAULT '',
    turn_index   integer,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS nokker_learning_journal_account ON nokker_learning_journal (tenant_id, account_id, seq);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['nokker_learned_skills', 'nokker_memory_pending', 'nokker_learning_journal'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
END $$;
