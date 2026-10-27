-- Approbations fortes (P1, 18.10.2026, docs/APPROVALS.md) — tables INDÉPENDANTES du store de F2
-- (nokker/store/pg.py : threads/turns) et du ledger de F6 : thread_id/turn_index sont de simples
-- colonnes, pas de clé étrangère (même raison que 0001_ledger_entries.sql : piliers écrits en
-- parallèle, l'intégration se fait au besoin par vue jointe, pas ici). Appliquée par
-- `nokker/approvals/pg.py::PgApprovals` (idempotent : IF NOT EXISTS partout).

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL,
    turn_index      INTEGER NOT NULL,
    channel         TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    arguments       JSONB NOT NULL,
    args_sha256     TEXT NOT NULL,
    strength        TEXT NOT NULL,              -- strong | standard
    rule            TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    advice          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|expired|executed|failed
    created_at      DOUBLE PRECISION NOT NULL,
    expires_at      DOUBLE PRECISION NOT NULL,
    decided_by      TEXT,
    decided_via     TEXT,
    decided_at      DOUBLE PRECISION,
    result_summary  TEXT,
    error           TEXT
);

-- 25.10.2026 (docs/LOKKAL.md §7.4.9) : `ToolCall.id` de l'appel qui a posé la carte, repris par le `tool.result` publié
-- après l'exécution. Nullable : les cartes d'avant n'en ont pas.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS call_id TEXT;

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status);
CREATE INDEX IF NOT EXISTS idx_approvals_thread ON approvals (thread_id, turn_index);

-- Append-only (docs/APPROVALS.md §2 : « chaque transition est append-only »). Rien n'est effacé.
CREATE TABLE IF NOT EXISTS approval_events (
    id           BIGSERIAL PRIMARY KEY,
    approval_id  TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    data         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_events_approval ON approval_events (approval_id);

CREATE TABLE IF NOT EXISTS devices (
    id                 TEXT PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,     -- enrôlement humain court (`nokker devices approve <code>`)
    "user"             TEXT NOT NULL,
    name               TEXT NOT NULL,
    public_key_spki    TEXT NOT NULL,            -- SPKI DER base64 — jamais une clé privée
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|revoked
    created_at         DOUBLE PRECISION NOT NULL,
    approved_at        DOUBLE PRECISION,
    approved_via       TEXT,                     -- totp | cli
    telegram_user_id   TEXT
);

-- 27.10.2026 (docs/APPROVALS.md §3.4) : dernier `signCount` WebAuthn vu pour l'appareil (anti-clonage). NULL = jamais
-- présenté ; reste à 0 pour un passkey synchronisé (compteur toujours nul, accepté sans protection).
ALTER TABLE devices ADD COLUMN IF NOT EXISTS sign_count BIGINT;

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices ("user");

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    "user"      TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    expires_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions ("user");

-- Nonces à usage unique (défi d'approbation ET défi de connexion, `scope_id` = approval_id ou
-- "login:<device_id>") — réservé par `challenge()`/`login_challenge()`, consommé (used=true) par
-- `decide()`/`create_session()`. Une 2e consommation du même nonce échoue toujours.
CREATE TABLE IF NOT EXISTS used_nonces (
    nonce       TEXT PRIMARY KEY,
    scope_id    TEXT NOT NULL,
    exp         DOUBLE PRECISION NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_used_nonces_scope ON used_nonces (scope_id);

-- Secrets TOTP (RFC 6238) — jamais renvoyés par l'API (nokker/api/server.py ne les expose dans aucune
-- route). `secret` chiffré par `nokker.approvals.pg.PgApprovals` si `NOKKER_APPROVALS_ENC_KEY` est
-- posée (Fernet) ; en clair sinon (limite documentée, docs/APPROVALS.md §3) — jamais en clair dans le
-- dépôt ou les logs dans les deux cas. `last_step` : dernier pas TOTP consommé (code non réutilisable).
CREATE TABLE IF NOT EXISTS totp_secrets (
    "user"      TEXT PRIMARY KEY,
    secret      TEXT NOT NULL,
    encrypted   BOOLEAN NOT NULL DEFAULT FALSE,
    last_step   BIGINT
);
