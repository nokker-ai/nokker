#!/bin/sh
# Nokker — base dédiée à la mémoire CortHeXis en mode (ii) "direct" (chantier A-2, PLAN-P1 §5.2).
# Exécuté automatiquement par l'image postgres au premier démarrage (docker-entrypoint-initdb.d,
# volume vide uniquement), comme 001-pgvector.sql. UNE base en plus dans le MÊME conteneur Postgres,
# PAS un second conteneur : le paquet `corthexis` (nokker/memory/corthexis.py:DirectCorthexisMemory)
# y tient ses propres tables (notes, chunks, générations d'index — migrées par `corthexis.store.Store`
# au premier appel, pas ici), séparées des tables du noyau Nokker (threads/tours, `store/pg.py`).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
    SELECT 'CREATE DATABASE corthexis OWNER ' || quote_ident('$POSTGRES_USER')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'corthexis')
    \gexec
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname corthexis -c "CREATE EXTENSION IF NOT EXISTS vector;"
