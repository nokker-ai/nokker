-- Agent de paiement (docs/PAY-AGENT.md, drapeau `pay_agent`, éteint par défaut). Migration 063.
-- Idempotente : rejouée à chaque démarrage (IF NOT EXISTS ; droits accordés seulement si le rôle existe).
--
-- Tables `pay_agent_*` : données de l'agent de paiement SEUL (service séparé, rôle `nokker_pay_agent`). Hors RLS par
-- conception : l'agent n'a pas de locataire, il sert tous les comptes ; aucune ne porte `tenant_id`. Le rôle du noyau
-- (`nokker_app`) n'y a AUCUN droit (révoqué ici et dans deploy/postgres/roles.sql, après son GRANT global).
--   pay_agent_methods : UN moyen de paiement par compte (pm_… préparée par le Payment Element ; aucun numéro de carte).
--   pay_agent_tokens  : un Shared Payment Token par mandat (UNIQUE mandate_id), état suivi par les webhooks Stripe.
--   pay_agent_events  : identifiants d'événements Stripe déjà traités (rejeu idempotent).
-- pay_mandates.agent : état du paiement par l'agent vu par le noyau (statut, plafond, expiration), sans numéro de SPT.
--
-- `nokker_pay_agent` lit les clés des passkeys dans `devices` : SELECT sur 4 colonnes SEULEMENT (id, "user",
-- public_key_spki, status), jamais `code` ni `telegram_user_id`, aucune écriture. `devices` est hors RLS par conception
-- (lue avant le locataire, docs/TENANCY.md §4) : la restriction est le GRANT par colonne.
CREATE TABLE IF NOT EXISTS pay_agent_methods (
    account         text PRIMARY KEY,
    payment_method  text NOT NULL,
    kind            text NOT NULL,
    brand           text,
    last4           text,
    exp_month       integer,
    exp_year        integer,
    device_id       text NOT NULL,
    created_at      double precision NOT NULL
);

CREATE TABLE IF NOT EXISTS pay_agent_tokens (
    id                  text PRIMARY KEY,
    mandate_id          text NOT NULL UNIQUE,
    mandate_sha256      text NOT NULL,
    account             text NOT NULL,
    spt_id              text NOT NULL UNIQUE,
    status              text NOT NULL,
    seller_profile      text NOT NULL,
    currency            text NOT NULL,
    max_amount_minor    bigint NOT NULL,
    expires_at          bigint NOT NULL,
    created_at          double precision NOT NULL,
    updated_at          double precision NOT NULL,
    used                boolean NOT NULL DEFAULT false,
    deactivated_reason  text,
    kernel_ref          text NOT NULL DEFAULT ''   -- rendu au noyau dans les rappels (locataire du mandat), opaque
);
CREATE INDEX IF NOT EXISTS pay_agent_tokens_account_idx ON pay_agent_tokens (account, status);

CREATE TABLE IF NOT EXISTS pay_agent_events (
    id           text PRIMARY KEY,
    received_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE pay_mandates ADD COLUMN IF NOT EXISTS agent jsonb;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['pay_agent_methods', 'pay_agent_tokens', 'pay_agent_events'] LOOP
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
            EXECUTE format('REVOKE ALL ON %I FROM nokker_app', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_pay_agent') THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO nokker_pay_agent', t);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_pay_agent') AND to_regclass('devices') IS NOT NULL THEN
        GRANT SELECT (id, "user", public_key_spki, status) ON devices TO nokker_pay_agent;
    END IF;
END $$;
