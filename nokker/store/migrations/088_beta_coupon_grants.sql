-- Nokker — coupon bêta PAR COMPTE (C16, décision du 15.11.2026 ; nokker/billing/beta_coupon.py, docs/LOKKAL.md).
-- Remplace le coupon global `billing_coupon` (posé sur CHAQUE Checkout) : « Solo offert 6 mois » ne vaut plus que pour
-- un compte rendu éligible À LA MAIN par l'opérateur, au moment où il envoie l'invitation (console). Une ligne par
-- invitation front1 ; le compte né de cette invitation (`accounts.invitation_id`) est le seul à pouvoir s'en servir.
--
-- HORS RLS par conception, comme `vouchers` (067) et `billing_events` (010) : la ligne naît AVANT le compte et son
-- locataire (à l'envoi de l'invitation), et le webhook la consomme sans locataire. Règle de 058 respectée : pas de
-- `tenant_id` ; la clé est l'id de l'invitation front1 (pas une donnée personnelle), le webhook retrouve la ligne par sa
-- session Checkout. Aucun contenu.
--
-- Cycle : granted → reserved (Checkout Solo créé avec la remise, `checkout_session`) → consumed (webhook
-- `checkout.session.completed` de CETTE session, une seule fois) ; `checkout.session.expired` rend la réservation
-- (reserved → granted). `revoked` : retirée par l'opérateur avant usage. Idempotente : rejouée à chaque démarrage.

CREATE TABLE IF NOT EXISTS beta_coupon_grants (
    invitation_id    TEXT PRIMARY KEY,                                   -- id d'invitation front1 (pas de FK), ou
                                                                         -- `account:<us_…>` posé après coup (console)
    plan             TEXT NOT NULL DEFAULT 'solo' CHECK (plan = 'solo'), -- offre Solo seulement
    months           INTEGER NOT NULL DEFAULT 6 CHECK (months = 6),     -- durée portée par le coupon Stripe (repeating 6)
    state            TEXT NOT NULL DEFAULT 'granted'
                     CHECK (state IN ('granted', 'reserved', 'consumed', 'revoked')),
    checkout_session TEXT,                                               -- cs_… de la dernière réservation
    reserved_until   TIMESTAMPTZ,
    consumed_event   TEXT,                                               -- evt_… qui l'a consommée (rejeu : rien)
    consumed_at      TIMESTAMPTZ,
    granted_by       TEXT NOT NULL,                                      -- opérateur (adresse Access), journalisé aussi
    granted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS beta_coupon_grants_session ON beta_coupon_grants (checkout_session);
