-- Canaux Slack et WhatsApp du noyau (docs/CHANNELS.md, nokker/channels/). Rejouée à chaque démarrage : idempotente.
-- Numérotée 048 (après 044 à 047 du train du 2.11 ; aucune dépendance d'ordre avec elles).
--
-- Toutes HORS RLS, comme `account_identities` et `telegram_pairing` (004) : lues par le webhook AVANT de connaître le
-- locataire (c'est l'identité du canal qui le donne). Accès seulement par `nokker/channels/common.py`.

-- 1. Deux nouvelles sortes d'identité : `slack` (« <équipe>:<utilisateur> ») et `whatsapp` (wa_id). Une identité ne
--    désigne toujours qu'un seul compte (clé primaire (kind, value) inchangée).
ALTER TABLE account_identities DROP CONSTRAINT IF EXISTS account_identities_kind_check;
ALTER TABLE account_identities ADD CONSTRAINT account_identities_kind_check
    CHECK (kind IN ('device', 'telegram', 'email', 'slack', 'whatsapp'));

-- 2. Codes d'appairage par canal (même contrat que telegram_pairing : SHA-256 seul, 10 min, usage unique). Effacés
--    avec le compte (`nokker erase`, par `account_id`).
CREATE TABLE IF NOT EXISTS channel_pairing (
    channel      TEXT NOT NULL CHECK (channel IN ('slack', 'whatsapp')),
    code_sha256  TEXT NOT NULL,
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    expires_at   TIMESTAMPTZ NOT NULL,
    used_at      TIMESTAMPTZ,
    PRIMARY KEY (channel, code_sha256)
);
CREATE INDEX IF NOT EXISTS channel_pairing_account ON channel_pairing (account_id);

-- 3. Événements déjà reçus (rejeu Slack, renvoi Meta, plusieurs répliques) : identifiant du fournisseur seulement
--    (event_id Slack, wamid WhatsApp). Purgés après 2 jours par le noyau.
CREATE TABLE IF NOT EXISTS channel_events (
    channel      TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, event_id)
);
CREATE INDEX IF NOT EXISTS channel_events_received ON channel_events (received_at);

-- 4. Fenêtre de 24 h de WhatsApp : dernier message entrant par identité, rangée sous une EMPREINTE
--    (sha256("whatsapp:" || wa_id)) — aucun numéro de téléphone ici.
CREATE TABLE IF NOT EXISTS channel_windows (
    channel          TEXT NOT NULL,
    identity_sha256  TEXT NOT NULL,
    last_inbound_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (channel, identity_sha256)
);
