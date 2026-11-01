-- Nokker — base CortHeXis de l'ANCIENNE forme (tables, schéma et base à nokker_app) rendue au rôle de migration
-- nokker_owner, pour `corthexis_schema: external` (docs/MEMORY-TENANCY.md §5.1). Idempotent : rejouable sans danger.
--
-- En bêta, l'étape `corthexis-owner` du job migrate du chart Helm fait l'équivalent (avant `nokker migrate`) ; ce
-- fichier sert hors chart (copie de restauration, recette, autre installation).
--
-- Pourquoi : la RLS forcée ne protège pas d'un propriétaire compromis (ALTER TABLE … DISABLE ROW LEVEL SECURITY), ni le
-- schéma d'un rôle qui le possède (DROP de n'importe quelle table). Après ce fichier, nokker_app ne possède plus rien.
--
-- Qui : un administrateur connecté à la base CortHeXis (psql -d corthexis), membre de nokker_app ET de nokker_owner
-- (sur un Postgres managé : `GRANT nokker_app, nokker_owner TO <admin>` d'abord, retirés après) et CREATEDB (pour la
-- base). ACCESS EXCLUSIVE sur chaque table, le temps d'un changement de catalogue : pod du noyau arrêté ou à faire
-- hors trafic. Ensuite : le job `nokker migrate` avec CORTHEXIS_OWNER_DATABASE_URL (droits DML de nokker_app,
-- fonctions de génération), puis le pod en `corthexis_schema: external`.
--
--   psql -v ON_ERROR_STOP=1 -d corthexis -f deploy/postgres/corthexis-owner.sql

DO $$
DECLARE
    r record;
BEGIN
    IF (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) = 'nokker_app' THEN
        EXECUTE format('ALTER DATABASE %I OWNER TO nokker_owner', current_database());
    END IF;
    IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public') = 'nokker_app' THEN
        ALTER SCHEMA public OWNER TO nokker_owner;
    END IF;
    -- tables et partitions (leurs index et séquences liées suivent)
    FOR r IN SELECT c.oid::regclass AS rel FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND pg_get_userbyid(c.relowner) = 'nokker_app'
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO nokker_owner', r.rel);
    END LOOP;
    -- séquences restantes (non liées à une colonne)
    FOR r IN SELECT c.oid::regclass AS rel FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'S' AND pg_get_userbyid(c.relowner) = 'nokker_app'
    LOOP
        EXECUTE format('ALTER SEQUENCE %s OWNER TO nokker_owner', r.rel);
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA public TO nokker_app;
REVOKE CREATE ON SCHEMA public FROM nokker_app;

-- Contrôle : aucune ligne attendue.
SELECT c.relname AS encore_a_nokker_app FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) = 'nokker_app';
