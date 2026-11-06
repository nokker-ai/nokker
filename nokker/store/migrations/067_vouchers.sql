-- Nokker — vouchers d'essai d'un mois (nokker/billing/voucher.py, docs/VOUCHERS.md).
-- `vouchers` et `voucher_uses` sont HORS RLS par conception, comme `billing_events` (010) : un code appartient à une
-- CAMPAGNE, pas à un locataire, et il est lu avant que le locataire ait un abonnement ; l'unicité « un essai par adresse,
-- toutes campagnes confondues » est globale (sous RLS, un locataire ne verrait pas les usages des autres et l'index
-- unique ne protégerait plus rien). Règle de 058 respectée : une table hors RLS ne porte PAS de `tenant_id` ; le
-- locataire et l'adresse n'y sont que des empreintes HMAC salées (`tenant_key`, `email_hash`), le webhook retrouve un
-- usage par sa session Checkout ou son abonnement Stripe. Aucun contenu ;
-- lecture et écriture par le service billing (connexion d'administration) et par la console (agrégats seulement).
-- Idempotente : rejouée à chaque démarrage.

CREATE TABLE IF NOT EXISTS vouchers (
    code        TEXT PRIMARY KEY,                                   -- normalisé : majuscules, A-Z 0-9 et tirets
    campaign    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('shared', 'single')),
    max_uses    INTEGER CHECK (max_uses IS NULL OR max_uses > 0),   -- NULL = illimité (partagé) ; 1 = code unique
    expires_at  TIMESTAMPTZ NOT NULL,                               -- dernière date pour UTILISER le code
    months      INTEGER NOT NULL DEFAULT 1 CHECK (months = 1),      -- un mois d'essai, seule durée voulue
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS vouchers_campaign ON vouchers (campaign);

CREATE TABLE IF NOT EXISTS voucher_uses (
    id                  BIGSERIAL PRIMARY KEY,
    code                TEXT NOT NULL REFERENCES vouchers (code),
    tenant_key          TEXT NOT NULL,       -- HMAC(sel, locataire) : « un essai par compte » sans nommer le locataire
    email_hash          TEXT NOT NULL,
    plan                TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('reserved', 'trialing', 'converted', 'canceled', 'expired')),
    checkout_session    TEXT,
    reserved_until      TIMESTAMPTZ,
    stripe_subscription TEXT,
    stripe_customer     TEXT,                -- cus_… (rappels : retrouve le locataire par billing_customers, clé)
    trial_end           TIMESTAMPTZ,
    card_fingerprint    TEXT,                -- point d'accroche anti-abus (même carte, deux adresses) : lu, jamais bloquant
    reminded_23         TIMESTAMPTZ,
    reminded_28         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Un usage vivant par locataire et par adresse ; une réservation expirée ne bloque plus personne.
CREATE UNIQUE INDEX IF NOT EXISTS voucher_uses_tenant ON voucher_uses (tenant_key) WHERE state <> 'expired';
CREATE UNIQUE INDEX IF NOT EXISTS voucher_uses_email ON voucher_uses (email_hash) WHERE state <> 'expired';
CREATE INDEX IF NOT EXISTS voucher_uses_code ON voucher_uses (code);
CREATE INDEX IF NOT EXISTS voucher_uses_session ON voucher_uses (checkout_session);
CREATE INDEX IF NOT EXISTS voucher_uses_subscription ON voucher_uses (stripe_subscription);

-- Fin d'essai de l'abonnement (affichée dans Mon compte) ; `subscriptions` reste sous RLS (010).
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS trial_end TIMESTAMPTZ;
