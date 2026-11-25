-- Nokker — le téléphone en nœud maison (P10a, docs/LOKKAL-PHONE.md §4.4, nokker/cloud/phone_home.py). Additive.
-- 1. `phone_tasks.origin` : l'origine d'une tâche (tour, membre du foyer qui l'a demandée, décision maison qui porte
--    une action) ; jusqu'ici en mémoire seulement. Défaut : un tour, comme avant.
-- 2. `home_journal` : le journal « qui, quoi, quand » de la maison au cloud (nokker/home/source.py::HomeJournal), par
--    locataire, au lieu du fichier `home-audit.jsonl` de Lokkal. `nokker erase` le couvre (tenant_id).

ALTER TABLE phone_tasks ADD COLUMN IF NOT EXISTS origin JSONB NOT NULL DEFAULT '{"kind": "turn"}';

-- 3. Correctif de 077 : `phone_nodes.key_storage` refusait `strongbox` (clé Android), que l'inscription (078) et le
--    noyau acceptent ; un téléphone Android à clé StrongBox échouait à la création de son nœud.
ALTER TABLE phone_nodes DROP CONSTRAINT IF EXISTS phone_nodes_key_storage_check;
ALTER TABLE phone_nodes ADD CONSTRAINT phone_nodes_key_storage_check
    CHECK (key_storage IN ('secure_enclave', 'strongbox', 'software'));

CREATE TABLE IF NOT EXISTS home_journal (
    tenant_id   TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    id          BIGSERIAL PRIMARY KEY,
    account_id  TEXT NOT NULL,                                  -- le compte dont c'est la maison (le nœud)
    at          DOUBLE PRECISION NOT NULL,
    record      JSONB NOT NULL                                  -- {event, who, what, outcome, …} ; jamais de jeton
);
CREATE INDEX IF NOT EXISTS home_journal_account ON home_journal (account_id, at);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'home_journal' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE home_journal ENABLE ROW LEVEL SECURITY;
        ALTER TABLE home_journal FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'home_journal' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON home_journal USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('home_journal')
                   AND tgname = 'home_journal_tenant_immutable') THEN
        CREATE TRIGGER home_journal_tenant_immutable BEFORE UPDATE OF tenant_id ON home_journal
            FOR EACH ROW EXECUTE FUNCTION nokker_tenant_immutable();
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
        GRANT SELECT, INSERT, DELETE ON home_journal TO nokker_app;
        GRANT USAGE, SELECT ON SEQUENCE home_journal_id_seq TO nokker_app;
    END IF;
END $$;
