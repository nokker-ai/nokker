-- Règles d'approbation suggérées (docs/APPROVALS.md §7) : ce que le propriétaire a fait d'une proposition.
-- Numérotée 047 (après 044 thread_shares, 045 trace_calls, 046 learning ; aucune dépendance d'ordre avec elles).
-- Rejouée à chaque démarrage (IF NOT EXISTS, politique vérifiée avant création).
--
-- Une ligne par (locataire, utilisateur, suggestion) : `accepted` (la règle a été écrite par le mécanisme existant,
-- `provenance` dit d'où elle vient) ou `dismissed` (masquée). Les propositions elles-mêmes ne sont pas stockées :
-- elles se recalculent depuis `approvals`. Contenu d'un compte : RLS comme les cartes (`tenant_id` du locataire de la
-- connexion), effacée avec le locataire.
CREATE TABLE IF NOT EXISTS approval_suggestion_marks (
    tenant_id      text NOT NULL DEFAULT current_setting('app.tenant_id'),
    "user"         text NOT NULL,
    suggestion_id  text NOT NULL,
    state          text NOT NULL CHECK (state IN ('accepted', 'dismissed')),
    rule           text NOT NULL,
    provenance     jsonb NOT NULL DEFAULT '{}'::jsonb,
    at             double precision NOT NULL,
    PRIMARY KEY (tenant_id, "user", suggestion_id)
);
CREATE INDEX IF NOT EXISTS approvals_created_at_idx ON approvals (created_at);

DO $$
BEGIN
    IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class
            WHERE oid = 'approval_suggestion_marks'::regclass) THEN
        ALTER TABLE approval_suggestion_marks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE approval_suggestion_marks FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                   AND tablename = 'approval_suggestion_marks' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON approval_suggestion_marks
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
