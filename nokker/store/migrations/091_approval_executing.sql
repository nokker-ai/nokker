-- Statut `executing` d'une carte d'approbation : entre « approuvée » et « terminée » (`executed` | `failed`), posé par
-- l'exécuteur au moment où l'outil démarre. La colonne `status` est du texte libre (aucune contrainte CHECK sur
-- `approvals`) : rien à élargir, mais on retire défensivement toute contrainte de statut qu'un déploiement aurait
-- ajoutée à la main. `executing_at` : instant du démarrage de l'exécution (NULL pour une carte d'avant ou jamais
-- exécutée). Idempotente, rejouée à chaque démarrage ; RLS par `tenant_id` inchangée ; l'effacement (`nokker erase`)
-- opère par `tenant_id` sur la table entière : la colonne ne demande rien de plus.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS executing_at DOUBLE PRECISION;
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'approvals'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE approvals DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
