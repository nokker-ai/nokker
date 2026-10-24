#!/bin/sh
# Nokker Lokkal — installeur Linux / macOS (docs/LOKKAL.md §3).
#
#   curl -fsSL https://nokker.ai/install.sh | sh
#   curl -fsSL https://nokker.ai/install.sh | sh -s -- --workspace ~/code --yes
#   sh ~/.nokker/install.sh --update [--version X.Y.Z]      (copie posée par l'installation)
#
# Ce qu'il fait, dans l'ordre : vérifie Docker (et propose de l'installer sous Linux), télécharge le paquet de la
# version voulue et en vérifie le sha256 (et la signature si cosign est là), génère les secrets locaux dans
# ~/.nokker/.env (0600), écrit ~/.config/nokker/lokkal.json pour la commande `nokker`, lance le compose, attend le
# noyau, pose la commande `nokker` dans ~/.nokker/bin et ouvre l'onboarding web.
#
# Options :
#   --version X.Y.Z     version à installer (défaut : la dernière release lokkal-v*)
#   --workspace DIR     dossier(s) de travail montés dans les bacs à sable, séparés par « : » (défaut : ~/nokker)
#   --dir DIR           dossier d'installation (défaut : ~/.nokker)
#   --yes               ne pose aucune question (installation de Docker comprise, sous Linux)
#   --update            garde .env et l'état, change de version, relance
#   --uninstall         arrête et retire les containers ; --purge efface aussi l'état et la base
#   --no-open           n'ouvre pas le navigateur
# Variables (tests) : NOKKER_RELEASE_BASE (URL des pièces de release), NOKKER_SKIP_PULL=1, NOKKER_NO_CLI=1.
set -eu

REPO=nokker-ai/nokker
VERSION=""; WORKSPACE=""; DIR="${HOME}/.nokker"; YES=0; MODE=install; PURGE=0; OPEN=1
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nokker"

say()  { printf '\033[1m›\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { # ask "question" → 0 si oui
  [ "$YES" = 1 ] && return 0
  [ -r /dev/tty ] || return 1
  printf '%s [o/N] ' "$1" > /dev/tty; read -r a < /dev/tty || return 1
  case "$a" in o|O|oui|y|Y|yes) return 0;; *) return 1;; esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION=${2:?}; shift 2;;
    --workspace) WORKSPACE=${2:?}; shift 2;;
    --dir) DIR=${2:?}; shift 2;;
    --yes|-y) YES=1; shift;;
    --update) MODE=update; shift;;
    --uninstall) MODE=uninstall; shift;;
    --purge) PURGE=1; shift;;
    --no-open) OPEN=0; shift;;
    -h|--help) sed -n '2,25p' "$0" 2>/dev/null || true; exit 0;;
    *) die "option inconnue : $1";;
  esac
done

[ "$(id -u)" = 0 ] && warn "lancé en root : Lokkal s'installe pour l'utilisateur courant ; préférez votre compte."
OS=$(uname -s); ARCH=$(uname -m)
case "$OS" in Linux) os=linux;; Darwin) os=darwin;; *) die "système non pris en charge : $OS (Windows : install.ps1)";; esac
case "$ARCH" in x86_64|amd64) arch=x64;; aarch64|arm64) arch=arm64;; *) die "architecture non prise en charge : $ARCH";; esac

need() { command -v "$1" >/dev/null 2>&1; }
need curl || die "curl est requis"
sha256() { if need sha256sum; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
rand() { # rand <octets> : base64 url sans remplissage
  head -c "$1" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'
}
fernet() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }  # 32 octets, base64 url AVEC « = »

compose() { docker compose -p nokker --env-file "$DIR/.env" -f "$DIR/docker-compose.yml" "$@"; }

