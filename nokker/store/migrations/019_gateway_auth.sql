-- Connexion à la passerelle (docs/LOKKAL.md §7.3, nokker/cloud/gateway_auth.py) : codes d'autorisation (60 s, usage
-- unique) et jetons de renouvellement du relais (rotation, famille révocable). HORS RLS comme lokkal_machines : on y
-- cherche par empreinte avant de connaître le locataire. Empreintes sha256 seulement. Rejouée à chaque démarrage.
CREATE TABLE IF NOT EXISTS lokkal_gateway_codes (
    code_sha256  text PRIMARY KEY,
    account_id   text NOT NULL,
    tenant_id    text NOT NULL,
    machine_id   text NOT NULL,
    host         text NOT NULL,
    challenge    text NOT NULL,
    expires_at   double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS lokkal_gateway_refresh (
    token_sha256 text PRIMARY KEY,
    family       text NOT NULL,
    account_id   text NOT NULL,
    tenant_id    text NOT NULL,              -- effacement du compte : `nokker erase` la couvre par tenant_id
    machine_id   text NOT NULL,
    host         text NOT NULL,
    expires_at   double precision NOT NULL,
    used_at      double precision,
    revoked_at   double precision
);
CREATE INDEX IF NOT EXISTS lokkal_gateway_refresh_family ON lokkal_gateway_refresh (family);
