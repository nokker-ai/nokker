-- Délégation web → Lokkal par besoin (lot X21, docs/LOKKAL.md §6.7 et §7.16, nokker/cloud/handoff.py). Trois tables,
-- toutes lues et écrites en connaissant le locataire (la machine authentifiée, ou le compte), donc sous RLS activée
-- ET forcée par tenant_id (docs/TENANCY.md §4), comme lokkal_machine_load (020) ; effacées par `nokker erase`
-- (tenant_id). Pas de clé étrangère. Rejouée à chaque démarrage. Numérotée 025 : 023 et 024 sont peut-être prises par
-- d'autres branches.
--
-- 1. Inventaire des ressources `local` de chaque machine, PAR NOMS SEULEMENT (dépôts, hôtes, connecteurs, serveurs
--    MCP) : jamais un chemin, un contenu ni un identifiant (`clean_resources` jette ce qui n'est pas un nom). Une
--    ligne courante par machine, pas d'historique.
CREATE TABLE IF NOT EXISTS lokkal_machine_resources (
    machine_id   text PRIMARY KEY,
    tenant_id    text NOT NULL DEFAULT current_setting('app.tenant_id'),
    resources    jsonb NOT NULL,               -- {v, repos[], hosts[], connectors[], mcp[]}
    at           double precision NOT NULL     -- heure de nokker.ai à la réception (secondes epoch)
);

-- 2. Demandes de relais : proposées par une session web, acceptées par l'utilisateur seul, exécutées par une session
--    Lokkal de la machine choisie. `reply` = la réponse finale de la session Lokkal (caviardée, ≤ 8 000 car.), rien
--    d'autre de la machine.
CREATE TABLE IF NOT EXISTS lokkal_handoffs (
    id                 text PRIMARY KEY,
    tenant_id          text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id         text NOT NULL,
    web_thread         text NOT NULL,
    kind               text NOT NULL,           -- repo | host | connector | mcp
    name               text NOT NULL,
    task               text NOT NULL,
    machine_id         text NOT NULL,
    machine_name       text NOT NULL,
    state              text NOT NULL,           -- proposed | accepted | waiting | running | awaiting_approval | done |
                                                -- failed | refused | expired
    created_at         double precision NOT NULL,
    updated_at         double precision NOT NULL,
    online             boolean NOT NULL DEFAULT false,
    lokkal_thread      text,
    reply              text,
    error              text,
    turn_index         integer NOT NULL DEFAULT -1,
    pending_approvals  integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS lokkal_handoffs_account ON lokkal_handoffs (tenant_id, account_id, created_at);
CREATE INDEX IF NOT EXISTS lokkal_handoffs_waiting ON lokkal_handoffs (machine_id) WHERE state = 'waiting';

-- 3. Notes de mémoire en miroir (§6.3, §6.7) : seulement les notes `both` (de l'utilisateur, ou d'un projet mis en
--    miroir) ; une note `local` est refusée à l'arrivée même si une machine l'envoie.
CREATE TABLE IF NOT EXISTS lokkal_mirror_notes (
    machine_id    text NOT NULL,
    project       text NOT NULL,              -- u-local | p-<16 hex>
    name          text NOT NULL,
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    kind          text NOT NULL,
    scope         text NOT NULL,
    locality      text NOT NULL,
    title         text NOT NULL,
    text          text NOT NULL,
    created       text NOT NULL,
    derived_from  text NOT NULL,
    updated_at    double precision NOT NULL,
    PRIMARY KEY (machine_id, project, name)
);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['lokkal_machine_resources', 'lokkal_handoffs', 'lokkal_mirror_notes'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
END $$;
