-- Code stable du motif d'une carte d'approbation (`Approval.reason_code`, ex. `accept_edits_write`, `ask_rule`) : posé
-- à côté du texte `reason`, pour que les clients ne reconnaissent plus ce texte. NULL pour une carte d'avant ou sans
-- code (juge, invariants : `rule` les distingue déjà). Colonne nullable, idempotente (IF NOT EXISTS), rejouée à chaque
-- démarrage ; la table `approvals` garde sa RLS par `tenant_id`. Rien n'est rangé dans `arguments`.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS reason_code text;
