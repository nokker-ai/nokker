-- Nokker — activation de pgvector sur la base de la mémoire.
-- Exécuté automatiquement par l'image postgres au premier démarrage
-- (docker-entrypoint-initdb.d, volume vide uniquement).
CREATE EXTENSION IF NOT EXISTS vector;
