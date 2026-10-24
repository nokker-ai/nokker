-- Nokker — console opérateur, lot A0 (doc interne nokker/espace-admin-2026-10.md §5-§6) : opérateurs, journal
-- d'audit, candidatures à la bêta. Horodatages TIMESTAMPTZ (lus par des humains, pas rejoués par le noyau).

CREATE TABLE IF NOT EXISTS operators (
    email       TEXT PRIMARY KEY,               -- e-mail du JWT Cloudflare Access, en minuscules
    role        TEXT NOT NULL CHECK (role IN ('owner', 'ops', 'lecteur')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ajout seul : jamais d'UPDATE ni de DELETE (aucune route ne le permet, même au rôle owner).
CREATE TABLE IF NOT EXISTS admin_audit (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,                  -- e-mail de l'opérateur, ou `system:<composant>`
    action      TEXT NOT NULL,                  -- ex. application.view, application.import, operator.seed
    object      TEXT NOT NULL DEFAULT '',       -- ex. application:42
    before      JSONB,
    after       JSONB,
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS admin_audit_at ON admin_audit (at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_object ON admin_audit (object);

CREATE TABLE IF NOT EXISTS beta_applications (
    id             BIGSERIAL PRIMARY KEY,
    source         TEXT NOT NULL DEFAULT 'front1',  -- d'où vient la ligne (tirage nokker-beta-api)
    source_id      BIGINT NOT NULL,                 -- id dans la SQLite de nokker-beta-api
    received_at    TIMESTAMPTZ NOT NULL,
    imported_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    prenom         TEXT NOT NULL DEFAULT '',
    nom            TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL,
    pays           TEXT NOT NULL DEFAULT '',
    profil         TEXT NOT NULL DEFAULT '',
    profil_detail  TEXT NOT NULL DEFAULT '',
    taches         TEXT NOT NULL DEFAULT '',
    outils         TEXT NOT NULL DEFAULT '',
    dispo          TEXT NOT NULL DEFAULT '',
    appareils      TEXT NOT NULL DEFAULT '',
    connu_par      TEXT NOT NULL DEFAULT '',        -- champ « source » du formulaire (« nous a connus par »)
    lang           TEXT NOT NULL DEFAULT 'fr',
    utm            TEXT NOT NULL DEFAULT '',
    referrer       TEXT NOT NULL DEFAULT '',
    consent_at     TIMESTAMPTZ,
    statut         TEXT NOT NULL DEFAULT 'recue',
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS beta_applications_email ON beta_applications (email);
CREATE INDEX IF NOT EXISTS beta_applications_received ON beta_applications (received_at DESC);
