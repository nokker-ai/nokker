#!/bin/sh
# Langue des messages d'install.sh et de backup.sh (anglais par défaut, français par la règle de la commande nokker).
#   sh deploy/lokkal/tests/i18n.sh              (sous sh, puis dash, bash et busybox sh s'ils sont là)
# Statique : chaque message est un format anglais entre apostrophes (aucune variable dedans), présent dans fr_text,
# avec autant de « % » dans les deux langues ; aucune entrée du catalogue n'est morte.
# Exécution : ordre --lang > NOKKER_LANG > LC_ALL > LC_MESSAGES > LANG, valeur vide sautée, --lang invalide refusé,
# aide dans les deux langues, format commençant par « -- » (printf --). Rien n'est installé : seuls les chemins qui
# s'arrêtent avant Docker sont joués.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ------------------------------------------------------------------------------------------------- statique
for f in install.sh backup.sh; do
  src="$HERE/$f"
  sed -n '/^# >>> fr_text/,/^# <<< fr_text/p' "$src" > "$TMP/cat.sh"
  [ -s "$TMP/cat.sh" ] || { fail "$f : catalogue fr_text introuvable"; continue; }
  . "$TMP/cat.sh"
  # Code sans commentaires (ligne de commentaire, ou « espace # espace » en fin de ligne), hors catalogue et aide.
  sed -e '/^# >>> fr_text/,/^# <<< fr_text/d' -e "/^usage() {/,/^}/d" -e 's/^[[:space:]]*#.*$//' \
      -e 's/[[:space:]]#[[:space:]].*$//' "$src" > "$TMP/code.sh"
  bad=$(grep -nE "(^|[^A-Za-z_])(say|warn|die|ask|msg) +[^' ]" "$TMP/code.sh" | grep -vE "(say|warn|die|ask|msg)\(\)" | grep -vF 'msg "$@"' || true)   # relais des fonctions de message
  [ -z "$bad" ] && ok || fail "$f : message sans format anglais entre apostrophes : $bad"
  grep -oE "(^|[^A-Za-z_])(say|warn|die|ask|msg) +'[^']*'" "$TMP/code.sh" | sed "s/^[^']*'//; s/'\$//" | sort -u > "$TMP/used"
  [ -s "$TMP/used" ] && ok || fail "$f : aucun message trouvé"
  while IFS= read -r en; do
    if fr=$(fr_text "$en"); then
      a=$(printf '%s' "$en" | tr -cd '%' | wc -c); b=$(printf '%s' "$fr" | tr -cd '%' | wc -c)
      [ "$a" = "$b" ] && ok || fail "$f : nombre de % différent : « $en » / « $fr »"
    else fail "$f : pas de traduction : « $en »"; fi
  done < "$TMP/used"
  # Entrées du catalogue jamais utilisées ([y/N] : utilisé par ask)
  sed -n "s/^    '\(.*\)') f=.*/\1/p" "$TMP/cat.sh" | sort -u > "$TMP/keys"
  dead=$(comm -23 "$TMP/keys" "$TMP/used" | grep -vxF ' [y/N] ' || true)
  [ -z "$dead" ] && ok || fail "$f : entrées mortes : $dead"
done

# ------------------------------------------------------------------------------------------------- exécution
# run SHELL SCRIPT [VAR=val…] -- ARGS… → sortie (stdout+stderr) dans $OUT ; environnement vide sauf HOME/PATH
run() {
  sh_=$1; script=$2; shift 2
  envs=""; while [ $# -gt 0 ] && [ "$1" != -- ]; do envs="$envs $1"; shift; done; [ $# -gt 0 ] && shift
  # shellcheck disable=SC2086
  OUT=$(env -i HOME="$TMP/home" PATH="$PATH" $envs $sh_ "$HERE/$script" "$@" 2>&1); RC=$?
}
expect() { # expect LIBELLÉ TEXTE
  case "$OUT" in *"$2"*) ok;; *) fail "$SH : $1 : attendu « $2 », reçu : $OUT";; esac
}

