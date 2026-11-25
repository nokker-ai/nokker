-- 086 : type de note « preference » dans la mémoire longue (ticket T6, docs/mem-dedup.md). Avant, une préférence de
-- la personne tombait en « feedback » (affiché « retour »). Idempotente : rejouable. Les notes d'équipe (023,
-- team_project_notes) ne changent pas : une préférence est personnelle, elle ne voyage pas à l'équipe.
DO $$
DECLARE
    c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE contype = 'c' AND conrelid = 'nokker_long_memory'::regclass
               AND pg_get_constraintdef(oid) LIKE '%feedback%' AND pg_get_constraintdef(oid) NOT LIKE '%preference%' LOOP
        EXECUTE format('ALTER TABLE nokker_long_memory DROP CONSTRAINT %I', c.conname);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'nokker_long_memory_kind_v2') THEN
        ALTER TABLE nokker_long_memory ADD CONSTRAINT nokker_long_memory_kind_v2
            CHECK (kind IN ('rule', 'preference', 'feedback', 'fact'));
    END IF;
END $$;
