-- Nokker — nœud téléphone (P7a, docs/LOKKAL-PHONE.md §3, nokker/cloud/phone.py). Additive et idempotente.
--
-- 1. `lokkal_machines.kind` : `lokkal` (défaut, tout ce qui existe) ou `phone`. Le jeton d'un téléphone ne vaut QUE sur
--    /v1/gateway/phone/*, avec la preuve de sa clé (X-Nokker-Node-Proof).
-- 2. `phone_nodes` : la clé publique du nœud (SPKI, P-256), son empreinte, sa plateforme, attestation et stockage de
--    la clé, et le dernier battement borné. Locataire du membre (tenant_isolation).
-- 3. `phone_tasks` : la file des opérations fermées confiées au téléphone (lecture seule en phase S), bail de 60 s,
--    échéance, résultat texte ≤ 64 Kio. Locataire du membre.
-- Numéro 077 : 075 = zones (p2/zones-core), 076 = appareils (p2/device-pim).

ALTER TABLE lokkal_machines ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'lokkal';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lokkal_machines_kind_check') THEN
        ALTER TABLE lokkal_machines ADD CONSTRAINT lokkal_machines_kind_check CHECK (kind IN ('lokkal', 'phone'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS phone_nodes (
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    machine_id   TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,                                  -- le membre auquel le téléphone est rattaché
    platform     TEXT NOT NULL CHECK (platform IN ('ios', 'android')),
    attested     BOOLEAN NOT NULL DEFAULT false,
    key_storage  TEXT NOT NULL CHECK (key_storage IN ('secure_enclave', 'software')),
    pubkey_spki  TEXT NOT NULL,                                  -- base64 DER, P-256
    key_fp       TEXT NOT NULL,                                  -- sha256 hex de la clé (8 premiers montrés sur la carte)
    beat         JSONB NOT NULL DEFAULT '{}',
    beat_at      DOUBLE PRECISION,
    created_at   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS phone_tasks (
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    task_id      TEXT PRIMARY KEY,                               -- pt_ + 16 hex
    machine_id   TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    op           TEXT NOT NULL,
    args         JSONB NOT NULL DEFAULT '{}',
    args_sha256  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'leased', 'done', 'failed', 'expired', 'cancelled')),
    lease_until  DOUBLE PRECISION,
    deadline     DOUBLE PRECISION NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    result       TEXT,
    error        JSONB,
    created_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS phone_tasks_machine ON phone_tasks (machine_id, status, created_at);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['phone_nodes', 'phone_tasks'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(t) AND tgname = t || '_tenant_immutable') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OF tenant_id ON %I FOR EACH ROW '
                           'EXECUTE FUNCTION nokker_tenant_immutable()', t || '_tenant_immutable', t);
        END IF;
    END LOOP;
END $$;
