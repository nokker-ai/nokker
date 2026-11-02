#!/bin/sh
# Sauvegarde et restauration complètes d'une installation Lokkal (~/.nokker), posé par l'installeur à côté d'install.sh.
#   backup.sh backup  [--to <dossier>|s3://<bucket>/<préfixe>] [--keep N] [--encrypt] [--dir ~/.nokker]   (défaut ~/nokker-backups)
#   backup.sh restore <archive>|s3://<bucket>/<clé>              [--yes] [--dir ~/.nokker]
#   backup.sh list    [--to <dossier>|s3://<bucket>/<préfixe>]
# Contenu de l'archive (un seul .tar, chiffré en .tar.enc si une phrase de passe est donnée) : meta, .env, config/,
# state/ (jeton local, coffre local, skills, points de retour), vault/ (parts de déverrouillage et AppRole), les bases
# nokker et corthexis (pg_dump -Fc) et, avec le profil vault, le volume du coffre OpenBao. Ni les dossiers de travail de
# l'utilisateur (ils sont à lui), ni les modèles téléchargés (rechargés au démarrage).
# L'archive contient les secrets de l'installation (.env). Vers un dossier (disque, NAS monté) : 0600, chiffrée avec
# --encrypt ou dès que NOKKER_BACKUP_PASSPHRASE est posée, sinon en clair avec un avertissement. Vers S3 : toujours
# chiffrée. Phrase de passe : NOKKER_BACKUP_PASSPHRASE, sinon demandée au terminal. --keep N vaut pour un dossier et S3.
# list n'a pas besoin d'installation (bucket S3, dossier). Sous WSL, un chemin Windows (C:\…) est traduit (wslpath).
# S3 (tout service compatible) par curl --aws-sigv4, sans autre outil : NOKKER_BACKUP_S3_ENDPOINT (https://…),
# NOKKER_BACKUP_S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, lus dans l'environnement ou ~/.nokker/backup.env.
set -eu
say()  { printf '\033[1m›\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }
tty_ok() { ( : < /dev/tty ) 2>/dev/null; }   # -r /dev/tty ment sans terminal de contrôle (ssh -T, cron)

CMD=${1:-}; [ $# -gt 0 ] && shift
DIR="${HOME}/.nokker"; TO="${HOME}/nokker-backups"   # hors de ~/.nokker : --uninstall --purge ne les efface pas
KEEP=0; YES=0; SRC=""; ENCRYPT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TO=${2:?}; shift 2;;
    --keep) KEEP=${2:?}; shift 2;;
    --dir) DIR=${2:?}; shift 2;;
    --encrypt) ENCRYPT=1; shift;;
    --yes|-y) YES=1; shift;;
    -h|--help) sed -n '2,15p' "$0"; exit 0;;
    -*) die "option inconnue : $1";;
    *) SRC=$1; shift;;
  esac
done
case "$KEEP" in ''|*[!0-9]*) die "--keep : nombre attendu";; esac
winpath() { # chemin Windows (C:\… ou \\serveur\…) sous WSL → chemin Linux
  case "$1" in [A-Za-z]:[\\/]*|\\\\*) need wslpath && wslpath -u "$1" && return 0;; esac; printf '%s\n' "$1"; }
case "$TO" in s3://*) ;; *) TO=$(winpath "$TO");; esac
case "$SRC" in s3://*|'') ;; *) SRC=$(winpath "$SRC");; esac
[ -f "$DIR/backup.env" ] && { set -a; . "$DIR/backup.env"; set +a; }
installed() { # tout sauf `list` : l'installation est nécessaire
  [ -f "$DIR/.env" ] || die "aucune installation Lokkal dans $DIR"
  PROJECT=$(getv COMPOSE_PROJECT_NAME); PROJECT=${PROJECT:-nokker}
  PGIMG=$(sed -n 's/^ *image: \(pgvector\/pgvector@sha256:[0-9a-f]*\)$/\1/p' "$DIR/docker-compose.yml" | head -1)
  [ -n "$PGIMG" ] || die "image postgres introuvable dans docker-compose.yml"
}
getv() { sed -n "s/^$1=//p" "$DIR/.env" | tail -1; }
dc() { docker compose -p "$PROJECT" --project-directory "$DIR" -f "$DIR/docker-compose.yml" --env-file "$DIR/.env" "$@"; }
vkey() { # clé de tri semver (préversions comprises) : X Y Z rc sous-rc
  printf '%s\n' "$1" | awk -F'[.-]' '{rc = ($4 == "rc") ? $5 : 999999; printf "%d %d %d %d %d\n", $1, $2, $3, rc, $6}'; }
newer() { [ "$1" != "$2" ] && [ "$(printf '%s %s\n%s %s\n' "$(vkey "$1")" "$1" "$(vkey "$2")" "$2" \
  | sort -n -k1,1 -k2,2 -k3,3 -k4,4 -k5,5 | tail -1 | cut -d' ' -f6)" = "$1" ]; }   # newer A B : A > B