SHELLS="sh"
for s in dash bash; do command -v "$s" >/dev/null 2>&1 && SHELLS="$SHELLS $s"; done
command -v busybox >/dev/null 2>&1 && SHELLS="$SHELLS busybox_sh"
mkdir -p "$TMP/home"
for SH in $SHELLS; do
  case "$SH" in busybox_sh) X="busybox sh";; *) X=$SH;; esac
  EN='unknown option: --bogus'; FR='option inconnue : --bogus'
  run "$X" install.sh -- --bogus;                                         expect "défaut" "$EN"
  run "$X" install.sh LANG=C -- --bogus;                                  expect "LANG=C" "$EN"
  run "$X" install.sh LANG=fr_CH.UTF-8 -- --bogus;                        expect "LANG=fr_CH" "$FR"
  run "$X" install.sh LANG=fr_CH.UTF-8 LC_ALL=en_US.UTF-8 -- --bogus;     expect "LC_ALL prime" "$EN"
  run "$X" install.sh LANG=en_US.UTF-8 LC_MESSAGES=fr_FR.UTF-8 -- --bogus; expect "LC_MESSAGES prime sur LANG" "$FR"
  run "$X" install.sh LC_ALL= LANG=fr_FR.UTF-8 -- --bogus;                expect "LC_ALL vide sauté" "$FR"
  run "$X" install.sh NOKKER_LANG=en LANG=fr_FR.UTF-8 -- --bogus;         expect "NOKKER_LANG prime" "$EN"
  run "$X" install.sh NOKKER_LANG=french -- --bogus;                      expect "NOKKER_LANG=french (comme /^fr/i)" "$FR"
  run "$X" install.sh NOKKER_LANG=fr -- --bogus --lang en;                expect "--lang en prime (fin)" "$EN"
  run "$X" install.sh -- --lang fr --bogus;                               expect "--lang fr (début)" "$FR"
  run "$X" install.sh -- --lang de;                                       expect "--lang invalide" "--lang: en or fr expected (got de)"
  [ "$RC" = 1 ] && ok || fail "$SH : --lang invalide : code $RC"
  run "$X" install.sh -- --help
  expect "aide en" "Options:"; expect "aide en, --lang" "--lang en|fr"
  [ "$RC" = 0 ] && ok || fail "$SH : --help : code $RC"
  run "$X" install.sh LANG=fr_FR.UTF-8 -- --help;                         expect "aide fr" "Options :"

  run "$X" backup.sh -- list --keep x;                                    expect "backup en (format en --)" "--keep: a number is expected"
  run "$X" backup.sh -- --lang fr list --keep x;                          expect "backup --lang fr" "--keep : nombre attendu"
  run "$X" backup.sh NOKKER_LANG=fr -- restore --dir "$TMP/none";         expect "backup fr" "aucune installation Lokkal dans $TMP/none"
  run "$X" backup.sh -- restore --dir "$TMP/none";                        expect "backup en" "no Lokkal install in $TMP/none"
  run "$X" backup.sh;                                                     expect "backup usage" "Full backup and restore"
  [ "$RC" = 2 ] && ok || fail "$SH : backup.sh sans commande : code $RC"
  run "$X" backup.sh LANG=fr_CH.UTF-8 -- --help;                          expect "backup aide fr" "Sauvegarde et restauration"
  run "$X" backup.sh -- list --to "$TMP/empty";                           [ "$RC" = 0 ] && ok || fail "$SH : list vide : code $RC ($OUT)"
done

# ------------------------------------------------------------------------------------------------- parcours complet
# install.sh de bout en bout (installation, --update relancé dans l'installeur de la version cible, --uninstall --purge)
# avec un faux docker et un faux curl, dans un HOME temporaire : la langue suit les relances (NOKKER_LANG), et l'en-tête
# de .env et les commentaires de home.yaml sont écrits dans la langue choisie. Paquet construit par bundle.sh.
if command -v bash >/dev/null 2>&1 && tar --version 2>/dev/null | grep -q GNU; then
  FAKE="$TMP/fakebin"; mkdir -p "$FAKE"
  cat > "$FAKE/docker" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "volume inspect") exit 1;;
  "context inspect") echo "unix:///var/run/docker.sock";;
esac
exit 0
EOF
  cat > "$FAKE/curl" <<'EOF'
