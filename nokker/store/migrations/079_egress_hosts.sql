-- Nokker — pages par la connexion du téléphone (P11, docs/LOKKAL-PHONE.md §4.5, nokker/cloud/egress.py). Additive.
-- La liste d'hôtes de la personne (≤ 50, `exemple.ch` ou `*.exemple.ch`) : un hôte de la liste passe par son
-- téléphone (`egress` allumé), pendant un tour qu'elle a ouvert. Locataire du membre (tenant_isolation).

CREATE TABLE IF NOT EXISTS member_egress_hosts (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT PRIMARY KEY,
    hosts       TEXT[] NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'member_egress_hosts' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE member_egress_hosts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE member_egress_hosts FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'member_egress_hosts' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON member_egress_hosts USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('member_egress_hosts')
                   AND tgname = 'member_egress_hosts_tenant_immutable') THEN
        CREATE TRIGGER member_egress_hosts_tenant_immutable BEFORE UPDATE OF tenant_id ON member_egress_hosts
            FOR EACH ROW EXECUTE FUNCTION nokker_tenant_immutable();
    END IF;
END $$;
