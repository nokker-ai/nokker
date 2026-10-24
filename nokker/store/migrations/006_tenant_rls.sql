-- Nokker — multi-locataire, isolation en base (bêta 28.10.2026, docs/TENANCY.md §4).
-- Chaque table de contenu porte `tenant_id` (les lignes d'avant sont rattachées à `tn_ninabot`), puis prend le
-- locataire de la connexion (`app.tenant_id`, posé par nokker/store/conn.py). RLS activée ET forcée (le propriétaire
-- n'y échappe pas non plus) ; politique : la ligne appartient au locataire de la connexion, en lecture comme en
-- écriture. Sans `app.tenant_id`, `current_setting(…, true)` vaut NULL : aucune ligne, aucune écriture.
--
-- Rejouée à chaque démarrage (apply_migrations, et après la migration de chaque store, nokker/store/conn.py::migrate) :
-- chaque étape vérifie l'état avant d'agir, aucun ALTER (verrou exclusif) une fois la table en place. Une table
-- absente (pilier pas encore migré, `connector_credentials` et `terms_acceptances` créées par 005) est traitée au
-- passage suivant.
--
-- Hors RLS, par conception : tenants, accounts, account_identities (lues avant de connaître le locataire) ;
-- devices, sessions, totp_secrets, used_nonces (authentification : verify_session, login_challenge et create_session
-- les lisent avant de connaître le locataire ; filtrage par compte dans le code, nokker/approvals) ; operators,
-- admin_audit, beta_applications (console). Clés primaires et unicités inchangées : les fils des testeurs
-- sont préfixés `tn_x:` (§3).

DO $$
DECLARE
    t    TEXT;
    rel  REGCLASS;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'turns', 'threads', 'nokker_threads', 'nokker_thread_inbox', 'nokker_bubbles',
        'nokker_episodes', 'nokker_day_summaries', 'nokker_view_state', 'nokker_reservoir',
        'approvals', 'approval_events',
        'ledger_entries', 'ledger_verdicts', 'llm_calls', 'verdicts',
        'nokker_agents', 'connector_credentials', 'terms_acceptances'
    ] LOOP
        rel := to_regclass(quote_ident(t));
        CONTINUE WHEN rel IS NULL;

        IF NOT EXISTS (SELECT 1 FROM pg_attribute
                       WHERE attrelid = rel AND attname = 'tenant_id' AND NOT attisdropped) THEN
            -- DEFAULT littéral : rattache les lignes existantes à l'interne ; ensuite, le locataire de la connexion
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT %L',
                           t, 'tn_ninabot');
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
                       WHERE d.adrelid = rel AND a.attname = 'tenant_id'
                         AND pg_get_expr(d.adbin, d.adrelid) LIKE 'current_setting(''app.tenant_id''%') THEN
            EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT current_setting(%L)', t, 'app.tenant_id');
        END IF;

        IF to_regclass(quote_ident(t || '_tenant_idx')) IS NULL THEN
            EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id)', t || '_tenant_idx', t);
        END IF;

        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = rel) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;

        IF NOT EXISTS (SELECT 1 FROM pg_policies
                       WHERE schemaname = current_schema() AND tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                           'USING (tenant_id = current_setting(%L, true)) '
                           'WITH CHECK (tenant_id = current_setting(%L, true))',
                           t, 'app.tenant_id', 'app.tenant_id');
        END IF;
    END LOOP;
END
$$;
