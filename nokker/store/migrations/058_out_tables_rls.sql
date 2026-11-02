-- Tables « hors RLS » qui portent pourtant un locataire : sous RLS forcée (chantier p2/rls-out-tables, 02.11.2026,
-- docs/TENANCY.md §4). Numéro 058 : AVANT 059_actor_tenant (la dernière, qui réécrit les politiques de compte ; aucune
-- politique d'ici ne lit `app.account_*`). Idempotente : rejouée à chaque démarrage, chaque étape vérifie l'état avant
-- d'agir (aucun ALTER ni verrou exclusif une fois la table en place).
--
-- Trou : 15 tables avec `tenant_id` n'avaient ni RLS ni politique, et `nokker_app` y a SELECT/UPDATE/DELETE. Une
-- requête qui oubliait son filtre de locataire y voyait tous les locataires.
--
-- Correctif, pour chacune :
--   - `tenant_id` prend par défaut le locataire de la connexion (comme 006) ;
--   - RLS activée ET forcée, politique `tenant_isolation` (lecture et écriture dans le locataire posé, comme 006) ;
--   - pour celles qu'on cherche AVANT de connaître le locataire (jeton, code, cookie, domaine présenté), des politiques
--     « par clé » : la ligne s'ouvre aussi si sa clé vaut `app.lookup_key` (posé par nokker/store/conn.py::connect_key,
--     le temps d'une connexion courte). Une requête sans clé ne voit que son locataire ; avec une clé, la seule ligne
--     qui la porte. Jamais d'INSERT par clé : une ligne s'écrit toujours dans son locataire (WITH CHECK) ;
--   - une ligne ne change jamais de locataire (trigger `nokker_tenant_immutable`) : une mise à jour par clé ne peut pas
--     déplacer la ligne chez un autre.
-- Lectures de l'opérateur pour tous les locataires (file d'effacement, modération, autorisations des mineurs) :
-- locataire par locataire (`known_tenants`), jamais par une valeur joker dans la politique.

CREATE OR REPLACE FUNCTION nokker_lookup_key() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('app.lookup_key', true), '') $$;

CREATE OR REPLACE FUNCTION nokker_tenant_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION '% : une ligne ne change jamais de locataire', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

DO $$
DECLARE
    spec  text[];
    t     text;
    keyx  text;
    cmds  text;
    cmd   text;
    rel   regclass;
    pol   text;
BEGIN
    -- {table, condition de clé ('' : aucune lecture par clé), commandes ouvertes par la clé}
    FOREACH spec SLICE 1 IN ARRAY ARRAY[
        -- comptes : session, identité, outil d'opérateur (par identifiant ou adresse)
        ['accounts', 'id = nokker_lookup_key() OR email = lower(nokker_lookup_key())', 'SELECT,UPDATE'],
        ['account_deletions', '', ''],
        ['billing_customers', 'stripe_customer = nokker_lookup_key()', 'SELECT'],  -- webhook Stripe
        ['household_minor_grants', 'account_id = nokker_lookup_key()', 'SELECT'],  -- tour du mineur : payeur
        ['household_minor_grant_audit', '', ''],
        ['lokkal_gateway_codes', 'code_sha256 = nokker_lookup_key()', 'SELECT,DELETE'],
        ['lokkal_gateway_refresh', 'token_sha256 = nokker_lookup_key() OR family = nokker_lookup_key()',
         'SELECT,UPDATE'],
        ['lokkal_machines', 'id = nokker_lookup_key() OR token_sha256 = nokker_lookup_key() '
                            'OR host = nokker_lookup_key() OR account_id = nokker_lookup_key()', 'SELECT,UPDATE'],
        ['lokkal_page_moderation', '', ''],
        ['lokkal_public_pages', '', ''],
        ['sso_domain_claims', 'domain = nokker_lookup_key()', 'SELECT'],
        ['sso_flows', 'state_sha256 = nokker_lookup_key()', 'SELECT,DELETE'],
        ['sso_routes', 'connection_id = nokker_lookup_key()', 'SELECT'],
        ['thread_share_tokens', 'token_sha256 = nokker_lookup_key()', 'SELECT'],
        ['ambient_displays', 'id = nokker_lookup_key() OR token_sha256 = nokker_lookup_key() '
                             'OR pair_sha256 = nokker_lookup_key() OR account_id = nokker_lookup_key()',
         'SELECT,UPDATE,DELETE']
    ] LOOP
        t := spec[1]; keyx := spec[2]; cmds := spec[3];
        rel := to_regclass(quote_ident(t));
        CONTINUE WHEN rel IS NULL;

        -- défaut = locataire de la connexion, comme 006 (le code écrit toujours le locataire en clair)
        IF NOT EXISTS (SELECT 1 FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
                       WHERE d.adrelid = rel AND a.attname = 'tenant_id'
                         AND pg_get_expr(d.adbin, d.adrelid) LIKE 'current_setting(''app.tenant_id''%') THEN
            EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT current_setting(%L)', t, 'app.tenant_id');
        END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = rel) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies
                       WHERE schemaname = current_schema() AND tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(%L, true)) '
                           'WITH CHECK (tenant_id = current_setting(%L, true))', t, 'app.tenant_id', 'app.tenant_id');
        END IF;
        IF keyx <> '' THEN
            FOREACH cmd IN ARRAY string_to_array(cmds, ',') LOOP
                pol := 'key_lookup_' || lower(cmd);
                CONTINUE WHEN EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                                         AND tablename = t AND policyname = pol);
                IF cmd = 'UPDATE' THEN
                    EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE USING (%s) WITH CHECK (%s)', pol, t, keyx, keyx);
                ELSE
                    EXECUTE format('CREATE POLICY %I ON %I FOR %s USING (%s)', pol, t, cmd, keyx);
                END IF;
            END LOOP;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = rel AND tgname = t || '_tenant_immutable') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OF tenant_id ON %I FOR EACH ROW '
                           'EXECUTE FUNCTION nokker_tenant_immutable()', t || '_tenant_immutable', t);
        END IF;
    END LOOP;
END
$$;