passphrase() {
  [ -n "${NOKKER_BACKUP_PASSPHRASE:-}" ] && return 0
  tty_ok || die "phrase de passe requise : NOKKER_BACKUP_PASSPHRASE"
  printf 'Phrase de passe de la sauvegarde : ' > /dev/tty; stty -echo < /dev/tty; read -r NOKKER_BACKUP_PASSPHRASE < /dev/tty
  stty echo < /dev/tty; printf '\n' > /dev/tty; [ -n "$NOKKER_BACKUP_PASSPHRASE" ] || die "phrase de passe vide"
  export NOKKER_BACKUP_PASSPHRASE
}
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 300000 -salt -pass env:NOKKER_BACKUP_PASSPHRASE -in "$1" -out "$2"; }
dec() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 300000 -pass env:NOKKER_BACKUP_PASSPHRASE -in "$1" -out "$2" \
          || die "déchiffrement impossible (phrase de passe ?)"; }

# --- S3 ---------------------------------------------------------------------------------------------------------
s3() { # s3 <méthode> <bucket/clé> [curl…]
  _m=$1; _k=$2; shift 2   # noms à part : sh n'a pas de variables locales
  : "${NOKKER_BACKUP_S3_ENDPOINT:?NOKKER_BACKUP_S3_ENDPOINT absent}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}"
  curl -fsS -X "$_m" --aws-sigv4 "aws:amz:${NOKKER_BACKUP_S3_REGION:-us-east-1}:s3" \
    --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" "${NOKKER_BACKUP_S3_ENDPOINT%/}/$_k" "$@"
}
s3_list() { # s3_list <bucket/préfixe> → clés
  _b=${1%%/*}; _p=${1#"$_b"}; _p=${_p#/}
  s3 GET "$_b?list-type=2&prefix=$_p" | tr '<' '\n' | sed -n 's/^Key>//p'
}

# --- backup -----------------------------------------------------------------------------------------------------
do_backup() {
  case "$TO" in s3://*) remote=1;; *) remote=0;; esac
  if [ "$remote" = 1 ] || [ -n "$ENCRYPT" ] || [ -n "${NOKKER_BACKUP_PASSPHRASE:-}" ]; then
    need openssl || die "openssl requis pour chiffrer la sauvegarde"; passphrase; ENCRYPT=1
  fi
  need curl || [ "$remote" = 0 ] || die "curl requis pour S3"
  dc ps --status running --services 2>/dev/null | grep -qx postgres || die "postgres ne tourne pas (nokker doctor --fix)"
  TS=$(date -u +%Y%m%dT%H%M%SZ); W=$(mktemp -d); trap 'rm -rf "$W"' EXIT; umask 077
  B="$W/nokker-backup-$TS"; mkdir -p "$B/db"
  ver=$(sed -n 's/^NOKKER_VERSION=//p' "$DIR/VERSION" 2>/dev/null || getv NOKKER_VERSION)
  printf '{"format": 1, "created": "%s", "version": "%s", "project": "%s", "profiles": "%s"}\n' \
    "$TS" "$ver" "$PROJECT" "$(getv COMPOSE_PROFILES)" > "$B/meta.json"
  say "bases (pg_dump)"
  for db in nokker corthexis; do dc exec -T postgres pg_dump -U postgres -Fc "$db" > "$B/db/$db.dump"; done
  dc exec -T postgres pg_dumpall -U postgres --globals-only --no-role-passwords > "$B/db/globals.sql"
  say "fichiers (.env, config, state, vault)"
  tar -C "$DIR" -cf "$B/files.tar" .env config state vault $( [ -f "$DIR/backup.env" ] && echo backup.env ) $( [ -d "$DIR/home" ] && echo home )
  if dc ps --services 2>/dev/null | grep -qx openbao; then
    say "coffre OpenBao (volume ${PROJECT}_vault, coffre arrêté le temps de la copie)"
    dc stop openbao >/dev/null 2>&1 || true
    docker run --rm --network none -v "${PROJECT}_vault:/v:ro" --entrypoint tar "$PGIMG" -C /v -cf - . > "$B/vault-volume.tar"
    dc start openbao >/dev/null 2>&1 || true
  fi
  ( cd "$B" && sha256sum meta.json files.tar db/* $( [ -f vault-volume.tar ] && echo vault-volume.tar ) > SHA256SUMS ) 2>/dev/null \
    || ( cd "$B" && shasum -a 256 meta.json files.tar db/* > SHA256SUMS )
  tar -C "$W" -cf "$W/out.tar" "nokker-backup-$TS"
  name="nokker-backup-$TS.tar"; out="$W/out.tar"
  if [ "$ENCRYPT" = 1 ]; then enc "$W/out.tar" "$W/out.tar.enc"; out="$W/out.tar.enc"; name="$name.enc"; fi
  size=$(wc -c < "$out" | tr -d ' ')
  case "$TO" in
    s3://*) s3 PUT "${TO#s3://}/$name" -T "$out" -o /dev/null; dest="$TO/$name"
       if [ "$KEEP" -gt 0 ]; then
         b=${TO#s3://}; b=${b%%/*}
         s3_list "${TO#s3://}" | grep 'nokker-backup-' | sort -r | tail -n +"$((KEEP + 1))" \
           | while read -r k; do s3 DELETE "$b/$k" -o /dev/null && say "rétention : supprimé s3://$b/$k"; done
       fi;;
    *) mkdir -p "$TO"; chmod 700 "$TO"; cp "$out" "$TO/$name"; chmod 600 "$TO/$name"; dest="$TO/$name"
       [ "$ENCRYPT" = 1 ] || warn "sauvegarde NON chiffrée (contient les secrets de l'installation) : ne la copiez pas ailleurs telle quelle"
       if [ "$KEEP" -gt 0 ]; then
         ls -1 "$TO"/nokker-backup-*.tar* 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r f; do rm -f "$f"; done
       fi;;
  esac
  say "sauvegarde : $dest ($size octets)"
}

# --- restore ----------------------------------------------------------------------------------------------------
do_restore() {
  [ -n "$SRC" ] || die "archive à restaurer ? (backup.sh list)"
  W=$(mktemp -d); trap 'rm -rf "$W"' EXIT; umask 077
  case "$SRC" in
    s3://*) s3 GET "${SRC#s3://}" -o "$W/in"; base=${SRC##*/};;
    *) [ -f "$SRC" ] || die "introuvable : $SRC"; cp "$SRC" "$W/in"; base=${SRC##*/};;
  esac
  case "$base" in *.enc) need openssl || die "openssl requis"; passphrase; dec "$W/in" "$W/in.tar";; *) mv "$W/in" "$W/in.tar";; esac
  tar -C "$W" -xf "$W/in.tar"; B=$(ls -d "$W"/nokker-backup-* 2>/dev/null | head -1); [ -d "$B" ] || die "archive invalide"
  ( cd "$B" && if need sha256sum; then sha256sum -c --quiet SHA256SUMS; else shasum -a 256 -c -q SHA256SUMS; fi ) \
    || die "empreintes incorrectes : archive abîmée"
  bver=$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$B/meta.json")
  iver=$(sed -n 's/^NOKKER_VERSION=//p' "$DIR/VERSION" 2>/dev/null || true)
  # Les migrations ne vont que vers l'avant : une sauvegarde plus récente que le noyau installé ne se restaure pas.
  if [ -n "$bver" ] && [ -n "$iver" ] && newer "$bver" "$iver"; then
    die "sauvegarde en $bver, installation en $iver : mettez d'abord à jour (nokker update --version $bver)"
  fi
  if [ "$YES" != 1 ]; then
    tty_ok || die "restauration : confirmez avec --yes"
    printf 'Remplacer les données de %s par la sauvegarde du %s ? [o/N] ' "$DIR" "$(sed -n 's/.*"created": "\([^"]*\)".*/\1/p' "$B/meta.json")" > /dev/tty
    read -r a < /dev/tty; case "$a" in o|O|oui|y|Y|yes) ;; *) die "abandon";; esac
  fi
  say "arrêt de Lokkal"; dc stop >/dev/null 2>&1 || true
  say "fichiers"; rm -rf "${DIR:?}/state" "${DIR:?}/config" "${DIR:?}/vault" "${DIR:?}/home"; tar -C "$DIR" -xpf "$B/files.tar"   # -p : droits d'origine (config/ lisible par les services, malgré umask 077)
  chmod 600 "$DIR/.env"; chmod 755 "$DIR/config"; chmod 644 "$DIR"/config/*
  say "bases (volume postgres recréé)"
  dc rm -fsv postgres >/dev/null 2>&1 || true; docker volume rm "${PROJECT}_postgres" >/dev/null 2>&1 || true
  dc up -d --wait postgres >/dev/null
  dc run --rm -T roles >/dev/null
  for db in nokker corthexis; do
    dc exec -T postgres pg_restore -U postgres -d "$db" --clean --if-exists --exit-on-error < "$B/db/$db.dump"
  done
  if [ -f "$B/vault-volume.tar" ]; then
    say "coffre OpenBao"
    docker volume rm "${PROJECT}_vault" >/dev/null 2>&1 || true; docker volume create "${PROJECT}_vault" >/dev/null
    docker run --rm -i --network none -v "${PROJECT}_vault:/v" --entrypoint tar "$PGIMG" -C /v -xf - < "$B/vault-volume.tar"
  fi
  say "redémarrage"; dc up -d --wait >/dev/null 2>&1 || dc up -d
  say "restauration terminée ($bver, $(sed -n 's/.*"created": "\([^"]*\)".*/\1/p' "$B/meta.json")) — vérifiez avec : nokker doctor"
}

do_list() {
  case "$TO" in
    s3://*) b=${TO#s3://}; b=${b%%/*}; s3_list "${TO#s3://}" | grep 'nokker-backup-' | sort -r | sed "s|^|s3://$b/|";;
    *) ls -1 "$TO"/nokker-backup-*.tar* 2>/dev/null | sort -r || true;;
  esac
}

case "$CMD" in
  backup) installed; do_backup;;
  restore) installed; do_restore;;
  list) do_list;;
  *) sed -n '2,15p' "$0"; exit 2;;
esac
