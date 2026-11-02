-- Relecteur automatique des cartes `standard` (docs/APPROVALS.md §8, drapeau `auto_review`) : la relecture rangée sur
-- la carte (`{decision, reason, model, at, by_rule[, revoked_by, revoked_at]}`), NULL si aucune relecture.
-- Numéro 051 (049 sur la branche, renumérotée à l'intégration du train 0311 ; avant actor_tenant, la dernière). Rejouée à chaque démarrage (IF NOT EXISTS) ; la table
-- `approvals` garde sa RLS par `tenant_id`.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS auto_review jsonb;
