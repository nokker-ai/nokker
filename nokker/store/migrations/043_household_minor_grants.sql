-- Profils mineurs du foyer, ouverts foyer par foyer (docs/LOKKAL.md §7.29.4, `household_minors_mode: allowlist`).
-- Numérotée 043 : 041 et 042 sont réservées par des branches de la rc.7 (vérifié sur la forge et les worktrees le 25.10).
-- Rejouée à chaque démarrage.
--
-- 1. `household_minor_grants` : les payeurs que l'OPÉRATEUR a autorisés à déclarer un profil `teen` ou `child` (et à
--    inviter avec une attestation de représentant légal). Aucune adresse dans la configuration : la liste vit ici.
--    HORS RLS, comme `account_minors` (039) : elle est lue dans le tour du MINEUR (son locataire n'est pas celui du
--    payeur) et par l'opérateur pour tous les locataires ; écrite par l'outil d'opérateur seul
--    (`python -m nokker household minors allow|revoke`). Un retrait garde la ligne (`revoked_at`) : rien n'est
--    supprimé, sauf avec le locataire du payeur (`nokker erase`, par `tenant_id`). `code` : code court montré à
--    l'opérateur à la place de l'adresse et du compte.
CREATE TABLE IF NOT EXISTS household_minor_grants (
    account_id   text PRIMARY KEY,                -- le payeur autorisé
    tenant_id    text NOT NULL,                   -- son locataire (effacement)
    code         text NOT NULL,                   -- mg-<6 hex>, seul identifiant affiché
    granted_at   double precision NOT NULL,
    revoked_at   double precision                 -- non NULL : retirée (profils mineurs du foyer suspendus)
);
CREATE UNIQUE INDEX IF NOT EXISTS household_minor_grants_code ON household_minor_grants (code);
CREATE INDEX IF NOT EXISTS household_minor_grants_tenant ON household_minor_grants (tenant_id);

-- 2. Audit des autorisations et des retraits : ajout seul (aucune ligne ne change ; supprimée seulement pendant
--    l'effacement de SON locataire, `nokker.erasing_tenant`). Hors RLS pour la même raison que la liste.
CREATE TABLE IF NOT EXISTS household_minor_grant_audit (
    id           bigserial PRIMARY KEY,
    at           double precision NOT NULL,
    tenant_id    text NOT NULL,
    account_id   text NOT NULL,
    code         text NOT NULL,
    action       text NOT NULL CHECK (action IN ('allow', 'revoke')),
    suspended    integer NOT NULL DEFAULT 0       -- profils mineurs suspendus par ce retrait
);
CREATE INDEX IF NOT EXISTS household_minor_grant_audit_tenant ON household_minor_grant_audit (tenant_id);

CREATE OR REPLACE FUNCTION nokker_minor_grant_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
            AND OLD.tenant_id = NULLIF(current_setting('nokker.erasing_tenant', true), '') THEN
        RETURN OLD;  -- effacement du locataire du payeur (nokker erase)
    END IF;
    RAISE EXCEPTION 'household_minor_grant_audit : journal en ajout seul (ligne %)', OLD.id
        USING ERRCODE = 'insufficient_privilege';
END $$;
DROP TRIGGER IF EXISTS household_minor_grant_audit_append_only ON household_minor_grant_audit;
CREATE TRIGGER household_minor_grant_audit_append_only BEFORE UPDATE OR DELETE ON household_minor_grant_audit
    FOR EACH ROW EXECUTE FUNCTION nokker_minor_grant_audit_append_only();
