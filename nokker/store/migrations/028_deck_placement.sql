-- Deck multi-machines et placement (lot X15, docs/LOKKAL.md §7.19, docs/BUBBLES.md §9.5). Rejouée à chaque démarrage
-- (apply_migrations ET nokker/agents/pg.py) : chaque étape vérifie l'état avant d'agir (aucun ALTER une fois les
-- colonnes en place) ; une table pas encore créée est traitée au passage suivant. Pas de clé étrangère.
--
-- 1. Agents : `needs` (ce que l'agent exige de sa machine, par NOMS : dépôts, hôtes, connecteurs, serveurs MCP, GPU).
--    Passages : `machine_id` (machine où nokker.ai a envoyé le passage ; NULL = il a tourné ici) et `guest` (sur la
--    machine : définition de l'agent du web dont c'est le passage — l'agent ne vit pas ici, son passage oui).
DO $$
DECLARE
    col  RECORD;
BEGIN
    IF to_regclass('nokker_agents') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_agents'::regclass
                       AND attname = 'needs' AND NOT attisdropped) THEN
        ALTER TABLE nokker_agents ADD COLUMN needs JSONB;
    END IF;
    IF to_regclass('nokker_agent_runs') IS NOT NULL THEN
        FOR col IN SELECT * FROM (VALUES ('machine_id', 'TEXT'), ('guest', 'JSONB')) AS c(name, ddl) LOOP
            IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_agent_runs'::regclass
                           AND attname = col.name AND NOT attisdropped) THEN
                EXECUTE format('ALTER TABLE nokker_agent_runs ADD COLUMN %I %s', col.name, col.ddl);
            END IF;
        END LOOP;
    END IF;
END
$$;

-- 2. Deck de chaque machine (chez nokker.ai) : ce que la machine publie d'elle-même pour le deck, PAR NOMS ET ÉTATS
--    seulement (sessions : fil, nom du dossier, état, titre seulement si le fil est en miroir ; agents : nom, état,
--    déclencheur ; nombre de cartes en attente). Jamais un texte de conversation, un chemin ou un secret
--    (`clean_deck` jette le reste). Une ligne courante par machine, pas d'historique. Lue et écrite en connaissant le
--    locataire (la machine authentifiée, ou le compte du deck), donc sous RLS activée ET forcée par tenant_id, comme
--    lokkal_machine_load (020) ; effacée par `nokker erase` (tenant_id).
CREATE TABLE IF NOT EXISTS lokkal_machine_deck (
    machine_id   text PRIMARY KEY,
    tenant_id    text NOT NULL DEFAULT current_setting('app.tenant_id'),
    deck         jsonb NOT NULL,               -- {v, sessions[], agents[], pending_approvals}
    at           double precision NOT NULL     -- heure de nokker.ai à la réception (secondes epoch)
);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'lokkal_machine_deck' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE lokkal_machine_deck ENABLE ROW LEVEL SECURITY;
        ALTER TABLE lokkal_machine_deck FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'lokkal_machine_deck'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON lokkal_machine_deck
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
