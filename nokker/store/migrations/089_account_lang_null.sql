-- `accounts.lang` : NULL = la personne n'a rien posé ; `fr`, `en`, `de`, `it` = un choix (`Tenancy.set_lang`). Avant,
-- la colonne valait `fr` par défaut et « fr » ne se distinguait pas de « pas posée » : un compte fr écrivant en allemand
-- ne pouvait pas garder fr. Les lignes existantes à `fr` sont remises à NULL : aucun endpoint ne règle la langue
-- (`set_lang` n'est branché nulle part avant ce lot) et la création posait `fr` hors `en` (de et it aussi devenaient `fr`), donc personne n'a pu choisir
-- fr ; seul un `en` a pu venir de l'invitation ou du navigateur, il reste. Lecture inchangée (`lang or 'fr'`).
-- Idempotente, rejouée à chaque démarrage : le bloc ne s'exécute que tant que la colonne est NOT NULL, sinon un `fr`
-- posé plus tard par `set_lang` serait effacé au démarrage suivant. `accounts` est sous RLS FORCÉE (058) et le
-- propriétaire n'a pas BYPASSRLS : sans locataire posé, l'UPDATE ne verrait aucune ligne. La RLS forcée est donc levée le
-- temps de la remise à NULL (même transaction) puis rétablie telle qu'elle était.
DO $$
DECLARE
    forced boolean;
BEGIN
    IF (SELECT is_nullable FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = 'accounts' AND column_name = 'lang') = 'NO' THEN
        ALTER TABLE accounts ALTER COLUMN lang DROP NOT NULL, ALTER COLUMN lang SET DEFAULT NULL;
        SELECT relforcerowsecurity INTO forced FROM pg_class WHERE oid = 'accounts'::regclass;
        IF forced THEN
            ALTER TABLE accounts NO FORCE ROW LEVEL SECURITY;
        END IF;
        UPDATE accounts SET lang = NULL WHERE lang = 'fr';
        IF forced THEN
            ALTER TABLE accounts FORCE ROW LEVEL SECURITY;
        END IF;
    END IF;
END
$$;
