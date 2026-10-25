-- Délégation web → Lokkal (X21, docs/LOKKAL.md §7.16) : essais d'ouverture de session ratés faute de passerelle.
-- Borne les relances d'une demande en attente (revue du train du 2.11, défaut 3) : au-delà, la demande passe en
-- `failed`. RLS de lokkal_handoffs inchangée (025). Colonne NOT NULL à valeur constante : instantané sur Postgres ≥ 11.
-- Rejouée à chaque démarrage.
ALTER TABLE lokkal_handoffs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
