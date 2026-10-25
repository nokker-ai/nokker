-- Packs d'agents sur nokker.ai et origine des agents (docs/en/packs.md, docs/LOKKAL.md §7.30, nokker/webpacks/).
-- Rejouée à chaque démarrage (apply_migrations ET nokker/agents/pg.py) : chaque étape vérifie l'état avant d'agir ;
-- une table pas encore créée est traitée au passage suivant. Pas de clé étrangère. Numérotée 036 (réservée à ce lot).
--
-- 1. Agents : `origin` = {pack, template} quand l'agent a été créé depuis la carte d'un pack (local et web) ; c'est ce
--    que `pack_remove` relit pour proposer de désarmer ou supprimer les agents d'un pack retiré. NULL sinon.
DO $$
BEGIN
    IF to_regclass('nokker_agents') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'nokker_agents'::regclass
                       AND attname = 'origin' AND NOT attisdropped) THEN
        ALTER TABLE nokker_agents ADD COLUMN origin JSONB;
    END IF;
END
$$;

-- 2. Packs installés par un compte chez nokker.ai : une ligne par (compte, pack). Le CONTENU des compétences n'est pas
--    copié : il reste celui livré avec le noyau (nokker/packs/), relu à chaque lecture et comparé à l'empreinte prise
--    à l'installation, après la garde des compétences (`skills` = [{family, name, sha256}]). `templates` : modèles
--    d'agents installables au web ; `machine` : ce qui reste pour une machine liée (compétences, modèles, raisons) ;
--    `proposed` : cartes agent_draft posées ({template, bubble_id}), fermées au retrait. Sous RLS activée ET forcée par
--    tenant_id (docs/TENANCY.md §4) ; effacée par `nokker erase` (tenant_id).
CREATE TABLE IF NOT EXISTS nokker_pack_installs (
    account_id    text NOT NULL,
    pack          text NOT NULL,
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    version       integer NOT NULL,
    skills        jsonb NOT NULL DEFAULT '[]'::jsonb,
    templates     jsonb NOT NULL DEFAULT '[]'::jsonb,
    machine       jsonb NOT NULL DEFAULT '{}'::jsonb,
    bind          jsonb NOT NULL DEFAULT '{}'::jsonb,
    proposed      jsonb NOT NULL DEFAULT '[]'::jsonb,
    installed_at  double precision NOT NULL,
    PRIMARY KEY (tenant_id, account_id, pack)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'nokker_pack_installs' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE nokker_pack_installs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE nokker_pack_installs FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'nokker_pack_installs'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nokker_pack_installs
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
