-- Ce que tu signes (lot X20c, docs/LOKKAL.md §7.3 et §7.12, nokker/cloud/approvals.py) : une carte forte d'un connecteur
-- RELAYÉ est déposée avec ses arguments ; nokker.ai vérifie leur empreinte contre le défi et rend lui-même le résumé
-- que la page de décision affiche. Les arguments ne sont gardés que jusqu'au statut final (ou l'expiration), puis
-- effacés (`arguments` NULL, lignes du résumé vidées) ; l'empreinte reste, pour l'appel relayé qui suit (même empreinte
-- exigée) et pour le journal. Une carte LOCALE garde `summary_source = 'machine'` (§6.7 : ses arguments ne sortent pas).
-- Même table que 021, donc même RLS forcée par tenant_id, couverte par `nokker erase`. Rejouée à chaque démarrage
-- (idempotente). Numérotée 027 : 024 est peut-être prise par une autre branche.
ALTER TABLE lokkal_relay_approvals ADD COLUMN IF NOT EXISTS args_sha256 text;
ALTER TABLE lokkal_relay_approvals ADD COLUMN IF NOT EXISTS arguments jsonb;
ALTER TABLE lokkal_relay_approvals ADD COLUMN IF NOT EXISTS summary jsonb;
ALTER TABLE lokkal_relay_approvals ADD COLUMN IF NOT EXISTS summary_source text NOT NULL DEFAULT 'machine';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lokkal_relay_approvals_summary_source') THEN
        ALTER TABLE lokkal_relay_approvals ADD CONSTRAINT lokkal_relay_approvals_summary_source
            CHECK (summary_source IN ('nokker.ai', 'machine'));
    END IF;
END $$;
-- Balayage des arguments expirés : seulement les lignes qui en portent encore.
CREATE INDEX IF NOT EXISTS lokkal_relay_approvals_held_arguments ON lokkal_relay_approvals (expires_at)
    WHERE arguments IS NOT NULL;
