-- Nokker — appareils de l'app et leurs capacités de lecture (docs/LOKKAL-PHONE.md §4.3, P8, nokker/devices/).
-- Additive et idempotente. Le téléphone déclare ce qu'il sait lire pour le membre (agenda, contacts, rappels) ; le
-- noyau ne garde que son id, son nom, sa plateforme et ces capacités : AUCUNE donnée lue (les réponses ne vivent que
-- dans le résultat du tour). Locataire du membre (tenant_isolation), propriétaire = le compte.
-- Numéro 076 : 075 = zones de l'app (p2/zones-core).

CREATE TABLE IF NOT EXISTS member_devices (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    platform    TEXT NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
    caps        TEXT[] NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, device_id)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'member_devices' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE member_devices ENABLE ROW LEVEL SECURITY;
        ALTER TABLE member_devices FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'member_devices' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON member_devices USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('member_devices')
                   AND tgname = 'member_devices_tenant_immutable') THEN
        CREATE TRIGGER member_devices_tenant_immutable BEFORE UPDATE OF tenant_id ON member_devices FOR EACH ROW
            EXECUTE FUNCTION nokker_tenant_immutable();
    END IF;
END $$;
