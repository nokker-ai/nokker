-- Nokker Lokkal, côté nokker.ai (docs/LOKKAL.md §7.2 révisé, §7.5 ; nokker/cloud/link.py, nokker/cloud/pg.py).
-- Machines liées à un compte et flux d'appareil en cours. HORS RLS, comme `devices` : on y cherche par empreinte de
-- jeton ou par code, avant de connaître le locataire. Aucun jeton en clair : seulement des empreintes sha256.
CREATE TABLE IF NOT EXISTS lokkal_machines (
    id            text PRIMARY KEY,
    account_id    text NOT NULL,
    tenant_id     text NOT NULL,              -- effacement du compte : `nokker erase` la couvre par tenant_id
    name          text NOT NULL,
    host          text NOT NULL UNIQUE,       -- <nom>.nokker.app
    token_sha256  text NOT NULL DEFAULT '',   -- vide jusqu'à ce que Lokkal vienne chercher son jeton
    created_at    double precision NOT NULL,
    revoked_at    double precision
);
CREATE INDEX IF NOT EXISTS lokkal_machines_token ON lokkal_machines (token_sha256) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS lokkal_machines_account ON lokkal_machines (account_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS lokkal_link_requests (
    device_sha256 text PRIMARY KEY,
    user_code     text NOT NULL,
    machine_name  text NOT NULL,
    expires_at    double precision NOT NULL,
    state         text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'denied')),
    machine_id    text
);
CREATE INDEX IF NOT EXISTS lokkal_link_requests_code ON lokkal_link_requests (user_code) WHERE state = 'pending';