#!/bin/sh
for a in "$@"; do case "$a" in */healthz) printf 200; exit 0;; esac; done
exit 7
EOF
  chmod +x "$FAKE/docker" "$FAKE/curl"
  REL="$TMP/rel"; bash "$HERE/bundle.sh" 0.0.0-test "$REL" >/dev/null 2>&1 && ok || fail "bundle.sh"
  e2e() { # e2e NOM LANGUE [ARGS…] → $OUT ; HOME et config propres à NOM
    h="$TMP/e2e-$1"; envlang=$2; shift 2   # sh n'a pas de variables locales : pas « lg », celle de la boucle
    OUT=$(env -i HOME="$h" XDG_CONFIG_HOME="$h/.config" PATH="$FAKE:$PATH" NOKKER_LANG="$envlang" NOKKER_RELEASE_BASE="file://$REL" \
          NOKKER_SKIP_PULL=1 NOKKER_NO_CLI=1 sh "$INST" --version 0.0.0-test --no-open --yes "$@" 2>&1); RC=$?
  }
  SH=e2e
  for lg in en fr; do
    h="$TMP/e2e-$lg"; mkdir -p "$h/.nokker/state"; printf tok > "$h/.nokker/state/local-token"
    INST="$HERE/install.sh"
    e2e "$lg" "$lg" --home --home-lan-ip 192.168.1.5
    [ "$RC" = 0 ] && ok || fail "e2e $lg : installation, code $RC : $OUT"
    if [ "$lg" = en ]; then
      expect "installation en" "Nokker Lokkal 0.0.0-test is ready: http://127.0.0.1:"
      expect "home en" "home gateway: listening on 192.168.1.5:8801/8802"
      grep -q '^# Nokker Lokkal: generated by install.sh on ' "$h/.nokker/.env" && ok || fail "e2e en : en-tête .env"
      grep -q '^# Lokkal home gateway' "$h/.nokker/home/home.yaml" && ok || fail "e2e en : home.yaml"
    else
      expect "installation fr" "Nokker Lokkal 0.0.0-test est prêt : http://127.0.0.1:"
      expect "home fr" "passerelle du foyer : écoute sur 192.168.1.5:8801/8802"
      grep -q '^# Nokker Lokkal — généré par install.sh le ' "$h/.nokker/.env" && ok || fail "e2e fr : en-tête .env"
      grep -q '^# Passerelle du foyer Lokkal' "$h/.nokker/home/home.yaml" && ok || fail "e2e fr : home.yaml"
    fi
    grep -q '^allow: \[\]$' "$h/.nokker/home/home.yaml" && ok || fail "e2e $lg : home.yaml sans destination"
    grep -q '^NOKKER_CONNECTOR_KEY=' "$h/.nokker/.env" && ok || fail "e2e $lg : .env complet"
    # --update depuis un installeur DIFFÉRENT de celui du paquet : relance dans l'installeur cible. La langue vient de
    # --lang, l'environnement dit l'autre : la relance doit garder celle de --lang (NOKKER_LANG=$L, pas l'héritage).
    { cat "$HERE/install.sh"; echo "# autre version"; } > "$TMP/other-install.sh"; INST="$TMP/other-install.sh"
    if [ "$lg" = en ]; then other=fr; else other=en; fi
    e2e "$lg" "$other" --update --lang "$lg"
    [ "$RC" = 0 ] && ok || fail "e2e $lg : --update, code $RC : $OUT"
    if [ "$lg" = en ]; then expect "relance en" "installer of version 0.0.0-test"; expect "après relance en" "is ready"
    else expect "relance fr" "installeur de la version 0.0.0-test"; expect "après relance fr" "est prêt"; fi
    INST="$HERE/install.sh"
    e2e "$lg" "$lg" --uninstall --purge
    [ "$RC" = 0 ] && ok || fail "e2e $lg : --uninstall --purge, code $RC : $OUT"
    if [ "$lg" = en ]; then expect "purge en" "Nokker Lokkal uninstalled, data erased."
    else expect "purge fr" "Nokker Lokkal désinstallé, données effacées."; fi
    [ ! -e "$h/.nokker" ] && ok || fail "e2e $lg : ~/.nokker effacé"
  done
else
  printf 'bash ou tar GNU absent : parcours complet non joué\n'
fi

# ------------------------------------------------------------------------------------------------- PowerShell
if command -v pwsh >/dev/null 2>&1; then
  if pwsh -NoProfile -NonInteractive -File "$HERE/tests/i18n.ps1"; then ok; else fail "tests/i18n.ps1"; fi
  # emission du code d'acces par install.ps1 (faux wsl.exe et faux curl, sous pwsh Linux) : voir tests/ps1-code.ps1
  if pwsh -NoProfile -NonInteractive -File "$HERE/tests/ps1-code.ps1"; then ok; else fail "tests/ps1-code.ps1"; fi
else
  printf 'pwsh absent : tests/i18n.ps1 non joué\n'
fi

# ------------------------------------------------------------------------------------------------- tunnel ssh
# remote_hint (lue dans install.sh) : par ssh, la ligne du tunnel avec le VRAI port choisi et l'hôte vu par le client,
# dans les deux langues ; hors ssh, rien.
{ sed -n '/^# >>> fr_text/,/^# <<< fr_text/p' "$HERE/install.sh"; sed -n '/^msg()  {/,/^say()  {/p' "$HERE/install.sh"
  sed -n '/^remote_hint() {/,/^}/p' "$HERE/install.sh"; } > "$TMP/rh.sh"
grep -q '^remote_hint() {' "$TMP/rh.sh" && ok || fail "remote_hint introuvable dans install.sh"
for L_ in en fr; do
  out=$(L=$L_ SSH_CONNECTION='203.0.113.9 51234 198.51.100.7 22' SSH_CLIENT='' sh -c ". $TMP/rh.sh; remote_hint 7423")
  case $L_ in
    en) want='remote machine? From your computer: ssh -L 7423:127.0.0.1:7423 '"$(id -un)"'@198.51.100.7, then open the link';;
    fr) want='machine distante ? Depuis ton poste : ssh -L 7423:127.0.0.1:7423 '"$(id -un)"'@198.51.100.7, puis ouvre le lien';;
  esac
  printf '%s' "$out" | grep -qF "$want" && ok || fail "remote_hint $L_ : « $out » au lieu de « $want »"
  out=$(L=$L_ SSH_CONNECTION='' SSH_CLIENT='' sh -c ". $TMP/rh.sh; remote_hint 7423")
  [ -z "$out" ] && ok || fail "remote_hint $L_ hors ssh : « $out » au lieu de rien"
done
grep -q '^remote_hint "\$WEB_PORT"$' "$HERE/install.sh" && ok || fail "install.sh n'appelle pas remote_hint avec le port web choisi"

printf '%s : %d contrôles, %d en échec\n' "$(basename "$0")" "$N" "$FAIL"
[ "$FAIL" = 0 ]
