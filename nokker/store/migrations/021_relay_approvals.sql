-- Approbations fortes par la passerelle (lot X10, docs/LOKKAL.md §7.3, nokker/cloud/approvals.py) : la boîte aux
-- lettres de nokker.ai. Une ligne par carte forte déposée par une machine liée ; nokker.ai n'y décide rien, il garde la
-- preuve signée par l'appareil (`proof`, telle quelle) jusqu'à ce que la machine l'ait vérifiée et rendu son résultat.
-- Sous RLS activée ET forcée par tenant_id (docs/TENANCY.md §4), comme lokkal_machine_load (020) : on la lit et l'écrit
-- en connaissant le locataire (la machine authentifiée par son jeton, ou le compte de la session). Pas de clé
-- étrangère : `nokker erase` (par tenant_id) l'efface avec le reste. Rejouée à chaque démarrage.
CREATE TABLE IF NOT EXISTS lokkal_relay_approvals (
    machine_id   text NOT NULL,
    approval_id  text NOT NULL,               -- l'id de l'approbation LOCALE, déposé tel quel
    tenant_id    text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id   text NOT NULL,
    title        text NOT NULL,
    reason       text NOT NULL DEFAULT '',     -- caviardé par la machine, 600 car. au plus
    advice       text,                         -- avis du juge (X8), caviardé ; NULL tant qu'il n'existe pas
    strength     text NOT NULL,
    challenge    text NOT NULL,                -- défi de la machine (JSON canonique, nonce à usage unique côté machine)
    expires_at   double precision NOT NULL,
    created_at   double precision NOT NULL,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'forwarded', 'approved', 'rejected', 'expired')),
    decision     text CHECK (decision IN ('approve', 'reject')),
    proof        jsonb,                        -- preuve de l'appareil, jamais crue par nokker.ai
    decided_at   double precision,
    result_at    double precision,
    outcome      text CHECK (outcome IN ('executed', 'failed')),
    error        text,
    PRIMARY KEY (machine_id, approval_id)
);
CREATE INDEX IF NOT EXISTS lokkal_relay_approvals_forwarded ON lokkal_relay_approvals (machine_id)
    WHERE status = 'forwarded';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'lokkal_relay_approvals' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE lokkal_relay_approvals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE lokkal_relay_approvals FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'lokkal_relay_approvals'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON lokkal_relay_approvals
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