# ------------------------------------------------------------------------------------------------- désinstallation
if [ "$MODE" = uninstall ]; then
  [ -f "$DIR/docker-compose.yml" ] || die "rien d'installé dans $DIR"
  if [ "$PURGE" = 1 ]; then
    ask "Effacer AUSSI la base, la mémoire et l'état de Nokker (irréversible) ?" || die "abandon"
    compose down -v --remove-orphans
    for c in $(docker ps -aq --filter label=nokker.sandbox); do docker rm -f "$c" >/dev/null 2>&1 || true; done
    rm -rf "$DIR" "$CONF_DIR/lokkal.json"
    say "Nokker Lokkal désinstallé, données effacées. Vos dossiers de travail n'ont pas été touchés."
  else
    compose down --remove-orphans
    for c in $(docker ps -aq --filter label=nokker.sandbox); do docker rm -f "$c" >/dev/null 2>&1 || true; done
    say "Nokker Lokkal arrêté ; données gardées dans $DIR (--uninstall --purge pour tout effacer)."
  fi
  exit 0
fi

# ------------------------------------------------------------------------------------------------- Docker
docker_ok() { need docker && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }
if ! docker_ok; then
  if need docker && ! docker info >/dev/null 2>&1; then
    if [ "$os" = linux ] && docker info 2>&1 | grep -qi "permission denied"; then
      die "Docker est là mais votre compte n'y a pas accès : sudo usermod -aG docker $USER, puis reconnectez-vous."
    fi
    die "Docker est installé mais ne répond pas : démarrez Docker (Docker Desktop / OrbStack / systemctl start docker)."
  fi
  if [ "$os" = darwin ]; then
    die "Docker est requis : installez Docker Desktop (https://www.docker.com/products/docker-desktop/) ou OrbStack (https://orbstack.dev), lancez-le, puis relancez cette commande."
  fi
  ask "Docker n'est pas installé. L'installer maintenant (script officiel get.docker.com, demande sudo) ?" \
    || die "Docker est requis : https://docs.docker.com/engine/install/"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh; rm -f /tmp/get-docker.sh
  sudo usermod -aG docker "$(id -un)" 2>/dev/null || true
  if ! docker info >/dev/null 2>&1; then
    die "Docker installé : reconnectez-vous (groupe docker), puis relancez cette commande."
  fi
fi
# Socket vu par le démon (monté dans dockerproxy) : rootless → celui du contexte courant
SOCK=/var/run/docker.sock
ep=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)
case "$ep" in unix://*) p=${ep#unix://}; case "$p" in /run/user/*|*/.docker/run/*) [ "$os" = linux ] && SOCK=$p;; esac;; esac

