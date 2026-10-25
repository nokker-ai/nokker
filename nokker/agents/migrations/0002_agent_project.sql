-- Agent rattaché à un projet d'équipe (lot X17, docs/BUBBLES.md §9.5 champ `project`, docs/LOKKAL.md §7.12).
-- NULL = aucun projet : le comportement d'avant. Rejouée à chaque démarrage : la colonne n'est ajoutée qu'une fois
-- (aucun ALTER, donc aucun verrou exclusif, quand elle existe déjà).
DO $$
BEGIN
    IF to_regclass('nokker_agents') IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_agents'::regclass AND attname = 'project'
            AND NOT attisdropped) THEN
        ALTER TABLE nokker_agents ADD COLUMN project TEXT;
    END IF;
END
$$;
