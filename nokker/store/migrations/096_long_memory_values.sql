-- Nokker — valeurs datées de la mémoire longue (nokker/memory/values.py, capture automatique à la clôture d'un épisode).
-- Additive et idempotente, rejouée à chaque démarrage par `PgLongMemoryStore` ; chaque étape vérifie l'état avant d'agir.
--
-- Une valeur (décision, préférence, réglage) est une note de `nokker_long_memory` qui porte en plus une CLÉ, sa VALEUR,
-- le fil et le tour d'où elle vient. Une valeur qui en remplace une autre (« passe de X à Y ») ARCHIVE l'ancienne :
-- `superseded_by` = nom de la note qui la remplace, `superseded_at` = quand. Une note archivée n'est jamais rappelée
-- ni consolidée ; elle reste lisible (export, historique) et part avec le compte à l'effacement (même table, même
-- `tenant_id`). L'index unique partiel garantit qu'il n'existe jamais deux valeurs VIVANTES pour la même clé d'un compte.
-- Aucune colonne nouvelle n'est requise des lignes d'avant : NULL partout, comportement inchangé.

DO $$
BEGIN
    IF to_regclass('nokker_long_memory') IS NULL THEN
        RETURN;  -- mémoire longue pas encore migrée (049) : traité au passage suivant
    END IF;
    ALTER TABLE nokker_long_memory ADD COLUMN IF NOT EXISTS value_key text;
    ALTER TABLE nokker_long_memory ADD COLUMN IF NOT EXISTS value_text text;
    ALTER TABLE nokker_long_memory ADD COLUMN IF NOT EXISTS source_thread text;
    ALTER TABLE nokker_long_memory ADD COLUMN IF NOT EXISTS source_turn integer;
    ALTER TABLE nokker_long_memory ADD COLUMN IF NOT EXISTS superseded_by text;
    ALTER TABLE nokker_long_memory ADD COLUMN IF NOT EXISTS superseded_at double precision;
    CREATE UNIQUE INDEX IF NOT EXISTS nokker_long_memory_live_key
        ON nokker_long_memory (tenant_id, account_id, value_key)
        WHERE value_key IS NOT NULL AND superseded_by IS NULL AND scope = 'user';
END
$$;

-- Journal des rappels de la mémoire longue : UNE ligne par recherche faite pour un tour (requête bornée, nombre de
-- candidates, notes rappelées avec leur score), qu'elle ait rappelé quelque chose ou non. Même isolation que la
-- mémoire (locataire ET compte, RLS forcée) ; `nokker erase` la vide avec le locataire (colonne `tenant_id`).
CREATE TABLE IF NOT EXISTS nokker_long_recall_log (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL DEFAULT current_setting('app.tenant_id'),
    account_id  text NOT NULL,
    at          double precision NOT NULL,
    thread_id   text NOT NULL DEFAULT '',
    query       text NOT NULL DEFAULT '',
    candidates  integer NOT NULL DEFAULT 0,
    injected    integer NOT NULL DEFAULT 0,
    notes       jsonb NOT NULL DEFAULT '[]'::jsonb,
    top_score   double precision
);
CREATE INDEX IF NOT EXISTS nokker_long_recall_log_acc ON nokker_long_recall_log (tenant_id, account_id, at);

DO $$
BEGIN
    IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'nokker_long_recall_log'::regclass) THEN
        ALTER TABLE nokker_long_recall_log ENABLE ROW LEVEL SECURITY;
        ALTER TABLE nokker_long_recall_log FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                   AND tablename = 'nokker_long_recall_log' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nokker_long_recall_log
            USING (tenant_id = current_setting('app.tenant_id', true)
                   AND account_id = NULLIF(current_setting('app.account_id', true), ''))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
                        AND account_id = NULLIF(current_setting('app.account_id', true), ''));
    END IF;
END
$$;
