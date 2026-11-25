-- Nokker — provenance des épisodes et des faits de la mémoire (nokker/memory/provenance.py, docs/PROVENANCE.md §4.4).
-- Additive et idempotente : rejouée à chaque démarrage, et par les stores qui lisent ces colonnes (contexte, mémoire
-- longue). Chaque table est traitée seulement si elle existe (pilier pas encore migré : traité au passage suivant).
--
-- Valeurs : person | assistant | unknown_legacy | third_party:<sous-type> (attachment:file, web:page, mail,
-- tool:<nom>…). DEFAULT 'unknown_legacy' : toute ligne d'avant prend cette valeur, rendue comme `person` au rappel (rien
-- ne change à la bascule) et gardée à part pour être reclassée plus tard d'après sa trace (jamais par un modèle). Le
-- code écrit toujours une valeur explicite : un nouveau fait sans provenance connue est rangé `third_party:unknown`.
-- ADD COLUMN avec un défaut constant : pas de réécriture de la table (PostgreSQL ≥ 11).
-- Numéro 080 : 077, 078 (p2/phone-node) et 079 (p2/phone-egress) sont pris par des branches en cours ; 074 n'est pas
-- comblé (on ne remonte pas sous des numéros déjà livrés).

-- Aucun ALTER (verrou exclusif) une fois la colonne en place : chaque étape vérifie l'état avant d'agir (comme 006).

DO $$
DECLARE
    t    TEXT;
    rel  REGCLASS;
BEGIN
    FOREACH t IN ARRAY ARRAY['nokker_episodes', 'nokker_long_memory', 'nokker_memory_pending'] LOOP
        rel := to_regclass(quote_ident(t));
        CONTINUE WHEN rel IS NULL;
        IF NOT EXISTS (SELECT 1 FROM pg_attribute
                       WHERE attrelid = rel AND attname = 'provenance' AND NOT attisdropped) THEN
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS provenance TEXT NOT NULL DEFAULT %L',
                           t, 'unknown_legacy');
        END IF;
    END LOOP;
END $$;
