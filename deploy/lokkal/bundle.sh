#!/usr/bin/env bash
# Paquet d'installation de Lokkal : ce que install.sh / install.ps1 téléchargent et posent dans ~/.nokker.
#   bundle.sh <version> [dossier de sortie=dist]   → nokker-lokkal-<version>.tar.gz + .sha256
# Un seul roles.sql dans le dépôt (deploy/postgres/) et une seule config du noyau : copiés ici, jamais dupliqués.
set -euo pipefail
VER=${1:?version}; OUT=${2:-dist}
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/nokker"; mkdir -p "$D"
cp -r "$HERE/docker-compose.yml" "$HERE/initdb" "$HERE/sql" "$HERE/web" "$HERE/install.sh" "$D/"
cp "$ROOT/deploy/postgres/roles.sql" "$D/sql/roles.sql"
# Config du noyau : le modèle de Nokker dev (deploy/lokkal/core-config.yaml), statique (chemins et hôtes par l'env).
[ -f "$HERE/core-config.yaml" ] || { echo "deploy/lokkal/core-config.yaml absent (branche p2/lokkal-dev pas intégrée ?)" >&2; exit 1; }
mkdir -p "$D/config"; cp "$HERE/core-config.yaml" "$D/config/config.yaml"
printf 'NOKKER_VERSION=%s\n' "$VER" > "$D/VERSION"
mkdir -p "$OUT"
tar -C "$STAGE" --owner=0 --group=0 --sort=name --mtime='2026-01-01' -czf "$OUT/nokker-lokkal-$VER.tar.gz" nokker
cp "$HERE/install.sh" "$HERE/install.ps1" "$OUT/"   # servis aussi seuls (nokker.ai/install.sh, install.ps1)
(cd "$OUT" && for f in "nokker-lokkal-$VER.tar.gz" install.sh install.ps1; do sha256sum "$f" > "$f.sha256"; done)
echo "$OUT/nokker-lokkal-$VER.tar.gz"
