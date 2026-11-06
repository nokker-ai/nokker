-- Agent de paiement, émetteur `wise` (docs/PAY-AGENT.md §10, drapeau `pay_agent`, éteint par défaut). Migration 064.
-- Idempotente : rejouée à chaque démarrage (IF NOT EXISTS ; droits accordés seulement si le rôle existe).
--
-- Même régime que 063 : tables de l'agent SEUL (rôle `nokker_pay_agent`), hors RLS par conception, AUCUN droit pour
-- `nokker_app`. Aucune colonne ne porte de PAN, de CVV ni de date d'expiration :
--   pay_agent_cards : UNE carte virtuelle par mandat (UNIQUE mandate_id) ; `card_ref` = cardToken Wise, `vault_ref` et
--                     `vault_sealed` = références et chiffrés du coffre (Evervault : `ev:encrypted:…`, illisibles ici).
--   pay_agent_3ds   : défis 3-D Secure délégués (un par référence de transaction et par carte), décision de la
--                     personne (passkey) et appareil qui l'a signée — journal de la SCA déléguée.
--   pay_agent_links : compte émetteur ouvert PAR L'AGENT pour un compte Nokker (profil Wise, revue KYC, état) ; seul
--                     profil que ce compte peut enregistrer comme moyen de paiement. Aucune donnée d'identité gardée.
-- pay_agent_methods gagne `issuer` (stripe_spt | wise) et `holder_name` (nom du titulaire des cartes Wise).
ALTER TABLE pay_agent_methods ADD COLUMN IF NOT EXISTS issuer text NOT NULL DEFAULT 'stripe_spt';
ALTER TABLE pay_agent_methods ADD COLUMN IF NOT EXISTS holder_name text;

CREATE TABLE IF NOT EXISTS pay_agent_links (
    account      text PRIMARY KEY,
    profile_ref  text NOT NULL UNIQUE,
    review_id    text,
    state        text NOT NULL,
    created_at   double precision NOT NULL,
    updated_at   double precision NOT NULL,
    required_by  text
);

CREATE TABLE IF NOT EXISTS pay_agent_cards (
    id                  text PRIMARY KEY,
    mandate_id          text NOT NULL UNIQUE,
    mandate_sha256      text NOT NULL,
    account             text NOT NULL,
    issuer              text NOT NULL,
    profile_ref         text NOT NULL,
    card_ref            text NOT NULL UNIQUE,
    vault_ref           text NOT NULL,
    merchant_host       text NOT NULL,
    currency            text NOT NULL,
    max_amount_minor    bigint NOT NULL,
    expires_at          bigint NOT NULL,
    status              text NOT NULL,
    created_at          double precision NOT NULL,
    updated_at          double precision NOT NULL,
    used                boolean NOT NULL DEFAULT false,
    deactivated_reason  text,
    kernel_ref          text NOT NULL DEFAULT '',
    vault_sealed        jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS pay_agent_cards_account_idx ON pay_agent_cards (account, status);

CREATE TABLE IF NOT EXISTS pay_agent_3ds (
    id                text PRIMARY KEY,
    card_id           text NOT NULL REFERENCES pay_agent_cards (id),
    mandate_id        text NOT NULL,
    account           text NOT NULL,
    challenge_ref     text NOT NULL,
    profile_ref       text NOT NULL,
    amount_minor      bigint NOT NULL,
    currency          text NOT NULL,
    merchant_name     text NOT NULL,
    merchant_url      text,
    merchant_country  text,
    merchant_match    text NOT NULL,
    expires_at        double precision NOT NULL,
    status            text NOT NULL,
    created_at        double precision NOT NULL,
    decided_at        double precision,
    device_id         text,
    UNIQUE (card_id, challenge_ref)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['pay_agent_links', 'pay_agent_cards', 'pay_agent_3ds'] LOOP
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
            EXECUTE format('REVOKE ALL ON %I FROM nokker_app', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_pay_agent') THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO nokker_pay_agent', t);
        END IF;
    END LOOP;
END $$;
