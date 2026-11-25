-- Nokker — inscriptions en cours d'un téléphone (P7a, nokker/cloud/phone.py). Additive et idempotente.
-- Une ligne par carte `phone_register`, de la demande au retrait du jeton : un redémarrage du noyau pendant
-- l'approbation n'oblige plus à se réinscrire. Le jeton n'y est JAMAIS en clair : seul son SCEAU (pour la clé
-- d'accord du téléphone) y attend le retrait unique. Locataire du membre (tenant_isolation). Ménage à l'expiration.

CREATE TABLE IF NOT EXISTS phone_registrations (
    tenant_id     TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    approval_id   TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    name          TEXT NOT NULL,
    platform      TEXT NOT NULL CHECK (platform IN ('ios', 'android')),
    key_storage   TEXT NOT NULL CHECK (key_storage IN ('secure_enclave', 'strongbox', 'software')),
    pubkey_spki   TEXT NOT NULL,
    enc_key_spki  TEXT NOT NULL,
    key_fp        TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'ready', 'claimed', 'rejected')),
    machine_id    TEXT,
    sealed_token  JSONB,                                      -- {v, epk, iv, ct} ; effacé au retrait
    expires_at    DOUBLE PRECISION NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS phone_registrations_account ON phone_registrations (account_id, expires_at);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'phone_registrations' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE phone_registrations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE phone_registrations FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'phone_registrations' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON phone_registrations USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('phone_registrations')
                   AND tgname = 'phone_registrations_tenant_immutable') THEN
        CREATE TRIGGER phone_registrations_tenant_immutable BEFORE UPDATE OF tenant_id ON phone_registrations
            FOR EACH ROW EXECUTE FUNCTION nokker_tenant_immutable();
    END IF;
END $$;
