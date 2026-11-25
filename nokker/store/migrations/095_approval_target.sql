-- Cible lisible d'une carte d'approbation (`nokker/approvals/target.py`) : calculée à la création, caviardée, 200
-- caractères au plus, figée. NULL pour une carte d'avant ou un outil sans cible connue. Idempotente, rejouée à chaque
-- démarrage ; RLS par `tenant_id` inchangée ; l'effacement (`nokker erase`) opère par `tenant_id` sur la table entière.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS target TEXT;
