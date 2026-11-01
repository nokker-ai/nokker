-- Compte posé = compte DU locataire posé (défense en profondeur, 02.11.2026). Numéro 050 (071 sur la branche, renumérotée à l'intégration) : la dernière ;
-- l'intégrateur la garde en dernier. Idempotente : rejouable à chaque démarrage, et APRÈS 023, 032, 037, 039, 049 (ordre alphabétique).
--
-- Trou : les politiques d'équipe, de foyer et de mémoire longue lisaient `app.account_id` / `app.account_email` tels
-- quels. Une connexion du locataire O qui posait le compte (ou l'adresse) d'un membre du foyer de P lisait les notes
-- et la mémoire communes du foyer, ses adhésions, ses dépenses, et pouvait entrer à la place d'un invité
-- (tests/integration/test_actor_tenant_rls.py). Le noyau pose toujours l'acteur de la session, dans son locataire :
-- pas de chemin connu depuis une route ; un acteur mal passé ne doit pourtant rien ouvrir.
--
-- Correctif :
--   - `nokker_actor()` : `app.account_id` seulement si ce compte appartient au locataire posé (`accounts`, hors RLS) ;
--     sinon NULL (aucune politique de compte ne s'ouvre) ;
--   - `nokker_actor_email()` : `app.account_email` seulement si c'est l'adresse de ce compte-là ;
--   - toute POLITIQUE du schéma qui lit `app.account_id` / `app.account_email` est réécrite pour lire ces fonctions
--     (ALTER POLICY : les migrations d'origine ne recréent une politique que si elle n'existe pas) ;
--   - toute FONCTION qui les lit (nokker_team_role, nokker_team_invited, nokker_household_invited_profile,
--     nokker_long_memory_visible…) est recréée de même. Les migrations d'origine les recréent à chaque démarrage, celle-ci
--     les corrige juste après, dans le même passage de `nokker migrate`.
--   - à la fin, plus aucune politique ni fonction ne lit ces réglages directement (sinon : exception, migration en échec).

CREATE OR REPLACE FUNCTION nokker_actor() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    acc text := NULLIF(current_setting('app.account_id', true), '');
    tid text := NULLIF(current_setting('app.tenant_id', true), '');
BEGIN
    IF acc IS NULL OR tid IS NULL THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM accounts a WHERE a.id = acc AND a.tenant_id = tid) THEN
        RETURN acc;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION nokker_actor_email() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    mail text := lower(NULLIF(current_setting('app.account_email', true), ''));
    acc  text := nokker_actor();
BEGIN
    IF mail IS NULL OR acc IS NULL THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM accounts a WHERE a.id = acc AND lower(a.email) = mail) THEN
        RETURN mail;
    END IF;
    RETURN NULL;
END;
$$;

DO $$
DECLARE
    r       record;
    q       text;
    c       text;
    def     text;
    id_re   constant text := 'NULLIF\(current_setting\(''app\.account_id''(::text)?, true\), ''''(::text)?\)';
    mail_re constant text := 'NULLIF\(current_setting\(''app\.account_email''(::text)?, true\), ''''(::text)?\)';
    left_   int;
BEGIN
    -- 1. politiques
    FOR r IN SELECT tablename, policyname, qual, with_check FROM pg_policies
              WHERE schemaname = current_schema()
                AND (coalesce(qual, '') ~ 'app\.account_(id|email)' OR coalesce(with_check, '') ~ 'app\.account_(id|email)')
    LOOP
        q := regexp_replace(regexp_replace(r.qual, id_re, 'nokker_actor()', 'g'), mail_re, 'nokker_actor_email()', 'g');
        c := regexp_replace(regexp_replace(r.with_check, id_re, 'nokker_actor()', 'g'), mail_re, 'nokker_actor_email()',
                            'g');
        EXECUTE format('ALTER POLICY %I ON %I', r.policyname, r.tablename)
             || CASE WHEN q IS NOT NULL THEN ' USING (' || q || ')' ELSE '' END
             || CASE WHEN c IS NOT NULL THEN ' WITH CHECK (' || c || ')' ELSE '' END;
    END LOOP;
    -- 2. fonctions
    FOR r IN SELECT p.oid, p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = current_schema() AND p.prosrc ~ 'app\.account_(id|email)'
                AND p.proname NOT IN ('nokker_actor', 'nokker_actor_email')
    LOOP
        def := pg_get_functiondef(r.oid);
        def := regexp_replace(regexp_replace(def, id_re, 'nokker_actor()', 'g'), mail_re, 'nokker_actor_email()', 'g');
        EXECUTE def;
    END LOOP;
    -- 3. plus rien ne lit ces réglages en direct
    SELECT count(*) INTO left_ FROM pg_policies
     WHERE schemaname = current_schema()
       AND (coalesce(qual, '') ~ 'app\.account_(id|email)' OR coalesce(with_check, '') ~ 'app\.account_(id|email)');
    IF left_ > 0 THEN
        RAISE EXCEPTION 'nokker_actor : % politique(s) lisent encore app.account_* en direct', left_;
    END IF;
    SELECT count(*) INTO left_ FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = current_schema() AND p.prosrc ~ 'app\.account_(id|email)'
       AND p.proname NOT IN ('nokker_actor', 'nokker_actor_email');
    IF left_ > 0 THEN
        RAISE EXCEPTION 'nokker_actor : % fonction(s) lisent encore app.account_* en direct', left_;
    END IF;
END
$$;
