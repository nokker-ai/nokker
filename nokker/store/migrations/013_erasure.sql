-- Effacement d'un testeur (conditions §7, docs/BACKUP-DRP.md §6 ; outil : nokker/admin/erase.py).
-- `turns` reste append-only (PLAN-P1 §2.7) : toute UPDATE échoue, et tout DELETE aussi, SAUF dans une transaction
-- d'effacement qui a posé `nokker.erasing_tenant` sur le locataire de la ligne (set_config(..., true) : local à la
-- transaction). Le noyau ne pose jamais ce réglage ; un DELETE ordinaire échoue comme avant.
CREATE OR REPLACE FUNCTION turns_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.tenant_id = current_setting('nokker.erasing_tenant', true) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'turns est append-only (fil %, tour %) : UPDATE/DELETE interdits (PLAN-P1 §2.7)',
        OLD.thread_id, OLD.turn_index;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
