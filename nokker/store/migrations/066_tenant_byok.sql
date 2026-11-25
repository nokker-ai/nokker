-- Nokker — BYOK cloud (docs/CLOUD-BYOK.md, nokker/byok/) : le modèle par défaut d'un locataire de nokker.ai et ses
-- points d'accès compatibles OpenAI. Une ligne par locataire. AUCUNE clé ici : la clé vit au coffre du noyau (portée
-- system, nom `byok.<tn_…>.<nom>`) ; `entries` = [{name, base_url, model, ok, models}]. Même forme que les tables de
-- contenu (docs/TENANCY.md §4) : tenant_id par défaut = app.tenant_id, RLS activée ET forcée.
CREATE TABLE IF NOT EXISTS tenant_byok (
    tenant_id      TEXT PRIMARY KEY DEFAULT current_setting('app.tenant_id'),
    default_model  TEXT NOT NULL DEFAULT 'router',          -- router | byok:<nom>
    entries        JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'tenant_byok' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE tenant_byok ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_byok FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'tenant_byok' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON tenant_byok
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;

-- Tours servis par le point d'accès du client : comptés à part (garde de débit), jamais dans le quota du Router
-- (`turns`, `week_turns`). Colonne de plus dans tenant_usage (007) : RLS inchangée.
ALTER TABLE tenant_usage ADD COLUMN IF NOT EXISTS byok_turns INTEGER NOT NULL DEFAULT 0;
