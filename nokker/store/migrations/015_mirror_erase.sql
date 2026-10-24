-- Miroir web ⇄ Lokkal (docs/LOKKAL.md §6.3, L5) : effacement propagé des COPIES du miroir, et d'elles seules.
-- `turns` reste append-only pour le noyau (migration 013) : ni UPDATE ni DELETE, sauf dans une transaction
-- d'effacement. Cette fonction en ouvre une, ÉTROITE : locataire de la connexion (`app.tenant_id`, jamais un autre),
-- fils dont l'identifiant commence par un préfixe de copie — `lokkal-<machine>-` (copies d'une machine chez
-- nokker.ai, éventuellement précédé de `<locataire>:`) ou `mirror-web-` (copies du web sur une machine). Un fil
-- « maison » (session web, session locale) n'est jamais atteignable par ici. SECURITY DEFINER : le rôle du noyau
-- (`nokker_app`) l'exécute sans recevoir de droit DELETE.
CREATE OR REPLACE FUNCTION nokker_mirror_erase(prefix text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    t text := current_setting('app.tenant_id', true);
    bare text := prefix;
    n integer;
BEGIN
    IF t IS NULL OR t = '' THEN
        RAISE EXCEPTION 'nokker_mirror_erase : aucun locataire (app.tenant_id)';
    END IF;
    IF starts_with(bare, t || ':') THEN
        bare := substr(bare, length(t) + 2);
    END IF;
    IF bare !~ '^(lokkal-[A-Za-z0-9_]+-|mirror-web-)$' THEN
        RAISE EXCEPTION 'nokker_mirror_erase : préfixe refusé (%), seulement des copies du miroir', prefix;
    END IF;
    PERFORM set_config('nokker.erasing_tenant', t, true);
    DELETE FROM verdicts WHERE tenant_id = t AND starts_with(thread_id, prefix);
    DELETE FROM llm_calls WHERE tenant_id = t AND starts_with(thread_id, prefix);
    DELETE FROM turns WHERE tenant_id = t AND starts_with(thread_id, prefix);
    GET DIAGNOSTICS n = ROW_COUNT;
    DELETE FROM threads WHERE tenant_id = t AND starts_with(thread_id, prefix);
    PERFORM set_config('nokker.erasing_tenant', '', true);
    RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION nokker_mirror_erase(text) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nokker_app') THEN
        GRANT EXECUTE ON FUNCTION nokker_mirror_erase(text) TO nokker_app;
    END IF;
END;
$$;
