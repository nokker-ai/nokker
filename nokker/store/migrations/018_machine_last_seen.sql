-- Écran « mes machines » (onboarding, docs/LOKKAL.md §7.2) : dernier contrôle du relais (GET /v1/gateway/machine).
-- Hors RLS comme lokkal_machines (014). Numéro 018 : 016 et 017 sont pris par p2/account (exports, suppressions).
-- Rejouée à chaque démarrage.
ALTER TABLE lokkal_machines ADD COLUMN IF NOT EXISTS last_seen_at DOUBLE PRECISION;
