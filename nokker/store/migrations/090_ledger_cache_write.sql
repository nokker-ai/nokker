-- Nokker — ledger : jetons écrits en cache et identifiant d'appel (contrat hôte, point 3c ; docs/HOST-EMBED.md §Ledger).
--
-- 1. `cache_write_tokens` : jetons du prompt ÉCRITS en cache par l'appel (amont au format Anthropic,
--    `cache_creation_input_tokens`). Déjà compris dans `prompt_tokens`, jamais dans `cached_tokens` (lus en cache).
--    Même type que les colonnes voisines (`cached_tokens` INTEGER) ; 0 pour tout appel d'avant et tout amont sans
--    écriture de cache : le coût d'une ligne existante ne change pas.
-- 2. `call_id` : identifiant de l'appel modèle posé par le noyau (`nc_` + 16 hexadécimaux), le même que l'en-tête
--    `X-Nokker-Call-Id` quand le palier le demande. NULL pour les lignes d'avant. Jamais d'identifiant de personne.
--
-- La RLS de `ledger_entries` (006, 037, 039) n'est pas touchée : deux colonnes de plus, mêmes politiques. Idempotente :
-- rejouée à chaque démarrage (`PostgresLedger.migrate` la rejoue aussi pour un ledger migré seul).
ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS call_id TEXT;
CREATE INDEX IF NOT EXISTS ledger_entries_call_id_idx ON ledger_entries (call_id) WHERE call_id IS NOT NULL;
