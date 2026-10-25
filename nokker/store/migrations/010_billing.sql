-- Nokker — abonnements Stripe (nokker/billing). `subscriptions` est une table de contenu : RLS activée ET forcée, comme
-- tenant_usage (007). `billing_customers` et `billing_events` sont HORS RLS par conception : le webhook Stripe arrive sans
-- locataire ; il retrouve le locataire par `stripe_customer` (billing_customers) avant d'entrer dans son tenant_scope, et
-- l'idempotence par `event_id` (billing_events) précède cette résolution. Aucune donnée de carte ici : Stripe les garde.

CREATE TABLE IF NOT EXISTS billing_customers (
    stripe_customer TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL UNIQUE,
    account_id      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS billing_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    tenant_id            TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    stripe_subscription  TEXT NOT NULL,
    stripe_customer      TEXT NOT NULL,
    plan                 TEXT NOT NULL,
    status               TEXT NOT NULL,
    current_period_end   TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
    last_event_at        TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, stripe_subscription)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'subscriptions' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'subscriptions' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON subscriptions
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
