#!/bin/sh
# Rôles du noyau (deploy/postgres/roles.sql, le même fichier que la bêta), mots de passe générés par l'installeur,
# et la base de la mémoire CortHeXis donnée au noyau (ses tables sont créées par corthexis au premier appel).
set -eu
psql -v ON_ERROR_STOP=1 -q -f /sql/roles.sql
psql -v ON_ERROR_STOP=1 -q -v owner="$NOKKER_OWNER_PASSWORD" -v app="$NOKKER_APP_PASSWORD" <<'SQL'
ALTER ROLE nokker_owner PASSWORD :'owner';
ALTER ROLE nokker_app PASSWORD :'app';
ALTER DATABASE corthexis OWNER TO nokker_app;
SQL
psql -v ON_ERROR_STOP=1 -q -d corthexis -c "ALTER SCHEMA public OWNER TO nokker_app"
