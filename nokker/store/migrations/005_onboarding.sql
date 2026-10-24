-- Nokker — intégration d'un testeur invité (bêta, 22.10.2026 ; doc interne nokker/scope-v1-2026-10.md §3 étape 5).
-- Les invitations et les candidatures vivent sur front1 (Suisse), pas ici : cette base ne garde que ce qui naît
-- côté plateforme — la preuve d'acceptation des conditions et les identifiants chiffrés des connecteurs.

-- Acceptation des conditions de la bêta : version (SHA-256 du texte affiché), date, IP, langue. Ajout seul côté
-- application (jamais d'UPDATE) ; ni clé étrangère ni trigger : l'effacement du compte (conditions §7) doit pouvoir
-- supprimer ces lignes. `invitation_id` = id de l'invitation sur front1 (autre base, donc pas de FK).
CREATE TABLE IF NOT EXISTS terms_acceptances (
    id                    BIGSERIAL PRIMARY KEY,
    account_id            TEXT NOT NULL,
    invitation_id         TEXT,
    terms_version_sha256  TEXT NOT NULL,
    lang                  TEXT NOT NULL,
    ip                    TEXT,
    accepted_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS terms_acceptances_account ON terms_acceptances (account_id);

-- Identifiants des connecteurs (mail IMAP/SMTP, agenda). `secret_enc` = jeton Fernet (clé NOKKER_CONNECTOR_KEY, hors
-- base) ; `settings` ne contient JAMAIS de secret (hôtes, ports, adresse du compte, URL CalDAV sans mot de passe).
-- Un seul connecteur par (compte, nature) en bêta.
CREATE TABLE IF NOT EXISTS connector_credentials (
    account_id   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('mail', 'calendar')),
    settings     JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret_enc   BYTEA NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at   TIMESTAMPTZ,
    PRIMARY KEY (account_id, kind)
);
