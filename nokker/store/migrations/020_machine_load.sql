-- Charge des machines Lokkal (lot X5, docs/LOKKAL.md §7.2) : la dernière charge envoyée par chaque machine
-- (`POST /v1/gateway/machine/load`), une seule ligne courante par machine, pas d'historique. À côté de
-- lokkal_machines.last_seen_at (018) mais dans sa propre table : on la lit et l'écrit en connaissant le locataire (la
-- machine authentifiée, ou le compte de « mes machines »), donc sous RLS activée ET forcée (docs/TENANCY.md §4), à la
-- différence de lokkal_machines qu'on cherche par empreinte de jeton. Pas de clé étrangère : l'effacement du compte
-- (`nokker erase`, par tenant_id) la supprime comme le reste. Rejouée à chaque démarrage.
CREATE TABLE IF NOT EXISTS lokkal_machine_load (
    machine_id   text PRIMARY KEY,
    tenant_id    text NOT NULL DEFAULT current_setting('app.tenant_id'),
    load         jsonb NOT NULL,
    at           double precision NOT NULL     -- heure de nokker.ai à la réception (secondes epoch)
);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'lokkal_machine_load' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE lokkal_machine_load ENABLE ROW LEVEL SECURITY;
        ALTER TABLE lokkal_machine_load FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'lokkal_machine_load'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON lokkal_machine_load
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
