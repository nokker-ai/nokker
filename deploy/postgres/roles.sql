-- Nokker — rôles Postgres de la bêta multi-locataire (docs/TENANCY.md §4). Idempotent : rejouable sans danger.
--
--   nokker_owner : propriétaire des tables, fait les migrations (NOKKER_PG_OWNER_DSN, job d'init seulement).
--   nokker_app   : le noyau (NOKKER_PG_DSN). NI propriétaire NI BYPASSRLS : la RLS de 006_tenant_rls.sql s'applique.
--
-- Ordre d'application :
--   1. ce fichier, par un administrateur (superutilisateur, ou CREATEROLE + propriétaire de la base) : rôles, droits
--      sur le schéma, droits par défaut des tables futures de nokker_owner ;
--   2. les mots de passe, hors dépôt : ALTER ROLE nokker_owner PASSWORD …; ALTER ROLE nokker_app PASSWORD …;
--   3. les migrations, en nokker_owner : `NOKKER_PG_OWNER_DSN=… nokker migrate` ;
--   4. ce fichier une seconde fois : GRANT sur les tables qui existaient avant l'étape 1 (base reprise d'un canari,
--      tables créées par un autre rôle). Sinon, les droits par défaut de l'étape 1 suffisent.
-- Une base déjà peuplée par un autre rôle : `ALTER TABLE … OWNER TO nokker_owner` table par table AVANT l'étape 3.
--
-- Hors RLS par conception, mais lisibles par nokker_app : tenants, accounts, account_identities, et les tables
-- d'authentification devices, sessions, totp_secrets, used_nonces (lues avant de connaître le locataire) ; operators,
-- admin_audit, beta_applications (console).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_owner') THEN
        CREATE ROLE nokker_owner LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
        CREATE ROLE nokker_app LOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO nokker_owner, nokker_app', current_database());
END
$$;

-- Personne n'échappe à la RLS, même un rôle qui existait avant avec d'autres attributs (ALTER seulement si besoin :
-- changer ces attributs demande un superutilisateur, qu'un Postgres managé ne donne pas).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('nokker_app', 'nokker_owner') AND (rolsuper OR rolbypassrls))
    THEN
        ALTER ROLE nokker_app NOSUPERUSER NOBYPASSRLS;
        ALTER ROLE nokker_owner NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

GRANT USAGE, CREATE ON SCHEMA public TO nokker_owner;
GRANT USAGE ON SCHEMA public TO nokker_app;
REVOKE CREATE ON SCHEMA public FROM nokker_app;

-- Tables existantes (étape 4) ; pas de TRUNCATE, REFERENCES ni TRIGGER.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO nokker_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO nokker_app;

-- Tables futures créées par les migrations de nokker_owner (étape 3).
ALTER DEFAULT PRIVILEGES FOR ROLE nokker_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nokker_app;
ALTER DEFAULT PRIVILEGES FOR ROLE nokker_owner IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO nokker_app;

-- nokker_backup : le dump quotidien (docs/BACKUP-DRP.md §3). LOGIN BYPASSRLS, lecture seule. Créé hors de ce fichier,
-- avec son mot de passe, par l'administrateur (il faut BYPASSRLS pour l'accorder). Un Postgres managé ne laisse pas
-- accorder pg_read_all_data : d'où ces GRANT, rejoués à chaque init pour qu'aucune table n'échappe au dump.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_backup') THEN
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO nokker_backup', current_database());
        GRANT USAGE ON SCHEMA public TO nokker_backup;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO nokker_backup;
        GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO nokker_backup;
        ALTER DEFAULT PRIVILEGES FOR ROLE nokker_owner IN SCHEMA public GRANT SELECT ON TABLES TO nokker_backup;
        ALTER DEFAULT PRIVILEGES FOR ROLE nokker_owner IN SCHEMA public GRANT SELECT ON SEQUENCES TO nokker_backup;
    END IF;
END
$$;
