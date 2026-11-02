-- Paiements « l'agent prépare, l'humain paie » (docs/PAYMENTS.md, drapeau `payments`, éteint par défaut).
-- Numéro 056 (050 sur la branche, renumérotée à l'intégration du train 0311) ; avant actor_tenant (la dernière).
-- Idempotente : rejouée à chaque démarrage (IF NOT EXISTS, politique vérifiée avant création).
--
-- pay_mandates : un mandat par carte « à payer » (construit à la demande, scellé par la passkey à l'approbation).
-- pay_budgets  : plafonds de dépense d'un membre (par achat, par jour, par mois), posés par le payeur du foyer.
-- Contenu d'un compte : RLS par `tenant_id` comme les cartes, effacé avec le locataire.
CREATE TABLE IF NOT EXISTS pay_mandates (
    tenant_id       text NOT NULL DEFAULT current_setting('app.tenant_id'),
    id              text NOT NULL,
    approval_id     text NOT NULL,
    account         text,
    approver        text,
    status          text NOT NULL,
    amount_minor    bigint NOT NULL,
    currency        text NOT NULL,
    merchant        text NOT NULL,
    created_at      double precision NOT NULL,
    valid_until     double precision NOT NULL,
    mandate         jsonb NOT NULL,
    mandate_sha256  text NOT NULL,
    envelope        jsonb,
    signed_at       double precision,
    PRIMARY KEY (tenant_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS pay_mandates_approval_idx ON pay_mandates (tenant_id, approval_id);
CREATE INDEX IF NOT EXISTS pay_mandates_account_idx ON pay_mandates (tenant_id, account, created_at);

CREATE TABLE IF NOT EXISTS pay_budgets (
    tenant_id           text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account             text NOT NULL,
    currency            text NOT NULL,
    per_purchase_minor  bigint NOT NULL,
    per_day_minor       bigint NOT NULL,
    per_month_minor     bigint NOT NULL,
    minor_allowed       boolean NOT NULL DEFAULT false,
    set_by              text,
    updated_at          double precision NOT NULL,
    PRIMARY KEY (tenant_id, account)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['pay_mandates', 'pay_budgets'] LOOP
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = t::regclass) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                       AND tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
END $$;
