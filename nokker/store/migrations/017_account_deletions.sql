-- Suppression d'un compte par la personne (nokker/account/delete.py, docs/CLIENT-API.md) : file d'effacement. Le
-- noyau y écrit quand la carte forte est accordée (compte et locataire déjà `suspended`) ; l'outil d'opérateur
-- (`nokker erase --pending --apply`, connexion propriétaire) efface chaque compte de la file — la ligne part avec lui
-- (colonne account_id : couverte par l'effacement). HORS RLS, comme `accounts` : lue avant de connaître un locataire.
-- Rejouée à chaque démarrage.
CREATE TABLE IF NOT EXISTS account_deletions (
    account_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    approval_id   TEXT NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
