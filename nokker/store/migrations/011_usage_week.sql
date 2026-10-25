-- Nokker — quota hebdomadaire de l'offre Solo (nokker/tenancy/limits.py, décision produit du 22.10.2026) : 200 tours
-- par semaine, du lundi 06:00 UTC au lundi suivant. Compteur `week_turns` sur la ligne dont `day` = date du lundi ; la
-- colonne `turns` de cette même ligne reste celle du jour de quota (12:00 UTC → 12:00 UTC) qui commence ce lundi-là :
-- deux colonnes, jamais deux sens pour une même colonne. RLS de tenant_usage inchangée (migration 007).

ALTER TABLE tenant_usage ADD COLUMN IF NOT EXISTS week_turns INTEGER NOT NULL DEFAULT 0;
