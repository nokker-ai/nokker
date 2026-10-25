-- Cartes fortes des connecteurs relayés (lot X20b, docs/LOKKAL.md §7.12, nokker/cloud/relay.py) : un envoi externe ou
-- un destructif relayé n'est exécuté par nokker.ai que sur une carte FORTE signée par un appareil du compte, vérifiée
-- ici. Le nonce du défi signé est gardé sur la ligne de l'appel exécuté, et ne sert qu'UNE fois, toutes machines et
-- tous locataires confondus (un index unique ne voit pas la RLS : c'est voulu, un nonce est aléatoire et global). Le
-- nonce n'est pas un secret (il est dans le défi que la page affiche) ; la signature elle-même n'est jamais écrite.
-- Une ligne refusée ne porte pas de nonce : un refus ne brûle pas le défi. Rejouée à chaque démarrage (idempotente).
-- Numérotée 026 : 023 à 025 sont peut-être prises par d'autres branches.
ALTER TABLE lokkal_relay_calls ADD COLUMN IF NOT EXISTS strong_nonce text;
CREATE UNIQUE INDEX IF NOT EXISTS lokkal_relay_calls_one_signature
    ON lokkal_relay_calls (strong_nonce) WHERE strong_nonce IS NOT NULL;