# ------------------------------------------------------------------------------------------------- version et paquet
BASE=${NOKKER_RELEASE_BASE:-}
if [ -z "$VERSION" ]; then
  if [ -n "$BASE" ] && [ -f "${BASE#file://}/LATEST" ]; then VERSION=$(cat "${BASE#file://}/LATEST")
  else
    VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=30" \
      | grep -o '"tag_name": *"lokkal-v[^"]*"' | head -1 | sed 's/.*lokkal-v//; s/"//') \
      || die "impossible de trouver la dernière version (--version X.Y.Z)"
  fi
fi
[ -n "$VERSION" ] || die "version introuvable"
BASE=${BASE:-https://github.com/$REPO/releases/download/lokkal-v$VERSION}
fetch() { case "$BASE" in file://*) cp "${BASE#file://}/$1" "$2";; *) curl -fsSL "$BASE/$1" -o "$2";; esac; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PKG=nokker-lokkal-$VERSION.tar.gz
say "Nokker Lokkal $VERSION ($os-$arch)"
fetch "$PKG" "$TMP/$PKG" || die "téléchargement impossible : $BASE/$PKG"
fetch "$PKG.sha256" "$TMP/$PKG.sha256" || die "empreinte introuvable : $PKG.sha256"
[ "$(sha256 "$TMP/$PKG")" = "$(cut -d' ' -f1 "$TMP/$PKG.sha256")" ] || die "empreinte sha256 du paquet incorrecte : abandon"
# Authenticité (en plus de l'intégrité du .sha256, servi au même endroit) : si cosign est là, la pièce doit être signée
# par le workflow de release de $REPO sur un tag lokkal-v. Code retour : 0 vérifiée, 2 pas de cosign, 1 tout le reste
# (signature absente ou fausse, cosign qui ne s'exécute pas : 126, 127…).
ID_RE="^https://github\\.com/$(printf %s "$REPO" | sed 's/\./\\./g')/\\.github/workflows/lokkal-images\\.yml@refs/tags/lokkal-v"
verify_sig() {
  need cosign || return 2
  fetch "$1.sigstore.json" "$TMP/$1.sigstore.json" 2>/dev/null || return 1
  cosign verify-blob --bundle "$TMP/$1.sigstore.json" --certificate-identity-regexp "$ID_RE" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com "$TMP/$1" >/dev/null 2>&1 || return 1
}
sig=0; verify_sig "$PKG" || sig=$?   # set -e : le code doit être capté, sinon le script sort ici sans message
case $sig in
  0) say "signature du paquet vérifiée (cosign)";;
  1) die "signature cosign du paquet invalide ou absente : abandon";;
esac

# ------------------------------------------------------------------------------------------------- pose des fichiers
umask 077
mkdir -p "$DIR" "$DIR/state" "$DIR/bin" "$CONF_DIR"
tar -xzf "$TMP/$PKG" -C "$TMP"
# Fichiers du paquet remplacés ; .env et state/ jamais touchés
for f in docker-compose.yml config initdb sql web VERSION install.sh; do rm -rf "${DIR:?}/$f"; cp -R "$TMP/nokker/$f" "$DIR/$f"; done
chmod -R go-w "$DIR"; chmod 755 "$DIR/initdb" "$DIR/sql" "$DIR/web" "$DIR/config"
chmod 644 "$DIR"/initdb/* "$DIR"/sql/* "$DIR"/web/* "$DIR"/config/*; chmod 700 "$DIR/install.sh"

port_free() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/" 2>/dev/null; [ $? -eq 7 ]; }   # 7 = connexion refusée
pick_port() { p=$1; while ! port_free "$p"; do p=$((p + 1)); done; echo "$p"; }

setv() { # setv CLÉ valeur : remplace ou ajoute dans .env
  if grep -q "^$1=" "$DIR/.env" 2>/dev/null; then
    sed "s|^$1=.*|$1=$2|" "$DIR/.env" > "$DIR/.env.tmp" && mv "$DIR/.env.tmp" "$DIR/.env"
  else printf '%s=%s\n' "$1" "$2" >> "$DIR/.env"; fi
}
getv() { grep "^$1=" "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-; }

if [ ! -f "$DIR/.env" ]; then
  [ "$MODE" = update ] && die "--update : aucune installation dans $DIR"
  WORKSPACE=${WORKSPACE:-$HOME/nokker}
  ws=""; IFS_=$IFS; IFS=:
  for w in $WORKSPACE; do
    case "$w" in "~"*) w="$HOME${w#\~}";; esac
    mkdir -p "$w"; w=$(cd "$w" && pwd -P)
    [ "$w" = / ] || [ "$w" = "$HOME" ] && die "dossier de travail trop large : $w (choisissez un sous-dossier)"
    ws="${ws:+$ws:}$w"
  done; IFS=$IFS_
  WEB_PORT=$(pick_port 7420); API_PORT=$(pick_port 8742)
  : > "$DIR/.env"; chmod 600 "$DIR/.env"
  cat >> "$DIR/.env" <<EOF
# Nokker Lokkal — généré par install.sh le $(date -u +%Y-%m-%dT%H:%M:%SZ). Secrets locaux : ne pas partager.
NOKKER_VERSION=$VERSION
NOKKER_UID=$(id -u)
NOKKER_GID=$(id -g)
NOKKER_STATE_DIR=$DIR/state
NOKKER_HOST_WORKSPACE=$ws
NOKKER_PORT=$WEB_PORT
NOKKER_API_PORT=$API_PORT
DOCKER_SOCKET=$SOCK
NOKKER_PG_ADMIN_PASSWORD=$(rand 24)
NOKKER_OWNER_PASSWORD=$(rand 24)
NOKKER_APP_PASSWORD=$(rand 24)
NOKKER_APPROVALS_KEY=$(fernet)
NOKKER_LOCAL_SECRET_KEY=$(fernet)
EOF
else
  setv NOKKER_VERSION "$VERSION"
  [ -n "$WORKSPACE" ] && warn "--workspace ignoré : installation existante (modifier NOKKER_HOST_WORKSPACE dans $DIR/.env)"
fi
chmod 600 "$DIR/.env"; chmod 700 "$DIR/state"
WEB_PORT=$(getv NOKKER_PORT); API_PORT=$(getv NOKKER_API_PORT)

cat > "$CONF_DIR/lokkal.json" <<EOF
{"compose": "$DIR/docker-compose.yml", "project": "nokker", "version": "$VERSION", "core_service": "core",
 "api_url": "http://127.0.0.1:$API_PORT", "web_url": "http://127.0.0.1:$WEB_PORT",
 "token_path": "$DIR/state/local-token", "env_file": "$DIR/.env", "install_dir": "$DIR"}
EOF
chmod 600 "$CONF_DIR/lokkal.json"

# ------------------------------------------------------------------------------------------------- démarrage
if [ "${NOKKER_SKIP_PULL:-0}" != 1 ]; then
  say "téléchargement des images (première fois : quelques minutes)"
  compose pull -q
fi
say "démarrage"
compose up -d --remove-orphans >/dev/null 2>"$TMP/up.log" || { cat "$TMP/up.log" >&2; die "le démarrage a échoué (journal : docker compose -p nokker logs)"; }
i=0
until [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$API_PORT/healthz")" = 200 ]; do
  i=$((i + 1)); [ $i -gt 120 ] && die "le noyau ne répond pas après 6 min : docker compose -p nokker logs core"
  sleep 3
done

# ------------------------------------------------------------------------------------------------- commande nokker
if [ "${NOKKER_NO_CLI:-0}" != 1 ]; then
  BIN=nokker-$os-$arch
  # pas "$TMP/nokker" : c'est le dossier du paquet extrait
  if fetch "$BIN" "$TMP/$BIN" 2>/dev/null && fetch "$BIN.sha256" "$TMP/$BIN.sha256" 2>/dev/null \
     && [ "$(sha256 "$TMP/$BIN")" = "$(cut -d' ' -f1 "$TMP/$BIN.sha256")" ] \
     && { verify_sig "$BIN"; [ $? -ne 1 ]; }; then
    install -m 755 "$TMP/$BIN" "$DIR/bin/nokker"
    case ":$PATH:" in *":$DIR/bin:"*) ;; *)
      for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        grep -q '.nokker/bin' "$rc" || printf '\n# Nokker Lokkal\nexport PATH="%s/bin:$PATH"\n' "$DIR" >> "$rc"
      done;;
    esac
  else
    warn "commande nokker non posée pour $os-$arch : absente de cette version, empreinte ou signature incorrecte (le web fonctionne)"
  fi
fi

URL="http://127.0.0.1:$WEB_PORT/"
say "Nokker Lokkal $VERSION est prêt : $URL"
say "dossier(s) de travail : $(getv NOKKER_HOST_WORKSPACE)"
[ -x "$DIR/bin/nokker" ] && say "terminal : nokker   (nouveau shell, ou export PATH=\"$DIR/bin:\$PATH\")"
if [ "$OPEN" = 1 ] && [ "$MODE" = install ]; then
  if [ "$os" = darwin ]; then open "$URL" 2>/dev/null || true
  elif need xdg-open && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then xdg-open "$URL" >/dev/null 2>&1 || true; fi
fi
