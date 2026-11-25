-- Nokker — prénom du membre (docs/CONNECTORS-MULTI.md §« Nom montré »). Additive et idempotente : rejouée à chaque démarrage.
--
-- 1. `accounts.first_name` : prénom tel que saisi (Mon compte, ou recopié de l'invitation au foyer à l'acceptation).
--    Vide = pas de prénom : le nom montré retombe sur la partie locale de l'adresse, comme avant.
-- 2. `team_invites.first_name` : prénom que le payeur saisit en invitant ; recopié dans le compte de l'invité à
--    l'acceptation s'il n'en a pas encore.
-- Numéro 072 : 070 et 071 sont pris par des branches en cours.

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS first_name TEXT NOT NULL DEFAULT '';
ALTER TABLE team_invites ADD COLUMN IF NOT EXISTS first_name TEXT NOT NULL DEFAULT '';
