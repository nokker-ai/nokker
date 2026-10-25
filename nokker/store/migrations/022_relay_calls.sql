-- Journal d'audit des connecteurs relayés (lot X20, docs/LOKKAL.md §7.12, nokker/cloud/relay.py) : une ligne par
-- appel qu'une machine Lokkal liée envoie à un connecteur posé chez nokker.ai (exécuté, en erreur ou refusé). Ni les
-- arguments ni le résultat : seulement leurs empreintes (sha256), l'outil, son effet, et la preuve de la carte locale
-- telle que la machine la décrit (`approval`). Une carte ne sert qu'une fois par machine (index unique ci-dessous, tenu
-- par la base même sous appels concurrents). Lue et écrite en connaissant le locataire (la machine authentifiée), donc
-- sous RLS activée ET forcée (docs/TENANCY.md §4) ; effacée par `nokker erase` (tenant_id). Rejouée à chaque démarrage.
-- Numérotée 022 : 021 est peut-être prise par une autre branche.
CREATE TABLE IF NOT EXISTS lokkal_relay_calls (
    audit_id       text PRIMARY KEY,
    tenant_id      text NOT NULL DEFAULT current_setting('app.tenant_id'),
    machine_id     text NOT NULL,
    account_id     text NOT NULL,
    connector      text NOT NULL,
    tool           text NOT NULL,
    effect         text NOT NULL,
    approval_id    text,                    -- carte locale qui a autorisé l'appel ; NULL pour une lecture ou un refus
    args_sha256    text NOT NULL,
    approval       jsonb,                   -- {id, args_sha256, decided_via, decided_by, decided_at, strength}
    outcome        text NOT NULL,           -- started | executed | error | refused
    code           text,                    -- motif d'un refus (approval_required, not_relayable…)
    result_sha256  text,
    at             double precision NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS lokkal_relay_calls_one_card
    ON lokkal_relay_calls (machine_id, approval_id) WHERE approval_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lokkal_relay_calls_tenant_at ON lokkal_relay_calls (tenant_id, at);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'lokkal_relay_calls' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE lokkal_relay_calls ENABLE ROW LEVEL SECURITY;
        ALTER TABLE lokkal_relay_calls FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'lokkal_relay_calls'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON lokkal_relay_calls
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
