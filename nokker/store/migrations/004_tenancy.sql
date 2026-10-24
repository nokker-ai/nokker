-- Nokker — multi-locataire, socle (bêta, 21.10.2026 ; doc interne nokker/scope-v1-2026-10.md §3 étape 2, §14).
-- Bêta : un compte = un locataire. Identités = comment on reconnaît le compte à l'entrée (appareil enrôlé, id Telegram,
-- e-mail d'invitation). Le tenant_id des tables de contenu et la RLS viennent dans les migrations suivantes.

CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,                       -- tn_ + 12 hex
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'closed'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id             TEXT PRIMARY KEY,                    -- us_ + 12 hex (= Turn.sender, device.user)
    tenant_id      TEXT NOT NULL REFERENCES tenants(id),
    email          TEXT NOT NULL UNIQUE,                -- en minuscules
    lang           TEXT NOT NULL DEFAULT 'fr',
    invitation_id  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'closed'))
);
CREATE INDEX IF NOT EXISTS accounts_tenant ON accounts (tenant_id);

CREATE TABLE IF NOT EXISTS account_identities (
    kind        TEXT NOT NULL CHECK (kind IN ('device', 'telegram', 'email')),
    value       TEXT NOT NULL,
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, value)                            -- une identité ne désigne jamais deux comptes
);
CREATE INDEX IF NOT EXISTS account_identities_account ON account_identities (account_id);

-- Appairage Telegram (contrat C4) : code court à usage unique, valable 10 min, émis pour un compte depuis l'app web,
-- consommé par `/start <code>` sur le bot de la bêta. Hors RLS comme les identités (lu avant de connaître le locataire).
CREATE TABLE IF NOT EXISTS telegram_pairing (
    code_sha256  TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    expires_at   TIMESTAMPTZ NOT NULL,
    used_at      TIMESTAMPTZ
);
