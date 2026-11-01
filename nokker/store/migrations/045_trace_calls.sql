-- Traces d'entraînement / d'évaluation (docs/TRACES.md, nokker/traces/) : UNE ligne par appel modèle de la boucle du
-- noyau — les messages EXACTEMENT tels qu'envoyés au fournisseur (après le filet de secrets du fil), les schémas
-- d'outils de cet appel, la suite jusqu'au prochain appel (réponse, résultats d'outils) et les métadonnées du tour.
-- Écrite seulement si `traces_record: true` (défaut false), à la fin du tour, en une fois ; rétention bornée
-- (`traces_retention_days`, purge par le noyau et par `nokker traces purge`). Contenu de locataire : tenant_id par
-- défaut = app.tenant_id, RLS activée ET forcée (docs/TENANCY.md §4) ; l'effacement d'un compte la couvre (colonne
-- tenant_id, nokker/admin/erase.py lit le catalogue). Rejouée à chaque démarrage : IF NOT EXISTS partout.
CREATE TABLE IF NOT EXISTS trace_calls (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    thread_id    TEXT NOT NULL,
    turn_index   INTEGER NOT NULL,
    call_index   INTEGER NOT NULL,
    account_id   TEXT,                          -- Turn.sender (consentement d'entraînement lu à l'export)
    kernel_sha   TEXT NOT NULL DEFAULT '',
    target       TEXT NOT NULL DEFAULT '',      -- beta | canary | lokkal | unknown
    prompt       JSONB NOT NULL,
    tools        JSONB NOT NULL,
    completion   JSONB NOT NULL,
    meta         JSONB NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS trace_calls_call ON trace_calls (tenant_id, thread_id, turn_index, call_index);
CREATE INDEX IF NOT EXISTS trace_calls_recorded ON trace_calls (tenant_id, recorded_at);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'trace_calls' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE trace_calls ENABLE ROW LEVEL SECURITY;
        ALTER TABLE trace_calls FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'trace_calls' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON trace_calls
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
