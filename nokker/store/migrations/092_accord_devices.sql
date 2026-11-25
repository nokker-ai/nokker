-- Nokker — appareils ACCORD appairés (docs/ACCORD-DEVICE.md, nokker/accord/store.py) : un bouton physique Autoriser /
-- Refuser sur le LAN de Lokkal. Une ligne par appareil : sa clé PUBLIQUE Ed25519 (aucun secret), son empreinte courte
-- affichée à l'appairage, et sa révocation (la ligne reste, la clé n'est plus jamais acceptée). Additive. Sous RLS du
-- locataire ; `nokker erase` la couvre (tenant_id).

CREATE TABLE IF NOT EXISTS accord_devices (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    device_id   TEXT PRIMARY KEY CHECK (device_id ~ '^acc_[0-9a-f]{12}$'),
    pubkey      TEXT NOT NULL CHECK (pubkey ~ '^[A-Za-z0-9_-]{43}$'),   -- 32 octets en base64url sans « = »
    key8        TEXT NOT NULL CHECK (key8 ~ '^[0-9a-f]{8}$'),
    host_fp     TEXT NOT NULL CHECK (host_fp ~ '^[0-9a-f]{64}$'),      -- empreinte de l'hôte épinglée à l'appairage
    paired_at   DOUBLE PRECISION NOT NULL,
    revoked_at  DOUBLE PRECISION
);
CREATE UNIQUE INDEX IF NOT EXISTS accord_devices_active_key ON accord_devices (tenant_id, pubkey) WHERE revoked_at IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'accord_devices' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE accord_devices ENABLE ROW LEVEL SECURITY;
        ALTER TABLE accord_devices FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'accord_devices' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON accord_devices USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('accord_devices')
                   AND tgname = 'accord_devices_tenant_immutable') THEN
        CREATE TRIGGER accord_devices_tenant_immutable BEFORE UPDATE OF tenant_id ON accord_devices
            FOR EACH ROW EXECUTE FUNCTION nokker_tenant_immutable();
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON accord_devices TO nokker_app;
    END IF;
END $$;
