#!/usr/bin/env bash
# Paquet d'installation de Lokkal : ce que install.sh / install.ps1 téléchargent et posent dans ~/.nokker.
#   bundle.sh <version> [dossier de sortie=dist]   → nokker-lokkal-<version>.tar.gz + .sha256
# Un seul roles.sql dans le dépôt (deploy/postgres/) et une seule config du noyau : copiés ici, jamais dupliqués.
set -euo pipefail
VER=${1:?version}; OUT=${2:-dist}
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/nokker"; mkdir -p "$D"
cp -r "$HERE/docker-compose.yml" "$HERE/initdb" "$HERE/sql" "$HERE/web" "$HERE/install.sh" "$HERE/backup.sh" "$D/"
mkdir -p "$D/vault"; cp "$HERE/vault/init.sh" "$HERE/vault/unseal.sh" "$D/vault/"   # profil vault (X4)
cp "$HERE/vault/pay-init.sh" "$D/vault/"; cp -r "$HERE/testshop" "$D/"   # profil pay (docs/PAY-AGENT.md §11.5), éteint
cp "$ROOT/deploy/postgres/roles.sql" "$D/sql/roles.sql"
# Config du noyau : le modèle de Nokker dev (deploy/lokkal/core-config.yaml), statique (chemins et hôtes par l'env).
[ -f "$HERE/core-config.yaml" ] || { echo "deploy/lokkal/core-config.yaml absent (branche p2/lokkal-dev pas intégrée ?)" >&2; exit 1; }
mkdir -p "$D/config"; cp "$HERE/core-config.yaml" "$D/config/config.yaml"
# Bureau, mode session (docs/DESKTOP.md §1.1, deploy/desktop/README.md §6) : script hôte, unité systemd --user et texte de
# mise en place FIGÉ (session-setup.json). Dossier `desktop-session` : `desktop/` est l'état de la personne (state, files).
mkdir -p "$D/desktop-session"
cp "$ROOT/deploy/desktop/session-run.sh" "$ROOT/deploy/desktop/nokker-desktop.service" "$ROOT/deploy/desktop/session-setup.json" "$D/desktop-session/"
# Kiosque HDMI (docs/KIOSK.md §4) : lanceur et unit systemd --user, posés dans ~/.nokker/kiosk/ par l'installeur ; jamais
# activés par lui (la personne les installe et les lance elle-même).
mkdir -p "$D/kiosk"; cp "$HERE/kiosk/nokker-kiosk.sh" "$HERE/kiosk/nokker-kiosk.service" "$D/kiosk/"
cp "$HERE/config-history.sha256" "$D/"   # config.yaml non modifiées des installations antérieures (install_config)
printf 'NOKKER_VERSION=%s\n' "$VER" > "$D/VERSION"
# Sites connectés, image PRIVÉE (docs/CONNECTED-SITES.md §3.1.1) : clé publique cosign épinglée et digest de
# sites-chrome pour CETTE version (sortie de ninabot-pro release.sh). Absents : --sites refuse proprement.
if [ -f "$HERE/keys/sites-chrome.pub" ]; then mkdir -p "$D/keys"; cp "$HERE/keys/sites-chrome.pub" "$D/keys/"; fi
if [ -f "$HERE/sites-chrome.digest" ]; then
  grep -Eqx 'sha256:[0-9a-f]{64}' "$HERE/sites-chrome.digest" || { echo "sites-chrome.digest mal formé" >&2; exit 1; }
  cp "$HERE/sites-chrome.digest" "$D/"
fi
mkdir -p "$OUT"
tar -C "$STAGE" --owner=0 --group=0 --sort=name --mtime='2026-01-01' -czf "$OUT/nokker-lokkal-$VER.tar.gz" nokker
cp "$HERE/install.sh" "$HERE/install.ps1" "$OUT/"   # servis aussi seuls (nokker.ai/install.sh, install.ps1)
(cd "$OUT" && for f in "nokker-lokkal-$VER.tar.gz" install.sh install.ps1; do sha256sum "$f" > "$f.sha256"; done)
echo "$OUT/nokker-lokkal-$VER.tar.gz"
