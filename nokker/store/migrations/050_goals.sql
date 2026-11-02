-- Objectifs persistants et rubriques notées par un correcteur séparé (chantier p2/outcomes-goal, docs/OUTCOMES-GOAL.md,
-- nokker/goals/). Rejouée à chaque démarrage : idempotente. NUMÉRO 050 (049 sur la branche, renumérotée à l'intégration du train 0311 ; 048 était la dernière
-- posée) — aucune dépendance d'ordre, sinon passer AVANT actor_tenant (la dernière, qui réécrit ses politiques).
--
-- Contenu de locataire : tenant_id par défaut = app.tenant_id, RLS activée ET forcée (docs/TENANCY.md §4) ; effacé
-- par `nokker erase` (colonne tenant_id, lue dans le catalogue). Un objectif appartient à un fil ; il survit aux tours
-- et aux redémarrages (le moteur reprend au démarrage ceux qui sont actifs et menés par le noyau).

-- 1. L'objectif (ou la rubrique d'une tâche) d'un fil : document JSON (texte, rubrique, budget, dépense, points
--    d'étape, dernière note), statut et meneur en colonnes pour la reprise.
CREATE TABLE IF NOT EXISTS nokker_goals (
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    id           TEXT NOT NULL,
    thread_id    TEXT NOT NULL,
    owner        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,   -- draft | active | done | budget | blocked | stopped (vérifié par le code)
    driver       TEXT NOT NULL,   -- kernel | client
    doc          JSONB NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS nokker_goals_thread ON nokker_goals (tenant_id, thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS nokker_goals_live ON nokker_goals (tenant_id, status, driver);

-- 2. Chaque note du correcteur (tracée, ajout seul) : critère par critère avec la justification.
CREATE TABLE IF NOT EXISTS nokker_goal_grades (
    seq          BIGSERIAL,
    tenant_id    TEXT NOT NULL DEFAULT current_setting('app.tenant_id'),
    goal_id      TEXT NOT NULL,
    thread_id    TEXT NOT NULL,
    turn_index   INTEGER,
    pass         INTEGER NOT NULL DEFAULT 0,
    grade        JSONB NOT NULL,
    at           DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, seq)
);
CREATE INDEX IF NOT EXISTS nokker_goal_grades_goal ON nokker_goal_grades (tenant_id, goal_id, seq);

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['nokker_goals', 'nokker_goal_grades'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = t AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) '
                           'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        END IF;
    END LOOP;
END $$;
