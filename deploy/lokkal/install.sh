#!/bin/sh
# nokker-install-capabilities: sites
# nokker-install-capabilities: desktop
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
#   --vault             coffre OpenBao pour les secrets du noyau (profil compose vault), descellé automatiquement
#   --vault-manual      idem, mais la part de descellement est affichée une fois et redemandée à chaque redémarrage
#   --local-llm         moteur d'inférence local (Ollama, CPU, profil compose local-llm), proposé dans /setup
#   --local-llm-model M modèle Ollama à tirer (défaut : qwen3:4b) ; implique --local-llm
#   --sites             sites connectés (profil sites : navigateur des sites, écran de passation ; docs/CONNECTED-SITES.md) ;
#                       image PRIVÉE (§3.1.1) : machine liée à un compte nokker.ai, amd64 seulement ; vérifiée par un
#                       cosign épinglé (sha256) posé dans ~/.nokker/libexec, hors du PATH
#   --sites-only        sur une installation existante, sans paquet : (re)tire et revérifie sites-chrome, allume le
#                       profil (`nokker sites enable` / `nokker sites update`)
#   --desktop           Bureau, mode isolé (profil desktop : bureau virtuel Xvfb, gnome-text-editor, Thunar, piloté par
#                       l'arbre d'accessibilité ; docs/DESKTOP.md) ; amd64 seulement, aucune sortie réseau
#   --desktop-session   GELÉ (refusé, « pas encore disponible ») ; quand il sera rouvert : Bureau, mode session (Linux, X11, amd64) : Nokker travaille dans les applis du bureau de la
#                       personne. Le texte de mise en place (deploy/desktop/session-setup.json, figé) est affiché et
#                       une réponse « oui » AU TERMINAL est exigée (--yes ne vaut pas accord) ; unité systemd --user
#                       nokker-desktop.service, jamais posée par défaut. Un seul mode par machine.
#   --home              passerelle du foyer (profil home : Home Assistant, enceintes, écrans ; docs/LOKKAL-HOME-BRIDGE.md),
#                       écoute sur l'adresse LAN de l'hôte seulement ; ~/.nokker/home/home.yaml livré sans destination
#   --home-mdns         en plus : découverte des écrans Cast/AirPlay (réseau de l'hôte, Linux seulement) ; implique --home
#   --home-lan-ip IP    adresse LAN (privée) de l'hôte vue par les appareils du foyer (défaut : détectée)
#   --lang en|fr        langue des messages (défaut : NOKKER_LANG, LC_ALL, LC_MESSAGES, LANG ; anglais sinon)
# Variables : NOKKER_VERSION (comme --version, qui prime). Tests : NOKKER_RELEASE_BASE (URL des pièces de release), NOKKER_SKIP_PULL=1, NOKKER_NO_CLI=1.
set -eu

REPO=nokker-ai/nokker
VERSION="${NOKKER_VERSION:-}"; WORKSPACE=""; DIR="${HOME}/.nokker"; YES=0; SITES=0; DESKTOP=0; DESKTOP_MODE=""; MODE=install; PURGE=0; OPEN=1; VAULT=""; LLM=0; LLM_MODEL=""; HOME_BRIDGE=0; HOME_MDNS=0; HOME_LAN_IP=""
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nokker"

# ------------------------------------------------------------------------------------------------- langue des messages
# Même règle que la commande nokker : --lang en|fr > NOKKER_LANG > LC_ALL > LC_MESSAGES > LANG (la première variable non
# vide) ; français si elle commence par « fr », anglais sinon. Le texte source est l'anglais (formats printf entre
# apostrophes, sans variable : les valeurs passent en arguments) ; fr_text en donne la traduction. Test :
# deploy/lokkal/tests/i18n.sh (chaque format a sa traduction, mêmes %s). La langue choisie passe aux relances
# (groupe docker, installeur de la version cible) par NOKKER_LANG, jamais par --lang, qu'un installeur plus ancien refuse.
lang_of() { case "$1" in [Ff][Rr]*) echo fr;; *) echo en;; esac; }   # comme /^fr/i de la commande nokker
# Langue de repli du NOYAU (NOKKER_LANG du .env, passée au conteneur core) : fr, en, de ou it, d'après la même règle mais
# sans se limiter aux deux langues de l'installeur (--lang en|fr l'emporte). Calculée une fois, sur l'environnement
# d'ORIGINE, et transmise aux relances par NOKKER_INSTALL_KLANG (les relances reçoivent NOKKER_LANG=en|fr).
klang_of() { case "$1" in [Ff][Rr]*) echo fr;; [Dd][Ee]*) echo de;; [Ii][Tt]*) echo it;; *) echo en;; esac; }
KLANG=${NOKKER_INSTALL_KLANG:-$(klang_of "${NOKKER_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}")}
L=$(lang_of "${NOKKER_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}")
LANG_OPT=""; n=$#   # --lang retiré des arguments (n'importe où), sa valeur gardée
while [ "$n" -gt 0 ]; do
  a=$1; shift; n=$((n - 1))
  if [ "$a" = --lang ]; then LANG_OPT=${1:-}; [ "$n" -gt 0 ] && { shift; n=$((n - 1)); }
  else set -- "$@" "$a"; fi
done
case "$LANG_OPT" in '') ;; [Ee][Nn]|[Ee][Nn][_-]*) L=en;; [Ff][Rr]|[Ff][Rr][_-]*) L=fr;; *) LANG_OPT="!$LANG_OPT";; esac
[ -n "$LANG_OPT" ] && [ "${LANG_OPT#!}" = "$LANG_OPT" ] && KLANG=$L
NOKKER_LANG=$L; NOKKER_INSTALL_KLANG=$KLANG; export NOKKER_LANG NOKKER_INSTALL_KLANG
# Arguments d'origine, quotés pour sh -c (relance sous le groupe docker juste après son installation)
ARGS=""; for a in "$@"; do ARGS="$ARGS '$(printf %s "$a" | sed "s/'/'\\\\''/g")'"; done

# >>> fr_text (catalogue : une ligne par message, format anglais → format français)
fr_text() {
  case "$1" in
    'unknown option: %s') f="option inconnue : %s";;
    '--lang: en or fr expected (got %s)') f="--lang : en ou fr attendu (reçu %s)";;
    'running as root: Lokkal installs for the current user; prefer your own account.') f="lancé en root : Lokkal s'installe pour l'utilisateur courant ; préférez votre compte.";;
    'unsupported system: %s (Windows: install.ps1)') f="système non pris en charge : %s (Windows : install.ps1)";;
    'unsupported architecture: %s') f="architecture non prise en charge : %s";;
    'curl is required') f="curl est requis";;
    'the docker project %s belongs to another install (%s): nothing was touched. Install with another --dir, or stop the other one from its account.') f="le projet docker « %s » appartient à une autre installation (%s) : rien n'a été touché. Installez avec un autre --dir, ou arrêtez l'autre depuis son compte.";;
    'the docker volume %s_postgres already exists (another install, or an old install removed without --purge): nothing was touched. If it is yours and can go: docker volume rm %s_postgres') f="le volume docker %s_postgres existe déjà (autre installation, ou ancienne installation retirée sans --purge) : rien n'a été touché. S'il est à vous et à jeter : docker volume rm %s_postgres";;
    'nothing installed in %s') f="rien d'installé dans %s";;
    'ALSO erase the Nokker database, memory and state (cannot be undone)?') f="Effacer AUSSI la base, la mémoire et l'état de Nokker (irréversible) ?";;
    'aborted') f="abandon";;
    'config/config.yaml: %s: true added (reply language of Lokkal; backup: %s)') f="config/config.yaml : %s: true ajouté (langue des réponses de Lokkal ; sauvegarde : %s)";;
    'config/config.yaml: adding the reply language keys would change more than those lines: nothing written: add reply_language: true and reply_language_tail: true by hand') f="config/config.yaml : l'ajout des clés de langue changerait plus que ces lignes : rien n'est écrit : ajoute reply_language: true et reply_language_tail: true à la main";;
    'NOKKER_ACCORD_LAN=%s refused (give the explicit LAN address and port of this machine): ACCORD relay off') f="NOKKER_ACCORD_LAN=%s refusé (donnez l'adresse LAN et le port explicites de cette machine) : relais ACCORD éteint";;
    'ACCORD relay published on %s only (profile accord)') f="relais ACCORD publié sur %s seulement (profil accord)";;
    'lokkal.json kept: it points to another install (%s)') f="lokkal.json gardé : il désigne une autre installation (%s)";;
    'nokker command: %s thread(s) of this install forgotten (%s)') f="commande nokker : %s fil(s) de cette installation oublié(s) (%s)";;
    'nokker command: remembered threads forgotten (%s removed)') f="commande nokker : fils mémorisés oubliés (%s supprimé)";;
    'sandboxes left by the previous version (legacy label %s, network %s), removed: %s') f="bacs à sable laissés par la version d'avant (ancienne étiquette %s, réseau %s), supprimés : %s";;
    'sandboxes with the legacy label %s on the shared network %s, not removed (another install may own them): %s') f="bacs à sable à l'ancienne étiquette %s sur le réseau commun %s, non supprimés (ils peuvent être à une autre installation) : %s";;
    'could not remove the old sandboxes: docker rm -f %s') f="suppression des anciens bacs à sable impossible : docker rm -f %s";;
    'no terminal to confirm the erase: nothing was touched. Re-run with --yes to erase without a question.') f="pas de terminal pour confirmer l'effacement : rien n'a été touché. Relancez avec --yes pour effacer sans question.";;
    'Nokker Lokkal uninstalled, data erased. Your work folders were not touched.') f="Nokker Lokkal désinstallé, données effacées. Vos dossiers de travail n'ont pas été touchés.";;
    'Nokker Lokkal stopped; data kept in %s (--uninstall --purge to erase everything).') f="Nokker Lokkal arrêté ; données gardées dans %s (--uninstall --purge pour tout effacer).";;
    'Docker is here but your account cannot use it: sudo usermod -aG docker %s, then log in again.') f="Docker est là mais votre compte n'y a pas accès : sudo usermod -aG docker %s, puis reconnectez-vous.";;
    'Docker is installed but does not answer: start Docker (Docker Desktop / OrbStack / systemctl start docker).') f="Docker est installé mais ne répond pas : démarrez Docker (Docker Desktop / OrbStack / systemctl start docker).";;
    'Docker is required: install Docker Desktop (https://www.docker.com/products/docker-desktop/) or OrbStack (https://orbstack.dev), start it, then run this command again.') f="Docker est requis : installez Docker Desktop (https://www.docker.com/products/docker-desktop/) ou OrbStack (https://orbstack.dev), lancez-le, puis relancez cette commande.";;
    'Docker is not installed. Install it now (official get.docker.com script, asks for sudo)?') f="Docker n'est pas installé. L'installer maintenant (script officiel get.docker.com, demande sudo) ?";;
    'Docker is required: https://docs.docker.com/engine/install/') f="Docker est requis : https://docs.docker.com/engine/install/";;
    'Docker installed: continuing the install under the docker group') f="Docker installé : suite de l'installation sous le groupe docker";;
    'Docker installed: log in again (docker group), then run this command again.') f="Docker installé : reconnectez-vous (groupe docker), puis relancez cette commande.";;
    'cannot read LATEST on %s') f="LATEST illisible sur %s";;
    'cannot find the latest version (--version X.Y.Z)') f="impossible de trouver la dernière version (--version X.Y.Z)";;
    'version not found') f="version introuvable";;
    'Nokker Lokkal %s (%s-%s)') f="Nokker Lokkal %s (%s-%s)";;
    'download failed: %s/%s') f="téléchargement impossible : %s/%s";;
    'checksum not found: %s.sha256') f="empreinte introuvable : %s.sha256";;
    'wrong sha256 checksum of the package: aborted') f="empreinte sha256 du paquet incorrecte : abandon";;
    'package signature verified (cosign)') f="signature du paquet vérifiée (cosign)";;
    'cosign signature of the package invalid or missing: aborted') f="signature cosign du paquet invalide ou absente : abandon";;
    'installer of version %s') f="installeur de la version %s";;
    'config/%s updated') f="config/%s mis à jour";;
    'config/%s changed locally: kept; new version delivered as config/%s.new') f="config/%s modifié localement : gardé ; nouvelle version livrée dans config/%s.new";;
    '--update: no install in %s') f="--update : aucune installation dans %s";;
    'config/config.yaml: local.code.idle_ttl_s: 1800 added (backup: %s)') f="config/config.yaml : local.code.idle_ttl_s: 1800 ajouté (sauvegarde : %s)";;
    'config/config.yaml: adding local.code.idle_ttl_s would change more than one line: nothing written: add local.code.idle_ttl_s: 1800 by hand in config/config.yaml') f="config/config.yaml : l'ajout de local.code.idle_ttl_s changerait plus d'une ligne : rien n'est écrit : ajoute local.code.idle_ttl_s: 1800 à la main dans config/config.yaml";;
    'config/config.yaml: local.approval_wait_s: 10 added (backup: %s)') f="config/config.yaml : local.approval_wait_s: 10 ajouté (sauvegarde : %s)";;
    'config/config.yaml: adding local.approval_wait_s would change more than one line: nothing written: add local.approval_wait_s: 10 by hand in config/config.yaml') f="config/config.yaml : l'ajout de local.approval_wait_s changerait plus d'une ligne : rien n'est écrit : ajoute local.approval_wait_s: 10 à la main dans config/config.yaml";;
    'config/config.yaml: local.code.require_sandbox: true added (copy: %s)') f="config/config.yaml : local.code.require_sandbox: true ajouté (copie : %s)";;
    'config/config.yaml: no local.code block with a backend: line; add local.code.require_sandbox: true by hand, then run again') f="config/config.yaml : pas de bloc local.code avec une ligne backend: ; ajoutez local.code.require_sandbox: true à la main, puis relancez";;
    'config/config.yaml: adding local.code.require_sandbox would change more than one line: nothing written') f="config/config.yaml : l'ajout de local.code.require_sandbox changerait plus d'une ligne : rien n'est écrit";;
    'config/config.yaml: tiers.small.model %s -> %s (copy: %s)') f="config/config.yaml : tiers.small.model %s -> %s (copie : %s)";;
    'config/config.yaml: rewriting tiers.small.model would change more than one line: nothing written') f="config/config.yaml : la réécriture de tiers.small.model changerait plus d'une ligne : rien n'est écrit";;
    'config/config.yaml: backend %s: sandbox not required; switch local.code.backend to docker to require it') f="config/config.yaml : backend %s : bac non exigé ; passez local.code.backend à docker pour l'exiger";;
    'config/config.yaml: work folder %s overlaps the install folder %s: sandbox not required; separate the folders to require it') f="config/config.yaml : le dossier de travail %s recouvre le dossier d'installation %s : bac non exigé ; séparez les dossiers pour l'exiger";;
    'the core refused local.code.require_sandbox (SandboxRequired): previous config/config.yaml restored, core restarted') f="le noyau a refusé local.code.require_sandbox (SandboxRequired) : config/config.yaml d'avant remis, noyau relancé";;
    'work folder too wide: %s (choose a subfolder)') f="dossier de travail trop large : %s (choisissez un sous-dossier)";;
    '# Nokker Lokkal: generated by install.sh on %s. Local secrets: do not share.') f="# Nokker Lokkal — généré par install.sh le %s. Secrets locaux : ne pas partager.";;
    '--workspace ignored: existing install (edit NOKKER_HOST_WORKSPACE in %s/.env)') f="--workspace ignoré : installation existante (modifier NOKKER_HOST_WORKSPACE dans %s/.env)";;
    '--vault: this Lokkal version has no OpenBao vault') f="--vault : cette version de Lokkal n'a pas le coffre OpenBao";;
    'vault turned on for an existing install: migrate the secrets of the file vault (nokker vault migrate --from file --to openbao)') f="coffre activé sur une installation existante : secrets du coffre fichier à migrer (nokker vault migrate --from file --to openbao)";;
    '--local-llm: this Lokkal version has no local-llm profile') f="--local-llm : cette version de Lokkal n'a pas le profil local-llm";;
    '--local-llm-model: invalid model name: %s') f="--local-llm-model : nom de modèle invalide : %s";;
    'none') f="aucune";;
    '--home: private LAN address of the host not found (%s); set --home-lan-ip 192.168.x.y') f="--home : adresse LAN privée de l'hôte introuvable (%s) ; précisez --home-lan-ip 192.168.x.y";;
    '--home-mdns: Linux only (multicast on the host network)') f="--home-mdns : Linux seulement (multicast du réseau de l'hôte)";;
    '# Lokkal home gateway (docs/LOKKAL-HOME-BRIDGE.md). NO destination is allowed while `allow` is empty:') f="# Passerelle du foyer Lokkal (docs/LOKKAL-HOME-BRIDGE.md). AUCUNE destination autorisée tant que \`allow\` est vide :";;
    '# add the devices of your network (private addresses only), then: docker compose ... restart home-bridge') f="# ajoutez les appareils de votre réseau (adresses privées seulement), puis : docker compose … restart home-bridge";;
    'home gateway: listening on %s:8801/8802; destinations in %s/home/home.yaml (empty)') f="passerelle du foyer : écoute sur %s:8801/8802 ; destinations dans %s/home/home.yaml (vide)";;
    'downloading the images (first time: a few minutes)') f="téléchargement des images (première fois : quelques minutes)";;
    'sandbox image not pulled (%s): the first turn will pull it') f="image des bacs à sable non tirée (%s) : le premier tour la tirera";;
    'OpenBao vault') f="coffre OpenBao";;
    'the vault does not start') f="le coffre ne démarre pas";;
    'vault initialised without an AppRole in %s/vault/approle: the core will not have access (LOKKAL section 7, generate-root)') f="coffre initialisé sans AppRole dans %s/vault/approle : le noyau n'y aura pas accès (LOKKAL §7, generate-root)";;
    'vault initialisation failed (docker compose -p %s logs openbao)') f="initialisation du coffre échouée (docker compose -p %s logs openbao)";;
    'vault sealed (manual mode): docker compose -p %s exec openbao bao operator unseal') f="coffre scellé (mode manuel) : docker compose -p %s exec openbao bao operator unseal";;
    'starting') f="démarrage";;
    'start failed (log: docker compose -p %s logs)') f="le démarrage a échoué (journal : docker compose -p %s logs)";;
    'local model %s downloading in the background (docker compose -p %s logs -f local-llm-pull)') f="modèle local %s en téléchargement en arrière-plan (docker compose -p %s logs -f local-llm-pull)";;
    'the core does not answer after 6 min: docker compose -p %s logs core') f="le noyau ne répond pas après 6 min : docker compose -p %s logs core";;
    'nokker command not installed for %s-%s: missing from this version, or wrong checksum or signature (the web works)') f="commande nokker non posée pour %s-%s : absente de cette version, empreinte ou signature incorrecte (le web fonctionne)";;
    'Nokker Lokkal %s is ready: %s') f="Nokker Lokkal %s est prêt : %s";;
    'work folder(s): %s') f="dossier(s) de travail : %s";;
    'terminal: nokker   (new shell, or export PATH="%s/bin:$PATH")') f="terminal : nokker   (nouveau shell, ou export PATH=\"%s/bin:\$PATH\")";;
    'first setup (model): local token not found (%s); run the installer again once the core has started') f="premier réglage (modèle) : jeton local introuvable (%s) ; relance l'installeur une fois le noyau démarré";;
    'first setup (choose a model): %s') f="premier réglage (choisir un modèle) : %s";;
    'remote machine? From your computer: ssh -L %s:127.0.0.1:%s %s, then open the link') f="machine distante ? Depuis ton poste : ssh -L %s:127.0.0.1:%s %s, puis ouvre le lien";;
    '  this link works once, for 60 seconds; later: nokker open /setup') f="  ce lien ne sert qu'une fois, pendant 60 secondes ; plus tard : nokker open /setup";;
    'first setup (choose a model): run nokker open /setup') f="premier réglage (choisir un modèle) : lance nokker open /setup";;
    '--sites: connected sites need an amd64 (x86_64) machine; nothing was touched') f="--sites : les sites connectés demandent une machine amd64 (x86_64) ; rien n'a été touché";;
    'connected sites: local token not found') f="sites connectés : jeton local introuvable";;
    'connected sites: this machine is not linked to a nokker.ai account; link it (Account tab of the web app), then: nokker sites enable') f="sites connectés : machine non liée à un compte nokker.ai ; liez-la (onglet Compte du web), puis : nokker sites enable";;
    'connected sites: nokker.ai refused access to the image (account or machine); nothing was pulled') f="sites connectés : accès à l'image refusé par nokker.ai (compte ou machine) ; rien n'a été tiré";;
    'connected sites: too many access requests (6 per 24 h per account); try again in about %s h') f="sites connectés : trop de demandes d'accès (6 par 24 h et par compte) ; réessayez dans %s h environ";;
    'connected sites: unsupported architecture (amd64 only)') f="sites connectés : architecture non prise en charge (amd64 seulement)";;
    'connected sites: access request refused (HTTP 400)') f="sites connectés : demande d'accès refusée (HTTP 400)";;
    'connected sites: link with nokker.ai cut or nokker.ai unreachable; try again later: nokker sites enable') f="sites connectés : lien avec nokker.ai coupé ou nokker.ai injoignable ; réessayez plus tard : nokker sites enable";;
    'connected sites: private registry unavailable; try again later: nokker sites enable') f="sites connectés : registre privé indisponible ; réessayez plus tard : nokker sites enable";;
    'connected sites: image access unavailable (HTTP %s); try again later: nokker sites enable') f="sites connectés : accès à l'image indisponible (HTTP %s) ; réessayez plus tard : nokker sites enable";;
    'connected sites: this Lokkal version has no sites-chrome image') f="sites connectés : cette version de Lokkal n'a pas d'image sites-chrome";;
    'connected sites: cosign download failed; the "connected sites" profile stays off. Run the installer again later.') f="sites connectés : téléchargement de cosign échoué ; le profil « sites connectés » reste éteint. Relancez l'installation plus tard.";;
    'connected sites: the downloaded cosign does not match its pinned checksum: refused, the "connected sites" profile stays off. Try again later; if it happens again, report it.') f="sites connectés : le cosign téléchargé ne correspond pas à son empreinte épinglée : refusé, le profil « sites connectés » reste éteint. Réessayez plus tard ; si cela se répète, signalez-le.";;
    'connected sites: invalid access response') f="sites connectés : réponse d'accès invalide";;
    'connected sites: credentials renewed meanwhile by another machine of the account; asking again') f="sites connectés : identifiants renouvelés entre-temps par une autre machine du compte ; nouvelle demande";;
    'connected sites: image %s not found or not accessible') f="sites connectés : image %s introuvable ou inaccessible";;
    'connected sites: image signature INVALID: image removed, profile left off') f="sites connectés : signature de l'image INVALIDE : image retirée, profil éteint";;
    'sites-chrome verified (cosign signature, %s)') f="sites-chrome vérifié (signature cosign, %s)";;
    '--sites-only: nothing installed in %s') f="--sites-only : aucune installation dans %s";;
    '--sites-only: this Lokkal version has no connected sites (nokker update)') f="--sites-only : cette version de Lokkal n'a pas les sites connectés (nokker update)";;
    '--sites-only: the core does not answer; start Lokkal first') f="--sites-only : le noyau ne répond pas ; démarrez Lokkal d'abord";;
    'starting sites-chrome failed') f="démarrage de sites-chrome échoué";;
    'connected sites on: Connectors > Connected sites') f="sites connectés allumés : Connecteurs → Sites connectés";;
    '--sites: this Lokkal version has no connected sites') f="--sites : cette version de Lokkal n'a pas les sites connectés";;
    'connected sites NOT turned on (reason above); the rest of Lokkal works. Next: nokker sites enable') f="sites connectés NON allumés (raison ci-dessus) ; le reste de Lokkal fonctionne. Ensuite : nokker sites enable";;
    '--desktop: the Desktop needs an amd64 (x86_64) machine; nothing was touched') f="--desktop : le Bureau demande une machine amd64 (x86_64) ; rien n'a été touché";;
    '--desktop: this Lokkal version has no Desktop') f="--desktop : cette version de Lokkal n'a pas le Bureau";;
    'sites-chrome already here and verified (%s): no access request') f="sites-chrome déjà là et vérifié (%s) : aucune demande d'accès";;
    'connected sites: kept ON with the previous image (%s); the new one will be fetched later: nokker sites enable') f="sites connectés : gardés ALLUMÉS sur l'image d'avant (%s) ; la nouvelle sera tirée plus tard : nokker sites enable";;
    'kept stopped as before the update: %s (start: docker compose -p %s start <service>)') f="gardé(s) à l'arrêt comme avant la mise à jour : %s (démarrer : docker compose -p %s start <service>)";;
    'Desktop on: link this machine to your account (Account tab of the web app, if not done yet), then approve the "Relier le bureau" (link the desktop) card on your phone or on the web; the only folder it can see: %s/desktop/files') f="Bureau allumé : liez cette machine à votre compte (onglet Compte du web, si ce n'est pas fait), puis approuvez la carte « Relier le bureau » sur votre téléphone ou sur le web ; seul dossier qu'il voit : %s/desktop/files";;
    '--desktop and --desktop-session: choose one mode') f="--desktop et --desktop-session : choisissez un seul mode";;
    '--desktop-session: the Desktop session mode is not available yet; for the isolated Desktop, use --desktop. Nothing was changed.') f="--desktop-session : le mode session du Bureau n'est pas encore disponible ; pour le Bureau isolé, utilisez --desktop. Rien n'a été modifié.";;
    '--desktop-session: Linux only; nothing was touched') f="--desktop-session : Linux seulement ; rien n'a été touché";;
    '--desktop-session: X11 only (this session is Wayland); nothing was touched') f="--desktop-session : X11 seulement (cette session est Wayland) ; rien n'a été touché";;
    '--desktop-session: the setup text is missing from this Lokkal version') f="--desktop-session : le texte de mise en place manque à cette version de Lokkal";;
    '--desktop-session: systemd --user is required (graphical session of your account)') f="--desktop-session : systemd --user est requis (session graphique de votre compte)";;
    'Desktop session mode NOT turned on (no yes in the terminal); the rest of Lokkal works') f="mode session du Bureau NON allumé (pas de oui au terminal) ; le reste de Lokkal fonctionne";;
    'Desktop image not pulled (%s)') f="image du Bureau non tirée (%s)";;
    'Desktop session mode: systemctl --user enable failed') f="mode session du Bureau : systemctl --user enable a échoué";;
    'Desktop, session mode on: in Nokker, approve the card that links the desktop, then allow your apps in the settings') f="Bureau, mode session allumé : dans Nokker, approuvez la carte « Relier le bureau », puis permettez vos applis dans les réglages";;
    ' [y/N] ') f=" [o/N] ";;
    *) printf '%s' "$1"; return 1;;
  esac
  printf '%s' "$f"
}
# <<< fr_text
msg()  { _f=$1; shift; [ "$L" = fr ] && _f=$(fr_text "$_f" || true); printf -- "$_f" "$@"; }   # msg FORMAT [VALEURS…] ; « -- » : un format peut commencer par « --option »
say()  { printf '\033[1m›\033[0m %s\n' "$(msg "$@")"; }
warn() { printf '\033[33m!\033[0m %s\n' "$(msg "$@")" >&2; }
die()  { printf '\033[31m✗\033[0m %s\n' "$(msg "$@")" >&2; exit 1; }
tty_ok() { ( : < /dev/tty ) 2>/dev/null; }   # sous-shell : en dash, une redirection ratée sur « : » (spécial) tue le script ; un terminal qui s'OUVRE (en ssh sans -t, /dev/tty existe et est lisible, mais ne s'ouvre pas)
remote_hint() { # remote_hint PORT : installation faite par ssh → le lien en 127.0.0.1 ne s'ouvre que par un tunnel
  # (parcours d'onboarding du 17.11 sur un serveur distant : lien de réglage inutilisable, rien ne le disait)
  [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}" ] || return 0
  _h=$(printf '%s\n' "${SSH_CONNECTION:-}" | awk '{print $3}'); [ -n "$_h" ] || _h=$(hostname 2>/dev/null || uname -n)
  say 'remote machine? From your computer: ssh -L %s:127.0.0.1:%s %s, then open the link' "$1" "$1" "$(id -un)@$_h"
}
ask()  { # ask FORMAT [VALEURS…] → 0 si oui (o/oui/y/yes, dans les deux langues), 1 si non, 2 sans terminal pour demander
  [ "$YES" = 1 ] && return 0
  tty_ok || return 2
  printf '%s%s' "$(msg "$@")" "$(msg ' [y/N] ')" > /dev/tty; read -r a < /dev/tty || return 1
  case "$a" in o|O|oui|Oui|y|Y|yes|Yes) return 0;; *) return 1;; esac
}
usage() {
  if [ "$L" = fr ]; then cat <<'EOF'
Nokker Lokkal — installeur Linux / macOS

  curl -fsSL https://nokker.ai/install.sh | sh
  curl -fsSL https://nokker.ai/install.sh | sh -s -- --workspace ~/code --yes
  sh ~/.nokker/install.sh --update [--version X.Y.Z]

Options :
  --version X.Y.Z     version à installer (défaut : la dernière)
  --workspace DIR     dossier(s) de travail montés dans les bacs à sable, séparés par « : » (défaut : ~/nokker)
  --dir DIR           dossier d'installation (défaut : ~/.nokker)
  --yes               ne pose aucune question (installation de Docker comprise, sous Linux)
  --update            garde .env et l'état, change de version, relance
  --uninstall         arrête et retire les containers ; --purge efface aussi l'état et la base
  --no-open           n'ouvre pas le navigateur
  --vault             coffre OpenBao pour les secrets du noyau, descellé automatiquement
  --vault-manual      idem, mais la part de descellement est affichée une fois et redemandée à chaque redémarrage
  --local-llm         moteur d'inférence local (Ollama, CPU), proposé dans /setup
  --local-llm-model M modèle Ollama à tirer (défaut : qwen3:4b) ; implique --local-llm
  --sites             sites connectés (navigateur des sites, image privée : machine liée, amd64)
  --sites-only        installation existante : retire et revérifie l'image des sites connectés, puis les allume
  --desktop           Bureau isolé : Nokker travaille dans des applis (éditeur de texte, fichiers) d'un bureau virtuel, amd64
  --home              passerelle du foyer (Home Assistant, enceintes, écrans), sur l'adresse LAN de l'hôte seulement
  --home-mdns         en plus : découverte des écrans Cast/AirPlay (Linux seulement) ; implique --home
  --home-lan-ip IP    adresse LAN (privée) de l'hôte vue par les appareils du foyer (défaut : détectée)
  --lang en|fr        langue des messages (défaut : NOKKER_LANG, LC_ALL, LC_MESSAGES, LANG ; anglais sinon)
EOF
  else cat <<'EOF'
Nokker Lokkal — installer for Linux / macOS

  curl -fsSL https://nokker.ai/install.sh | sh
  curl -fsSL https://nokker.ai/install.sh | sh -s -- --workspace ~/code --yes
  sh ~/.nokker/install.sh --update [--version X.Y.Z]

Options:
  --version X.Y.Z     version to install (default: the latest)
  --workspace DIR     work folder(s) mounted in the sandboxes, separated by ":" (default: ~/nokker)
  --dir DIR           install folder (default: ~/.nokker)
  --yes               asks no question (Docker install included, on Linux)
  --update            keeps .env and the state, changes the version, restarts
  --uninstall         stops and removes the containers; --purge also erases the state and the database
  --no-open           does not open the browser
  --vault             OpenBao vault for the secrets of the core, unsealed automatically
  --vault-manual      same, but the unseal share is shown once and asked again at each restart
  --local-llm         local inference engine (Ollama, CPU), offered in /setup
  --local-llm-model M Ollama model to pull (default: qwen3:4b); implies --local-llm
  --sites             connected sites (sites browser, private image: linked machine, amd64)
  --sites-only        existing install: pull and verify the connected sites image again, then turn them on
  --desktop           isolated Desktop: Nokker works in apps (text editor, files) of a virtual desktop, amd64
  --home              home gateway (Home Assistant, speakers, screens), on the LAN address of the host only
  --home-mdns         also: discovery of Cast/AirPlay screens (Linux only); implies --home
  --home-lan-ip IP    LAN (private) address of the host as seen by the home devices (default: detected)
  --lang en|fr        language of the messages (default: NOKKER_LANG, LC_ALL, LC_MESSAGES, LANG; English otherwise)
EOF
  fi
}
case "$LANG_OPT" in '!'*) die '--lang: en or fr expected (got %s)' "${LANG_OPT#!}";; esac

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
    --vault) VAULT=auto; shift;;
    --vault-manual) VAULT=manual; shift;;
    --local-llm) LLM=1; shift;;
    --sites) SITES=1; shift;;
    --sites-only) SITES=1; MODE=sites; shift;;
    --desktop) [ "$DESKTOP_MODE" = session ] && die '--desktop and --desktop-session: choose one mode'; DESKTOP=1; DESKTOP_MODE=virtual; shift;;
    # Mode session GELÉ (décision dev 06.11 : agent-desktop exige l'uid, l'espace de PID et l'espace réseau de la
    # personne ; aucun montage ne l'isole). Le code reste (desktop_session_*), aucune activation possible.
    --desktop-session) die '--desktop-session: the Desktop session mode is not available yet; for the isolated Desktop, use --desktop. Nothing was changed.';;
    --home) HOME_BRIDGE=1; shift;;
    --home-mdns) HOME_BRIDGE=1; HOME_MDNS=1; shift;;
    --home-lan-ip) HOME_BRIDGE=1; HOME_LAN_IP=${2:?}; shift 2;;
    --local-llm-model) LLM=1; LLM_MODEL=${2:?}; shift 2;;
    -h|--help) usage; exit 0;;
    *) die 'unknown option: %s' "$1";;
  esac
done

[ "$(id -u)" = 0 ] && warn 'running as root: Lokkal installs for the current user; prefer your own account.'
OS=$(uname -s); ARCH=$(uname -m)
case "$OS" in Linux) os=linux;; Darwin) os=darwin;; *) die 'unsupported system: %s (Windows: install.ps1)' "$OS";; esac
case "$ARCH" in x86_64|amd64) arch=x64;; aarch64|arm64) arch=arm64;; *) die 'unsupported architecture: %s' "$ARCH";; esac
# Sites connectés : refus arm64 AVANT toute demande d'identifiants (Google Chrome n'existe qu'en amd64 sous Linux)
[ "$SITES" = 1 ] && [ "$arch" != x64 ] && die '--sites: connected sites need an amd64 (x86_64) machine; nothing was touched'
# Bureau : refus arm64 avant tout (agent-desktop n'est épinglé et testé qu'en x86_64)
[ "$DESKTOP" = 1 ] && [ "$arch" != x64 ] && die '--desktop: the Desktop needs an amd64 (x86_64) machine; nothing was touched'
# Mode session : Linux et X11 seulement (README §6) ; refus avant tout
if [ "$DESKTOP_MODE" = session ]; then
  [ "$os" = linux ] || die '--desktop-session: Linux only; nothing was touched'
  case "${XDG_SESSION_TYPE:-}" in wayland) die '--desktop-session: X11 only (this session is Wayland); nothing was touched';; esac
fi

need() { command -v "$1" >/dev/null 2>&1; }
need curl || die 'curl is required'
sha256() { if need sha256sum; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
rand() { # rand <octets> : base64 url sans remplissage
  head -c "$1" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'
}
fernet() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }  # 32 octets, base64 url AVEC « = »

# Projet compose PROPRE à l'installation : les noms de projet sont globaux au démon docker, et un projet partagé fait
# reprendre à une 2e installation (autre compte, autre --dir) les containers et le volume postgres de la 1re (et son
# --purge les effacerait). Nouvelle installation : nokker-<compte>, plus une empreinte du dossier hors ~/.nokker ;
# installation existante sans COMPOSE_PROJECT_NAME dans .env : « nokker » (ses volumes nokker_* sont gardés). Le nom
# est écrit dans .env (COMPOSE_PROJECT_NAME : `docker compose -f $DIR/docker-compose.yml …` le retrouve seul) et dans
# lokkal.json (`project`, relu par la commande nokker). Le réseau des bacs à sable suit : <projet>-sandbox.
envv() { grep "^$1=" "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-; }
if [ -f "$DIR/.env" ]; then
  PROJECT=$(envv COMPOSE_PROJECT_NAME); PROJECT=${PROJECT:-nokker}
  SANDBOX_NET=$(envv NOKKER_SANDBOX_NETWORK); SANDBOX_NET=${SANDBOX_NET:-nokker-sandbox}
else
  u=$(id -un | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_\n-' '-')
  PROJECT="nokker-${u:-user}"
  [ "$DIR" = "$HOME/.nokker" ] || PROJECT="$PROJECT-$(printf %s "$DIR" | cksum | cut -d' ' -f1)"
  SANDBOX_NET="$PROJECT-sandbox"
fi
compose() { docker compose -p "$PROJECT" --env-file "$DIR/.env" -f "$DIR/docker-compose.yml" "$@"; }
# Services optionnels arrêtés EXPRÈS (Bureau, sites, maison…) : un --update les garde arrêtés (recréés sur la nouvelle
# image, jamais démarrés), au lieu d'un `compose up -d` qui relançait tout (répétition du 07.11 : Bureau arrêté relancé,
# nouvelle carte « Relier le bureau »). Les services de base et les tâches d'init démarrent toujours.
OPTIONAL_SERVICES="desktop sites-chrome home-bridge local-llm payagent paychrome testshop"
KEEP_STOPPED=""
stopped_optional() { # services optionnels dont le conteneur existe et ne tourne pas
  compose ps -a --format '{{.Service}} {{.State}}' 2>/dev/null | while read -r svc st; do
    case " $OPTIONAL_SERVICES " in *" $svc "*) case "$st" in exited|created|dead) printf '%s ' "$svc";; esac;; esac
  done
}
up_kept() { # up_kept [options compose up…] : démarre tout sauf KEEP_STOPPED, qui sont recréés sans démarrer
  if [ -z "$KEEP_STOPPED" ]; then compose up -d "$@"; return; fi
  _svcs=""; for _s in $(compose config --services); do case " $KEEP_STOPPED " in *" $_s "*) ;; *) _svcs="$_svcs $_s";; esac; done
  # shellcheck disable=SC2086
  compose up -d "$@" $_svcs && compose create $KEEP_STOPPED
}

setv() { # setv CLÉ valeur : remplace ou ajoute dans .env
  if grep -q "^$1=" "$DIR/.env" 2>/dev/null; then
    sed "s|^$1=.*|$1=$2|" "$DIR/.env" > "$DIR/.env.tmp" && mv "$DIR/.env.tmp" "$DIR/.env"
  else printf '%s=%s\n' "$1" "$2" >> "$DIR/.env"; fi
}
getv() { grep "^$1=" "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-; }
add_profile() { # add_profile NOM : ajoute un profil à COMPOSE_PROFILES sans retirer les autres (vault, local-llm)
  cur=$(getv COMPOSE_PROFILES)
  case ",$cur," in *",$1,"*) ;; *) setv COMPOSE_PROFILES "${cur:+$cur,}$1";; esac
}

# ------------------------------------------------------------------------------------------------- sites connectés
# Image PRIVÉE (docs/CONNECTED-SITES.md §3.1.1) : identifiants de lecture du registre demandés au noyau LOCAL (qui les
# obtient de nokker.ai par le lien de la machine), utilisés dans une config docker dédiée ($DIR/docker, 0700), puis
# effacés (logout) : rien ne reste sur disque. Image tirée PAR DIGEST (figé dans le paquet), signature cosign vérifiée
# avec la clé épinglée ($DIR/keys/sites-chrome.pub) par le cosign épinglé (sites_cosign) ; cosign non conforme ou
# signature fausse : refus, profil éteint.
SITES_ZERO=sha256:0000000000000000000000000000000000000000000000000000000000000000
# Bureau, mode session (README §6) : unité systemd --user et script hôte posés seulement après le texte de mise en place
# (session-setup.json, FIGÉ, affiché tel quel) et un « oui » tapé au terminal ; --yes ne vaut pas accord.
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; UNIT="$UNIT_DIR/nokker-desktop.service"
desktop_session_off() { # arrête et retire l'unité et le script (rien si absents)
  [ -f "$UNIT" ] || [ -f "$DIR/bin/nokker-desktop-session-run" ] || return 0
  systemctl --user disable --now nokker-desktop.service >/dev/null 2>&1 || true
  rm -f "$UNIT" "$DIR/bin/nokker-desktop-session-run"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  docker rm -f "nokker-desktop-session-$(id -u)" >/dev/null 2>&1 || true
}
desktop_session_text() { # desktop_session_text FICHIER : la ligne de la langue, telle quelle (une ligne par langue)
  sed -n "s/^ *\"$L\": *\"\(.*\)\",\{0,1\}\$/\1/p" "$1" | head -1
}
desktop_session_consent() { # 0 : « oui » tapé au terminal après le texte ; 1 sinon (jamais --yes)
  txt=$(desktop_session_text "$1"); [ -n "$txt" ] || die '--desktop-session: the setup text is missing from this Lokkal version'
  tty_ok || return 1
  printf '\n%s ' "$txt" > /dev/tty; read -r a < /dev/tty || return 1
  case "$a" in o|O|oui|Oui|OUI|y|Y|yes|Yes|YES) return 0;; *) return 1;; esac
}
remove_profile() { cur=$(getv COMPOSE_PROFILES); setv COMPOSE_PROFILES "$(printf %s "$cur" | tr ',' '\n' | grep -vx "$1" | paste -sd, -)"; }
sites_field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$2" | head -1; }
sites_creds() { # sites_creds FICHIER : identifiants du registre (JSON de la route locale) ; 0 ok, 1 sinon (message dit)
  tok=$(cat "$DIR/state/local-token" 2>/dev/null) || { warn 'connected sites: local token not found'; return 1; }
  code=$(curl -s -m 30 -o "$1" -w '%{http_code}' -X POST -H "Authorization: Bearer $tok" \
    "http://127.0.0.1:$(getv NOKKER_API_PORT)/v1/local/sites/registry" 2>/dev/null) || code=000
  case "$code" in 429|5??|000) SITES_TEMP=1;; esac   # refus passager : l'état d'avant est gardé (--update)
  case "$code" in
    200) return 0;;
    409) warn 'connected sites: this machine is not linked to a nokker.ai account; link it (Account tab of the web app), then: nokker sites enable';;
    403) warn 'connected sites: nokker.ai refused access to the image (account or machine); nothing was pulled';;
    429) w=$(sed -n 's/.*"retry_after_s": *\([0-9]*\).*/\1/p' "$1" | head -1)
         warn 'connected sites: too many access requests (6 per 24 h per account); try again in about %s h' "$(( (${w:-3600} + 3599) / 3600 ))";;
    400) if grep -q unsupported_arch "$1" 2>/dev/null; then warn 'connected sites: unsupported architecture (amd64 only)'
         else warn 'connected sites: access request refused (HTTP 400)'; fi;;
    503) if grep -q link_unavailable "$1" 2>/dev/null; then warn 'connected sites: link with nokker.ai cut or nokker.ai unreachable; try again later: nokker sites enable'
         else warn 'connected sites: private registry unavailable; try again later: nokker sites enable'; fi;;
    *) warn 'connected sites: image access unavailable (HTTP %s); try again later: nokker sites enable' "$code";;
  esac
  return 1
}
# cosign ÉPINGLÉ pour les sites (GO dev 06.11) : une machine vierge n'a pas cosign. Version fixe, sha256 de l'artefact
# comparé à la valeur ci-dessous (jamais à un checksums.txt téléchargé), posé dans ~/.nokker/libexec (0755), JAMAIS dans
# le PATH de la personne (~/.nokker/bin l'est), utilisé seulement ici. Empreinte fausse : refus, aucun repli.
# NOKKER_COSIGN_URL : tests seulement (file://…) ; l'empreinte reste exigée.
COSIGN_SHA=8b24b946dd5809c6bd93de08033bcf6bc0ed7d336b7785787c080f574b89249b   # cosign v2.4.1 linux-amd64
COSIGN_BIN="$DIR/libexec/cosign"
sites_cosign() { # 0 : cosign épinglé présent et conforme ; 1 sinon (raison affichée)
  [ -x "$COSIGN_BIN" ] && [ "$(sha256 "$COSIGN_BIN")" = "$COSIGN_SHA" ] && return 0
  mkdir -p "$DIR/libexec"; chmod 755 "$DIR/libexec"; t="$DIR/libexec/.cosign.$$"
  if ! curl -fsSL "${NOKKER_COSIGN_URL:-https://github.com/sigstore/cosign/releases/download/v2.4.1/cosign-linux-amd64}" -o "$t" 2>/dev/null; then
    rm -f "$t"; warn 'connected sites: cosign download failed; the "connected sites" profile stays off. Run the installer again later.'; return 1
  fi
  if [ "$(sha256 "$t")" != "$COSIGN_SHA" ]; then
    rm -f "$t"; warn 'connected sites: the downloaded cosign does not match its pinned checksum: refused, the "connected sites" profile stays off. Try again later; if it happens again, report it.'; return 1
  fi
  chmod 755 "$t"; mv -f "$t" "$COSIGN_BIN"
}
SITES_PREV_ON=0; SITES_PREV_DG=""; SITES_TEMP=0
sites_local() { # sites_local DIGEST : l'image du registre privé à ce digest est dans docker
  _img=$(getv NOKKER_SITES_CHROME_IMAGE); [ -n "$_img" ] && docker image inspect "$_img@$1" >/dev/null 2>&1; }
sites_keep_prev() { # refus passager pendant --update : sites rallumés sur l'image d'avant (déjà vérifiée), ou rien
  [ "$MODE" = update ] && [ "$SITES_TEMP" = 1 ] && [ "$SITES_PREV_ON" = 1 ] && [ -n "$SITES_PREV_DG" ] \
    && sites_local "$SITES_PREV_DG" || return 1
  setv NOKKER_SITES_CHROME_DIGEST "$SITES_PREV_DG"; add_profile sites; setv NOKKER_SITES 1
  warn 'connected sites: kept ON with the previous image (%s); the new one will be fetched later: nokker sites enable' "$SITES_PREV_DG"
}
sites_enable() { # tire, vérifie et allume ; 0 ok, 1 profil laissé éteint (raison affichée)
  keyf="$DIR/keys/sites-chrome.pub"; dg=$(getv NOKKER_SITES_CHROME_DIGEST)
  [ -r "$keyf" ] && [ -n "$dg" ] && [ "$dg" != "$SITES_ZERO" ] || { warn 'connected sites: this Lokkal version has no sites-chrome image'; return 1; }
  # Mise à jour, sites allumés avant, MÊME digest épinglé, image déjà là : rien à demander au registre (quota de 6 par
  # 24 h et par compte). L'image a passé cosign quand le profil a été allumé, et son contenu est fixé par le digest.
  if [ "${SITES_PREV_ON:-0}" = 1 ] && [ "${SITES_PREV_DG:-}" = "$dg" ] && sites_local "$dg"; then
    add_profile sites; setv NOKKER_SITES 1
    say 'sites-chrome already here and verified (%s): no access request' "$dg"; return 0
  fi
  sites_cosign || return 1
  st=$(mktemp -d); chmod 700 "$st"; dc="$DIR/docker"; mkdir -p "$dc"; chmod 700 "$dc"
  # Un robot par COMPTE : chaque demande régénère son secret pour toutes les machines du compte. Si une autre machine
  # du foyer en demande un entre notre demande et notre pull, le pull prend un 401 : UNE nouvelle demande, puis un
  # nouveau pull, jamais plus (quota de 6 par 24 h).
  ok=0; try=1
  while [ "$try" -le 2 ]; do
    sites_creds "$st/c.json" || break
    reg=$(sites_field registry "$st/c.json"); user=$(sites_field username "$st/c.json")
    case "$reg" in ""|*[!A-Za-z0-9.:-]*) warn 'connected sites: invalid access response'; break;; esac
    ref="$reg/lokkal-private/sites-chrome@$dg"
    if sites_field password "$st/c.json" | docker --config "$dc" login -u "$user" --password-stdin "$reg" >/dev/null 2>"$st/err" \
       && docker --config "$dc" pull -q "$ref" >/dev/null 2>>"$st/err"; then ok=1; break; fi
    docker --config "$dc" logout "$reg" >/dev/null 2>&1 || true
    if [ "$try" = 1 ] && grep -Eqi 'unauthorized|denied|401' "$st/err"; then
      say 'connected sites: credentials renewed meanwhile by another machine of the account; asking again'
      try=2; continue
    fi
    warn 'connected sites: image %s not found or not accessible' "$ref"; break
  done
  if [ "$ok" = 1 ] && ! DOCKER_CONFIG="$dc" "$COSIGN_BIN" verify --key "$keyf" --insecure-ignore-tlog=true "$ref" >/dev/null 2>&1; then
    warn 'connected sites: image signature INVALID: image removed, profile left off'; docker rmi "$ref" >/dev/null 2>&1 || true; ok=0
  fi
  [ -n "${reg:-}" ] && { docker --config "$dc" logout "$reg" >/dev/null 2>&1 || true; }
  rm -f "$dc/config.json"; rm -rf "$st"
  [ "$ok" = 1 ] || return 1
  setv NOKKER_SITES_CHROME_IMAGE "$reg/lokkal-private/sites-chrome"; add_profile sites; setv NOKKER_SITES 1
  say 'sites-chrome verified (cosign signature, %s)' "$dg"
}
# Refus si le projet appartient à une autre installation (containers d'un autre dossier), ou, pour une installation
# neuve, si son volume postgres existe déjà (données d'une autre installation, ou d'une ancienne effacée sans --purge).
guard_project() {
  real=$(cd "$DIR" 2>/dev/null && pwd -P || echo "$DIR")
  other=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
            --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | sort -u | grep -vxF -e "$DIR" -e "$real" || true)
  [ -z "$other" ] || die 'the docker project %s belongs to another install (%s): nothing was touched. Install with another --dir, or stop the other one from its account.' "$PROJECT" "$other"
  if [ "${1:-}" = new ] && docker volume inspect "${PROJECT}_postgres" >/dev/null 2>&1; then
    die 'the docker volume %s_postgres already exists (another install, or an old install removed without --purge): nothing was touched. If it is yours and can go: docker volume rm %s_postgres' "$PROJECT" "$PROJECT"
  fi
}

# ------------------------------------------------------------------------------------------------- désinstallation
# --purge : la commande nokker retient le dernier fil de chaque dossier dans local-threads.json (clé « <serveur>|<cwd> »,
# apps/cli lastthread.ts) ; sans ménage, une installation neuve sur le même port REPRENAIT un fil de la pile effacée
# (vu par onboarding le 17.11 : le noyau accepte un thread_id inconnu). On retire les entrées de CETTE installation :
# serveur http(s) sur la boucle locale (127.0.0.1, localhost, [::1], 0.0.0.0 : la CLI accepte --server et
# NOKKER_SERVER_URL) avec le port API de l'installation ; le reste intact, écriture atomique 0600. Sans python3, ou
# fichier illisible : fichier supprimé (simple repli de la CLI, qui retombe sur la liste du noyau).
purge_threads() { # purge_threads PORT_API
  _tf="$CONF_DIR/local-threads.json"; [ -f "$_tf" ] || return 0
  if [ -n "$1" ] && command -v python3 >/dev/null 2>&1 && _n=$(python3 -c '
import json, os, sys, tempfile
from urllib.parse import urlsplit
p, port = sys.argv[1], int(sys.argv[2])
d = json.load(open(p, encoding="utf-8"))
if not (isinstance(d, dict) and d.get("version") == 1 and isinstance(d.get("entries"), dict)):
    sys.exit(3)
def ours(k):
    u = urlsplit(k.split("|", 1)[0])
    try:
        return u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0") and u.port == port
    except ValueError:
        return False
kept = {k: v for k, v in d["entries"].items() if not ours(k)}
n = len(d["entries"]) - len(kept)
if n:
    d["entries"] = kept
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(p)), prefix=".local-threads.")
    with os.fdopen(fd, "w", encoding="utf-8") as w:
        w.write(json.dumps(d, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
print(n)
' "$_tf" "$1" 2>/dev/null); then
    [ "$_n" = 0 ] || say 'nokker command: %s thread(s) of this install forgotten (%s)' "$_n" "$_tf"
    return 0
  fi
  rm -f "$_tf"; say 'nokker command: remembered threads forgotten (%s removed)' "$_tf"
}
# --purge : lokkal.json (~/.config/nokker) est COMMUN aux installations d'un compte (la commande nokker y lit son
# serveur et son jeton) ; il n'est retiré que s'il désigne CETTE installation (install_dir, ou à défaut le compose,
# chemins comparés après résolution des liens). Sinon il est gardé avec un message : purger une installation latérale
# retirait celui de la principale (contourné à la main par onboarding le 17.11).
lokkal_json_purge() {
  _lj="$CONF_DIR/lokkal.json"; [ -f "$_lj" ] || return 0
  _id=$(sed -n 's/.*"install_dir": *"\([^"]*\)".*/\1/p' "$_lj" | head -1)
  [ -n "$_id" ] || _id=$(sed -n 's/.*"compose": *"\([^"]*\)\/docker-compose\.yml".*/\1/p' "$_lj" | head -1)
  _a=$(cd "$DIR" 2>/dev/null && pwd -P) || _a=$DIR
  _b=$(cd "$_id" 2>/dev/null && pwd -P) || _b=$_id
  if [ -n "$_id" ] && [ "$_a" = "$_b" ]; then rm -f "$_lj"; return 0; fi
  say 'lokkal.json kept: it points to another install (%s)' "${_id:-?}"
}
if [ "$MODE" = uninstall ]; then
  [ -f "$DIR/docker-compose.yml" ] || die 'nothing installed in %s' "$DIR"
  # confirmation de --purge AVANT tout geste (sans terminal ni --yes : arrêt net avec la marche à suivre, rien touché)
  if [ "$PURGE" = 1 ]; then
    if ask 'ALSO erase the Nokker database, memory and state (cannot be undone)?'; then :; else
      [ $? = 2 ] && die 'no terminal to confirm the erase: nothing was touched. Re-run with --yes to erase without a question.'
      die 'aborted'
    fi
  fi
  guard_project
  desktop_session_off   # avant `compose down` : son conteneur est sur desktopnet
  # bacs à sable AVANT `compose down` : encore attachés au réseau <projet>-sandbox, ils empêchaient compose de le
  # retirer (« has active endpoints ») ; le réseau restait après --purge (vu par onboarding le 18.11)
  if [ "$PURGE" = 1 ]; then
    for c in $(docker ps -aq --filter label=nokker.sandbox --filter "network=$SANDBOX_NET"); do docker rm -f "$c" >/dev/null 2>&1 || true; done
    compose down -v --remove-orphans
    purge_threads "$(getv NOKKER_API_PORT)"
    lokkal_json_purge
    rm -rf "$DIR"
    say 'Nokker Lokkal uninstalled, data erased. Your work folders were not touched.'
  else
    for c in $(docker ps -aq --filter label=nokker.sandbox --filter "network=$SANDBOX_NET"); do docker rm -f "$c" >/dev/null 2>&1 || true; done
    compose down --remove-orphans
    say 'Nokker Lokkal stopped; data kept in %s (--uninstall --purge to erase everything).' "$DIR"
  fi
  exit 0
fi

# ------------------------------------------------------------------------------------------------- Docker
docker_ok() { need docker && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }
if ! docker_ok; then
  if need docker && ! docker info >/dev/null 2>&1; then
    if [ "$os" = linux ] && docker info 2>&1 | grep -qi "permission denied"; then
      die 'Docker is here but your account cannot use it: sudo usermod -aG docker %s, then log in again.' "$USER"
    fi
    die 'Docker is installed but does not answer: start Docker (Docker Desktop / OrbStack / systemctl start docker).'
  fi
  if [ "$os" = darwin ]; then
    die 'Docker is required: install Docker Desktop (https://www.docker.com/products/docker-desktop/) or OrbStack (https://orbstack.dev), start it, then run this command again.'
  fi
  ask 'Docker is not installed. Install it now (official get.docker.com script, asks for sudo)?' \
    || die 'Docker is required: https://docs.docker.com/engine/install/'
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh; rm -f /tmp/get-docker.sh
  sudo usermod -aG docker "$(id -un)" 2>/dev/null || true
  if ! docker info >/dev/null 2>&1; then
    # Le groupe docker ne vaut qu'aux sessions suivantes : on se relance sous ce groupe (sg) au lieu de demander de
    # se reconnecter. Lancé par `curl … | sh`, l'installeur n'a pas de fichier : il est retéléchargé.
    self="$0"; tmpself=""
    if [ ! -f "$self" ]; then
      self=$(mktemp); tmpself=$self; u="https://nokker.ai/install.sh"
      [ -n "$VERSION" ] && u="https://github.com/$REPO/releases/download/lokkal-v$VERSION/install.sh"
      [ -n "${NOKKER_RELEASE_BASE:-}" ] && u="$NOKKER_RELEASE_BASE/install.sh"   # tests : le même installeur
      case "$u" in file://*) cp "${u#file://}" "$self" || self="";; *) curl -fsSL "$u" -o "$self" || self="";; esac
    fi
    if [ -n "$self" ] && need sg && sg docker -c "docker info" >/dev/null 2>&1; then
      say 'Docker installed: continuing the install under the docker group'
      [ -n "$tmpself" ] && exec sg docker -c "NOKKER_LANG=$L sh '$self' $ARGS; rc=\$?; rm -f '$self'; exit \$rc"
      exec sg docker -c "NOKKER_LANG=$L sh '$self' $ARGS"
    fi
    die 'Docker installed: log in again (docker group), then run this command again.'
  fi
fi
# Socket vu par le démon (monté dans dockerproxy) : rootless → celui du contexte courant
SOCK=/var/run/docker.sock
ep=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)
case "$ep" in unix://*) p=${ep#unix://}; case "$p" in /run/user/*|*/.docker/run/*) [ "$os" = linux ] && SOCK=$p;; esac;; esac

# Sites seuls (`nokker sites enable` / `nokker sites update`) : installation existante, aucun paquet téléchargé
if [ "$MODE" = sites ]; then
  [ -f "$DIR/.env" ] && [ -f "$DIR/docker-compose.yml" ] || die '--sites-only: nothing installed in %s' "$DIR"
  grep -q '^  sites-chrome:' "$DIR/docker-compose.yml" || die '--sites-only: this Lokkal version has no connected sites (nokker update)'
  guard_project; umask 077
  [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$(getv NOKKER_API_PORT)/healthz")" = 200 ] \
    || die '--sites-only: the core does not answer; start Lokkal first'
  sites_enable || exit 3
  # core (NOKKER_SITES change) et sites-chrome seulement : un service arrêté exprès (desktop…) reste arrêté
  compose up -d core sites-chrome >/dev/null 2>"$DIR/state/sites-up.log" || { cat "$DIR/state/sites-up.log" >&2; die 'starting sites-chrome failed'; }
  rm -f "$DIR/state/sites-up.log"
  say 'connected sites on: Connectors > Connected sites'
  exit 0
fi

# ------------------------------------------------------------------------------------------------- version et paquet
BASE=${NOKKER_RELEASE_BASE:-}
if [ -z "$VERSION" ]; then
  if [ -n "$BASE" ] && [ -f "${BASE#file://}/LATEST" ]; then VERSION=$(cat "${BASE#file://}/LATEST")
  elif [ -n "$BASE" ] && [ "${BASE#file://}" = "$BASE" ]; then
    VERSION=$(curl -fsSL "$BASE/LATEST" | tr -d '[:space:]') || die 'cannot read LATEST on %s' "$BASE"
  else
    # La plus HAUTE version (semver, préversions comprises : 0.1.0 > 0.1.0-rc.10 > 0.1.0-rc.9), pas la première de
    # l'API (ordre de création). Clé de tri portable (sans sort -V) : X Y Z puis n° de rc (sans rc = au-dessus).
    VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=100" \
      | grep -o '"tag_name": *"lokkal-v[^"]*"' | sed 's/.*lokkal-v//; s/"//' \
      | awk -F'[.-]' '{rc = ($4 == "rc") ? $5 : 999999; printf "%d %d %d %d %d %s\n", $1, $2, $3, rc, $6, $0}' \
      | sort -n -k1,1 -k2,2 -k3,3 -k4,4 -k5,5 | tail -1 | cut -d' ' -f6) || true
    [ -n "$VERSION" ] || die 'cannot find the latest version (--version X.Y.Z)'
  fi
fi
[ -n "$VERSION" ] || die 'version not found'
BASE=${BASE:-https://github.com/$REPO/releases/download/lokkal-v$VERSION}
fetch() { case "$BASE" in file://*) cp "${BASE#file://}/$1" "$2";; *) curl -fsSL "$BASE/$1" -o "$2";; esac; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PKG=nokker-lokkal-$VERSION.tar.gz
say 'Nokker Lokkal %s (%s-%s)' "$VERSION" "$os" "$arch"
fetch "$PKG" "$TMP/$PKG" || die 'download failed: %s/%s' "$BASE" "$PKG"
fetch "$PKG.sha256" "$TMP/$PKG.sha256" || die 'checksum not found: %s.sha256' "$PKG"
[ "$(sha256 "$TMP/$PKG")" = "$(cut -d' ' -f1 "$TMP/$PKG.sha256")" ] || die 'wrong sha256 checksum of the package: aborted'
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
  0) say 'package signature verified (cosign)';;
  1) die 'cosign signature of the package invalid or missing: aborted';;
esac

# ------------------------------------------------------------------------------------------------- pose des fichiers
if [ -f "$DIR/.env" ]; then guard_project; else guard_project new; fi
umask 077
mkdir -p "$DIR" "$DIR/state" "$DIR/bin" "$CONF_DIR"
# Liste des hôtes ssh ouverts par egress (écrite par le noyau depuis hosts.yaml validé, lue par egress en lecture
# seule, par le groupe de l'utilisateur) : à créer ICI, sinon docker la crée en root et le noyau ne peut plus l'écrire.
mkdir -p "$DIR/state/egress" && chmod 750 "$DIR/state/egress"
tar -xzf "$TMP/$PKG" -C "$TMP"
# Mise à jour : la pose des fichiers suit l'installeur de la version CIBLE, pas celui déjà installé (qui peut ignorer
# un fichier nouveau ou écraser config/). Relancé une seule fois, avec les mêmes choix ; il retélécharge et revérifie.
if [ "$MODE" = update ] && [ -z "${NOKKER_INSTALL_REEXEC:-}" ] && ! cmp -s "$0" "$TMP/nokker/install.sh"; then
  cp "$TMP/nokker/install.sh" "$TMP.install.sh"; rm -rf "$TMP"; trap - EXIT
  say 'installer of version %s' "$VERSION"
  set -- --update --version "$VERSION" --dir "$DIR" --no-open
  [ "$YES" = 1 ] && set -- "$@" --yes
  [ "$VAULT" = auto ] && set -- "$@" --vault
  [ "$VAULT" = manual ] && set -- "$@" --vault-manual
  [ -n "$LLM_MODEL" ] && set -- "$@" --local-llm-model "$LLM_MODEL"
  [ "$LLM" = 1 ] && [ -z "$LLM_MODEL" ] && set -- "$@" --local-llm
  [ -n "$HOME_LAN_IP" ] && set -- "$@" --home-lan-ip "$HOME_LAN_IP"
  [ "$HOME_MDNS" = 1 ] && set -- "$@" --home-mdns
  [ "$HOME_BRIDGE" = 1 ] && set -- "$@" --home
  [ "$SITES" = 1 ] && set -- "$@" --sites
  [ "$DESKTOP_MODE" = virtual ] && set -- "$@" --desktop
  [ "$DESKTOP_MODE" = session ] && set -- "$@" --desktop-session
  NOKKER_LANG=$L NOKKER_INSTALL_REEXEC="$TMP.install.sh" exec sh "$TMP.install.sh" "$@"
fi
[ -n "${NOKKER_INSTALL_REEXEC:-}" ] && rm -f "$NOKKER_INSTALL_REEXEC"
# Fichiers du paquet remplacés ; .env et state/ jamais touchés. config/ est à l'utilisateur : voir install_config.
for f in docker-compose.yml initdb sql web VERSION install.sh backup.sh; do rm -rf "${DIR:?}/$f"; cp -R "$TMP/nokker/$f" "$DIR/$f"; done
# Kiosque HDMI : lanceur et unit livrés dans ~/.nokker/kiosk/, jamais installés ni démarrés ici (docs/KIOSK.md §4)
if [ -d "$TMP/nokker/kiosk" ]; then rm -rf "${DIR:?}/kiosk"; cp -R "$TMP/nokker/kiosk" "$DIR/kiosk"; chmod 755 "$DIR/kiosk/nokker-kiosk.sh"; fi
# config/ : un fichier que l'utilisateur n'a pas touché (identique à la version livrée précédente, gardée dans
# .dist/config) suit la nouvelle version ; un fichier modifié reste tel quel, la nouvelle version est posée à côté
# en <fichier>.new. Sans .dist (installation ≤ 0.1.0-rc.8.1), config.yaml est reconnu non modifié par son empreinte
# dans config-history.sha256 (ligne network_name, réécrite par l'ancien installeur, ramenée au défaut).
install_config() {
  mkdir -p "$DIR/config" "$DIR/.dist/config"
  for src in "$TMP/nokker/config"/*; do
    [ -f "$src" ] || continue
    n=${src##*/}; cur="$DIR/config/$n"; base="$DIR/.dist/config/$n"
    if [ ! -f "$cur" ] || cmp -s "$cur" "$src"; then cp "$src" "$cur"
    elif [ -f "$base" ] && cmp -s "$cur" "$base"; then cp "$src" "$cur"; say 'config/%s updated' "$n"
    elif [ ! -f "$base" ] && [ "$n" = config.yaml ] && [ -f "$TMP/nokker/config-history.sha256" ] \
      && grep -qx "$(sed 's/^    network_name: .*/    network_name: nokker-sandbox/' "$cur" | sha256 -)" \
           "$TMP/nokker/config-history.sha256"; then cp "$src" "$cur"; say 'config/%s updated' "$n"
    else cp "$src" "$cur.new"; warn 'config/%s changed locally: kept; new version delivered as config/%s.new' "$n" "$n"
    fi
    cp "$src" "$base"
  done
}
install_config
# local.code.require_sandbox (bac exigé, défaut du code : false) : un config.yaml modifié localement est gardé tel quel
# par install_config, donc une installation mise à jour n'aurait jamais la clé. Ajoutée SI ABSENTE, une seule ligne sous
# local.code après « backend: », copie datée avant ; une valeur déjà posée (même false) n'est jamais touchée ; sans bloc
# local.code (ou sans ligne backend:), arrêt avec un message plutôt qu'une écriture à l'aveugle.
# Avec la clé, le noyau REFUSE DE DÉMARRER (nokker/local/api.py, _check_sandbox_isolation) si le backend n'est pas un
# bac ou si une racine de travail recouvre l'état : la clé n'est donc posée que si backend vaut docker ET qu'aucune
# racine de NOKKER_HOST_WORKSPACE ne contient le dossier d'installation (ni n'est dedans) ; sinon avertissement, rien
# d'écrit, pas d'arrêt. Et si le noyau refuse quand même au redémarrage (SandboxRequired), la copie est remise (plus bas).
sandbox_flag() { # $1 = config.yaml, $2 = clé de local.code (défaut require_sandbox) → valeur sans commentaire, vide si absente
  awk -v k="${2:-require_sandbox}" '/^[^ #]/ {l = ($0 ~ /^local:/); c = 0} l && /^  [^ #]/ {c = ($0 ~ /^  code:/)}
       l && c && $0 ~ "^    " k ":" {sub("^    " k ":[ ]*", ""); sub(/[ ]*#.*/, ""); gsub(/["\047]/, ""); print; exit}' "$1"
}
real_dir() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
roots_overlap() { # $1 = dossier d'installation, $2 = racines « : » → première racine qui le contient ou qu'il contient
  d=$(real_dir "$1"); _ifs=$IFS; IFS=:
  for r in $2; do
    [ -n "$r" ] || continue; r=$(real_dir "$r"); r=${r%/}
    [ -n "$r" ] || { IFS=$_ifs; printf /; return 0; }   # racine « / » : recouvre tout
    case "$d/" in "$r"/*) IFS=$_ifs; printf '%s' "$r"; return 0;; esac
    case "$r/" in "$d"/*) IFS=$_ifs; printf '%s' "$r"; return 0;; esac
  done
  IFS=$_ifs; return 1
}
RS_BAK=""
ensure_require_sandbox() {
  f="$DIR/config/config.yaml"; [ -f "$f" ] || return 0
  [ -n "$(sandbox_flag "$f")" ] && return 0
  be=$(sandbox_flag "$f" backend); [ -n "$be" ] || be=docker
  if [ "$be" != docker ]; then
    warn 'config/config.yaml: backend %s: sandbox not required; switch local.code.backend to docker to require it' "$be"; return 0
  fi
  if ov=$(roots_overlap "$DIR" "$(getv NOKKER_HOST_WORKSPACE)"); then
    warn 'config/config.yaml: work folder %s overlaps the install folder %s: sandbox not required; separate the folders to require it' "$ov" "$DIR"; return 0
  fi
  line='    require_sandbox: true                      # bac exigé (ajouté par install.sh --update)'
  awk -v line="$line" 'BEGIN {done = 0} {print}
       /^[^ #]/ {l = ($0 ~ /^local:/); c = 0} l && /^  [^ #]/ {c = ($0 ~ /^  code:/)}
       !done && l && c && /^    backend:/ {print line; done = 1}
       END {exit !done}' "$f" > "$f.require-sandbox" \
    || { rm -f "$f.require-sandbox"; die 'config/config.yaml: no local.code block with a backend: line; add local.code.require_sandbox: true by hand, then run again'; }
  # Garde-fou : exactement une ligne de plus, et le fichier sans cette ligne est l'ancien à l'octet près.
  if [ "$(wc -l < "$f.require-sandbox")" -ne $(( $(wc -l < "$f") + 1 )) ] \
     || ! grep -vxF "$line" "$f.require-sandbox" | cmp -s - "$f"; then
    rm -f "$f.require-sandbox"; die 'config/config.yaml: adding local.code.require_sandbox would change more than one line: nothing written'
  fi
  bak="$f.before-require-sandbox-$(date -u +%Y%m%dT%H%M%SZ)"; cp -p "$f" "$bak"
  cat "$f.require-sandbox" > "$f"; rm -f "$f.require-sandbox"; RS_BAK=$bak
  say 'config/config.yaml: local.code.require_sandbox: true added (copy: %s)' "${bak##*/}"
}
# Filet : la clé ajoutée ci-dessus et refusée quand même par le noyau (SandboxRequired, cas que le contrôle shell ne voit
# pas) → copie d'avant remise. Appelé à DEUX endroits : à l'échec de `compose up` (le noyau sort, il n'est jamais
# « healthy » et les services qui en dépendent font échouer le up AVANT l'attente de healthz : vu en vrai le 17.11) et
# dans l'attente de healthz. 0 = copie remise (à relancer), 1 = rien à faire.
rs_restore_if_refused() {
  # texte exact de la dernière ligne de la trace du noyau (nokker/local/api.py, relevé sur l'image rc.9.33 le 17.11) :
  #   nokker.local.api.SandboxRequired: local.code.require_sandbox : backend « local » refusé …
  [ -n "$RS_BAK" ] && compose logs --tail 200 core 2>/dev/null | grep -q 'nokker\.local\.api\.SandboxRequired: local\.code\.require_sandbox' \
    || return 1
  cat "$RS_BAK" > "$DIR/config/config.yaml"; RS_BAK=""
  warn 'the core refused local.code.require_sandbox (SandboxRequired): previous config/config.yaml restored, core restarted'
}
# Bacs de l'ANCIENNE étiquette (rc.9.34, host-2d) : avant, nokker.instance valait sha256(<chemin d'état DANS le
# conteneur>)[:12], soit sha256("/opt/data") pour TOUS les Lokkal — elle n'identifie aucune installation. Le noyau ne
# les voit plus (le proxy docker ne sert que l'étiquette de SON instance) : sans ménage ici, ils restent Up pour
# toujours. Ceux de CETTE installation = ancienne étiquette ET réseau des bacs de cette installation (<projet>-sandbox,
# propre à chaque installation ; même patron que --uninstall). Jamais : installation neuve, NOKKER_INSTANCE absente ou
# égale à l'ancienne valeur, local.instance posé (l'utilisateur garde l'ancienne identité), ancien réseau commun
# « nokker-sandbox » d'un projet qui n'est pas « nokker » (signalés seulement). Liste affichée avant la suppression.
legacy_reap() {
  [ "$MODE" = update ] || return 0
  _inst=$(getv NOKKER_INSTANCE); [ -n "$_inst" ] || return 0
  _leg=$(printf %s /opt/data | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)
  printf %s "$_leg" | grep -Eqx '[0-9a-f]{12}' || return 0
  [ "$_inst" = "$_leg" ] && return 0
  awk '/^[^ #]/ {l = ($0 ~ /^local:/)} l && /^  instance:[ ]*[^ #]/ {f = 1} END {exit !f}' "$DIR/config/config.yaml" 2>/dev/null && return 0
  _ids=""; _names=""
  for _c in $(docker ps -aq --filter label=nokker.sandbox --filter "label=nokker.instance=$_leg" --filter "network=$SANDBOX_NET" 2>/dev/null); do
    # re-contrôle exact (jamais un préfixe) : valeur de l'étiquette et réseau attaché
    [ "$(docker inspect -f '{{index .Config.Labels "nokker.instance"}}' "$_c" 2>/dev/null)" = "$_leg" ] || continue
    case " $(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$_c" 2>/dev/null) " in
      *" $SANDBOX_NET "*) ;; *) continue;; esac
    _ids="$_ids $_c"; _names="$_names $(docker inspect -f '{{.Name}}' "$_c" 2>/dev/null | tr -d /)"
  done
  [ -n "$_ids" ] || return 0
  if [ "$SANDBOX_NET" = nokker-sandbox ] && [ "$PROJECT" != nokker ]; then
    warn 'sandboxes with the legacy label %s on the shared network %s, not removed (another install may own them): %s' "$_leg" "$SANDBOX_NET" "${_names# }"
    return 0
  fi
  say 'sandboxes left by the previous version (legacy label %s, network %s), removed: %s' "$_leg" "$SANDBOX_NET" "${_names# }"
  # shellcheck disable=SC2086
  docker rm -f $_ids >/dev/null 2>&1 || warn 'could not remove the old sandboxes: docker rm -f %s' "${_ids# }"
}
start_stack() { # compose up ; si le noyau a refusé la clé posée par CETTE exécution : copie remise, up relancé une fois
  up_kept --remove-orphans >/dev/null 2>"$TMP/up.log" && return 0
  rs_restore_if_refused && up_kept --remove-orphans >/dev/null 2>"$TMP/up.log" && return 0
  cat "$TMP/up.log" >&2; die 'start failed (log: docker compose -p %s logs)' "$PROJECT"
}
ensure_require_sandbox
# tiers.small.model : le modèle 35B (qwen/qwen3.6-35b-a3b-eu) est retiré le 21.11.2026 ; le gabarit passe au 27B. Un
# config.yaml gardé par install_config garderait le 35B. Réécrit SEULEMENT si la valeur est exactement l'ancienne valeur
# du gabarit (une clé personnelle ou un choix de la personne n'est jamais touché) ; une seule ligne change, copie datée
# avant ; une valeur absente ou différente : rien. Jamais d'arrêt : au pire, un avertissement et rien d'écrit.
SMALL_OLD=qwen/qwen3.6-35b-a3b-eu; SMALL_NEW=qwen/qwen3.8-27b
small_model() { # $1 = config.yaml → tiers.small.model sans commentaire ni guillemets, vide si absent
  awk '/^[^ #]/ {t = ($0 ~ /^tiers:/); s = 0} t && /^  [^ #]/ {s = ($0 ~ /^  small:/)}
       t && s && /^    model:/ {sub(/^    model:[ ]*/, ""); sub(/[ ]*#.*/, ""); gsub(/["\047]/, ""); print; exit}' "$1"
}
ensure_small_27b() {
  f="$DIR/config/config.yaml"; [ -f "$f" ] || return 0
  [ "$(small_model "$f")" = "$SMALL_OLD" ] || return 0
  awk -v new="$SMALL_NEW" 'BEGIN {done = 0} /^[^ #]/ {t = ($0 ~ /^tiers:/); s = 0} t && /^  [^ #]/ {s = ($0 ~ /^  small:/)}
       !done && t && s && /^    model:/ {print "    model: " new "                    # 35B retiré (install.sh --update)"; done = 1; next}
       {print} END {exit !done}' "$f" > "$f.small-27b" || { rm -f "$f.small-27b"; return 0; }
  if [ "$(wc -l < "$f.small-27b")" -ne "$(wc -l < "$f")" ] \
     || [ "$(diff "$f" "$f.small-27b" | grep -c '^[<>]')" -ne 2 ] || [ "$(small_model "$f.small-27b")" != "$SMALL_NEW" ]; then
    rm -f "$f.small-27b"; warn 'config/config.yaml: rewriting tiers.small.model would change more than one line: nothing written'; return 0
  fi
  bak="$f.before-small-27b-$(date -u +%Y%m%dT%H%M%SZ)"; cp -p "$f" "$bak"
  cat "$f.small-27b" > "$f"; rm -f "$f.small-27b"
  say 'config/config.yaml: tiers.small.model %s -> %s (copy: %s)' "$SMALL_OLD" "$SMALL_NEW" "${bak##*/}"
}
ensure_small_27b
# local.code.idle_ttl_s (bac sans appel depuis ce délai supprimé, session gardée, bac recréé à l'appel suivant ; défaut du
# code : 0, jamais) : même motif, même raison (un config.yaml gardé n'a jamais la clé). Ajoutée SI ABSENTE, une seule ligne
# sous local.code après « backend: », copie datée avant ; une valeur déjà posée (même 0) n'est jamais touchée ; pas de bloc
# local.code : rien (ensure_require_sandbox s'est déjà arrêtée là-dessus). Valeur valide pour tout backend, rien à remettre.
ensure_idle_ttl() {
  f="$DIR/config/config.yaml"; [ -f "$f" ] || return 0
  [ -n "$(sandbox_flag "$f" idle_ttl_s)" ] && return 0
  line='    idle_ttl_s: 1800                           # bac à sable sans appel depuis 30 min supprimé (ajouté par install.sh --update)'
  awk -v line="$line" 'BEGIN {done = 0} {print}
       /^[^ #]/ {l = ($0 ~ /^local:/); c = 0} l && /^  [^ #]/ {c = ($0 ~ /^  code:/)}
       !done && l && c && /^    backend:/ {print line; done = 1}
       END {exit !done}' "$f" > "$f.idle-ttl" || { rm -f "$f.idle-ttl"; return 0; }
  if [ "$(wc -l < "$f.idle-ttl")" -ne $(( $(wc -l < "$f") + 1 )) ] \
     || ! grep -vxF "$line" "$f.idle-ttl" | cmp -s - "$f"; then
    rm -f "$f.idle-ttl"; die 'config/config.yaml: adding local.code.idle_ttl_s would change more than one line: nothing written: add local.code.idle_ttl_s: 1800 by hand in config/config.yaml'
  fi
  bak="$f.before-idle-ttl-$(date -u +%Y%m%dT%H%M%SZ)"; cp -p "$f" "$bak"
  cat "$f.idle-ttl" > "$f"; rm -f "$f.idle-ttl"
  say 'config/config.yaml: local.code.idle_ttl_s: 1800 added (backup: %s)' "${bak##*/}"
}
ensure_idle_ttl
# local.approval_wait_s (carte posée : le tour attend sa décision ce nombre de secondes et exécute l'outil lui-même, la personne
# approuve dans la CLI PENDANT le tour ; défaut du code : 0, le tour finit « en attente ») : même motif (un config.yaml gardé
# n'a jamais la clé). Ajoutée SI ABSENTE, une seule ligne juste sous « local: », copie datée avant ; une valeur déjà posée (même
# 0) n'est jamais touchée ; sans « local: » en bloc (absent, ou en notation compacte) : rien, pas d'arrêt.
ensure_approval_wait() {
  f="$DIR/config/config.yaml"; [ -f "$f" ] || return 0
  awk '/^[^ #]/ {l = ($0 ~ /^local:/)} l && /^  approval_wait_s:/ {f = 1} END {exit !f}' "$f" && return 0
  line='  approval_wait_s: 10                          # carte posée : le tour attend sa décision 10 s (ajouté par install.sh --update)'
  awk -v line="$line" 'BEGIN {done = 0} {print}
       !done && /^local:[ ]*(#.*)?$/ {print line; done = 1}
       END {exit !done}' "$f" > "$f.approval-wait" || { rm -f "$f.approval-wait"; return 0; }
  if [ "$(wc -l < "$f.approval-wait")" -ne $(( $(wc -l < "$f") + 1 )) ] \
     || ! grep -vxF "$line" "$f.approval-wait" | cmp -s - "$f"; then
    rm -f "$f.approval-wait"; die 'config/config.yaml: adding local.approval_wait_s would change more than one line: nothing written: add local.approval_wait_s: 10 by hand in config/config.yaml'
  fi
  bak="$f.before-approval-wait-$(date -u +%Y%m%dT%H%M%SZ)"; cp -p "$f" "$bak"
  cat "$f.approval-wait" > "$f"; rm -f "$f.approval-wait"
  say 'config/config.yaml: local.approval_wait_s: 10 added (backup: %s)' "${bak##*/}"
}
ensure_approval_wait
# reply_language et reply_language_tail (nokker/context/reply_language.py) : sur Lokkal, la consigne « réponds dans la langue
# de la personne » doit être rendue (contrôle d'onboarding du 18.11 : message anglais, préfixe et outils en français →
# réponse en français). Le défaut du NOYAU reste faux (SOKKAN mesure mieux sans) ; Lokkal les pose à vrai : dans le
# gabarit (installation neuve) et ici, à chaque --update, SEULEMENT si la clé est absente (une valeur déjà posée, même
# false, n'est jamais touchée). Clés de premier niveau ajoutées en fin de fichier, copie datée avant.
ensure_reply_lang() {
  f="$DIR/config/config.yaml"; [ -f "$f" ] || return 0
  _add=""
  for _k in reply_language reply_language_tail; do grep -q "^$_k:" "$f" || _add="$_add $_k"; done
  [ -n "$_add" ] || return 0
  _n=0
  { cat "$f"; [ -z "$(tail -c 1 "$f")" ] || echo
    for _k in $_add; do printf '%s: true   # langue des réponses de Lokkal (ajouté par install.sh --update ; défaut du noyau : false)\n' "$_k"; done
  } > "$f.reply-lang"
  for _k in $_add; do _n=$((_n + 1)); done
  if [ "$(grep -vcE '^(reply_language|reply_language_tail): true   # langue des réponses de Lokkal' "$f.reply-lang")" -ne "$(grep -c '' "$f")" ] \
     || [ "$(grep -cE '^(reply_language|reply_language_tail): true   # langue des réponses de Lokkal' "$f.reply-lang")" -ne \
          "$(( _n + $(grep -cE '^(reply_language|reply_language_tail): true   # langue des réponses de Lokkal' "$f") ))" ]; then
    rm -f "$f.reply-lang"
    die 'config/config.yaml: adding the reply language keys would change more than those lines: nothing written: add reply_language: true and reply_language_tail: true by hand'
  fi
  bak="$f.before-reply-lang-$(date -u +%Y%m%dT%H%M%SZ)"; cp -p "$f" "$bak"
  cat "$f.reply-lang" > "$f"; rm -f "$f.reply-lang"
  say 'config/config.yaml: %s: true added (reply language of Lokkal; backup: %s)' "$(echo $_add | sed 's/ /, /')" "${bak##*/}"
}
ensure_reply_lang
chmod -R go-w "$DIR"; chmod 755 "$DIR/initdb" "$DIR/sql" "$DIR/web" "$DIR/config"
chmod 644 "$DIR"/initdb/* "$DIR"/sql/* "$DIR"/web/* "$DIR"/config/*; chmod 700 "$DIR/install.sh" "$DIR/backup.sh"
# Coffre (profil vault) : scripts remplacés, part et AppRole (vault/unseal, vault/approle) jamais touchés. Les deux
# dossiers existent toujours : core monte vault/approle (vide sans le profil), docker le créerait sinon en root.
mkdir -p "$DIR/vault/unseal" "$DIR/vault/approle"; chmod 700 "$DIR/vault" "$DIR/vault/unseal" "$DIR/vault/approle"
if [ -d "$TMP/nokker/vault" ]; then
  for f in init.sh unseal.sh; do cp "$TMP/nokker/vault/$f" "$DIR/vault/$f"; chmod 600 "$DIR/vault/$f"; done
fi

port_free() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/" 2>/dev/null; [ $? -eq 7 ]; }   # 7 = connexion refusée
# Réservation (17.11, deux installations lancées à 16 s d'écart ont pris les mêmes ports : un port n'est « pris » qu'une
# fois publié par compose, bien après le choix) : mkdir atomique d'un dossier par port dans un répertoire commun à tous
# les comptes (/tmp, sticky). Une réservation de plus d'une heure (installation morte) est reprise ; répertoire
# inutilisable → pas de réservation (comportement d'avant), jamais de boucle sans fin. Rendue quand le noyau répond.
PORT_RES=${NOKKER_PORT_RESERVATIONS:-/tmp/nokker-lokkal-ports}
reserve_port() {
  [ -d "$PORT_RES" ] || { mkdir -p "$PORT_RES" 2>/dev/null && chmod 1777 "$PORT_RES" 2>/dev/null; }
  [ -d "$PORT_RES" ] && [ -w "$PORT_RES" ] || return 0
  [ -n "$(find "$PORT_RES/$1" -maxdepth 0 -mmin +60 2>/dev/null)" ] && rmdir "$PORT_RES/$1" 2>/dev/null
  mkdir "$PORT_RES/$1" 2>/dev/null
}
pick_port() { p=$1; while ! { port_free "$p" && reserve_port "$p"; }; do p=$((p + 1)); done; echo "$p"; }
RESERVED=""


if [ ! -f "$DIR/.env" ]; then
  [ "$MODE" = update ] && die '--update: no install in %s' "$DIR"
  WORKSPACE=${WORKSPACE:-$HOME/nokker}
  ws=""; IFS_=$IFS; IFS=:
  for w in $WORKSPACE; do
    case "$w" in "~"*) w="$HOME${w#\~}";; esac
    mkdir -p "$w"; w=$(cd "$w" && pwd -P)
    [ "$w" = / ] || [ "$w" = "$HOME" ] && die 'work folder too wide: %s (choose a subfolder)' "$w"
    ws="${ws:+$ws:}$w"
  done; IFS=$IFS_
  WEB_PORT=$(pick_port 7420); API_PORT=$(pick_port 8742); RESERVED="$WEB_PORT $API_PORT"
  : > "$DIR/.env"; chmod 600 "$DIR/.env"
  cat >> "$DIR/.env" <<EOF
$(msg '# Nokker Lokkal: generated by install.sh on %s. Local secrets: do not share.' "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
NOKKER_VERSION=$VERSION
NOKKER_UID=$(id -u)
NOKKER_GID=$(id -g)
NOKKER_STATE_DIR=$DIR/state
NOKKER_HOST_WORKSPACE=$ws
NOKKER_PORT=$WEB_PORT
NOKKER_API_PORT=$API_PORT
DOCKER_SOCKET=$SOCK
COMPOSE_PROJECT_NAME=$PROJECT
NOKKER_SANDBOX_NETWORK=$SANDBOX_NET
NOKKER_PG_ADMIN_PASSWORD=$(rand 24)
NOKKER_OWNER_PASSWORD=$(rand 24)
NOKKER_APP_PASSWORD=$(rand 24)
NOKKER_APPROVALS_KEY=$(fernet)
NOKKER_LOCAL_SECRET_KEY=$(fernet)
NOKKER_CONNECTOR_KEY=$(fernet)
EOF
else
  setv NOKKER_VERSION "$VERSION"
  [ -n "$WORKSPACE" ] && warn '--workspace ignored: existing install (edit NOKKER_HOST_WORKSPACE in %s/.env)' "$DIR"
fi
# Clé des identifiants de connecteurs (Fernet, rc.9.4) : sans elle l'étape Connecteurs de /setup échoue (403). Posée à
# l'installation ; ajoutée à la mise à jour d'une installation qui ne l'a pas, jamais remplacée (secrets déjà chiffrés).
[ -n "$(getv NOKKER_CONNECTOR_KEY)" ] || setv NOKKER_CONNECTOR_KEY "$(fernet)"
# Langue de repli du noyau (turn-lang-5a) : posée à l'installation et ajoutée à la mise à jour d'une installation qui ne
# l'a pas ; jamais remplacée (un choix de l'utilisateur, ou une valeur éditée à la main, est gardé).
[ -n "$(getv NOKKER_LANG)" ] || setv NOKKER_LANG "$KLANG"
# ACCORD sur le LAN (docs/ACCORD-DEVICE.md §1) : NOKKER_ACCORD_LAN (IP:port EXPLICITE, posé à la main dans .env) allume le
# profil `accord` (relais accord-relay publié sur cette adresse seulement) et l'écoute interne du noyau ; vide, ou une
# adresse joker (0.0.0.0, [::], sans IP, port 0) : profil retiré, rien de publié. Rejoué à chaque --update.
accord_lan() {
  _al=$(getv NOKKER_ACCORD_LAN)
  [ -n "$_al" ] || { remove_profile accord; return 0; }
  case "$_al" in
    0.0.0.0:*|'[::]':*|:*|*:0) _ok=0;;
    *) printf %s "$_al" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}:[1-9][0-9]{0,4}$|^\[[0-9a-fA-F:]+\]:[1-9][0-9]{0,4}$' && _ok=1 || _ok=0;;
  esac
  if [ "$_ok" != 1 ]; then
    warn 'NOKKER_ACCORD_LAN=%s refused (give the explicit LAN address and port of this machine): ACCORD relay off' "$_al"
    remove_profile accord; return 0
  fi
  add_profile accord
  [ -n "$(getv NOKKER_ACCORD_BIND)" ] || setv NOKKER_ACCORD_BIND 0.0.0.0:8443   # écoute DANS le conteneur du noyau
  say 'ACCORD relay published on %s only (profile accord)' "$_al"
}
accord_lan
if [ -n "$VAULT" ]; then
  [ -f "$DIR/vault/init.sh" ] || die '--vault: this Lokkal version has no OpenBao vault'
  [ "$(getv NOKKER_SECRETS_PROVIDER)" = openbao ] || [ "$MODE" != update ] \
    || warn 'vault turned on for an existing install: migrate the secrets of the file vault (nokker vault migrate --from file --to openbao)'
  add_profile vault; setv NOKKER_SECRETS_PROVIDER openbao; setv NOKKER_VAULT_UNSEAL "$VAULT"
fi
if [ "$LLM" = 1 ]; then
  grep -q '^  ollama:' "$DIR/docker-compose.yml" || die '--local-llm: this Lokkal version has no local-llm profile'
  if [ -n "$LLM_MODEL" ]; then
    case "$LLM_MODEL" in *[!A-Za-z0-9._:/-]*) die '--local-llm-model: invalid model name: %s' "$LLM_MODEL";; esac
    setv NOKKER_LOCAL_LLM_MODEL "$LLM_MODEL"
  fi
  add_profile local-llm
fi
# Sites connectés (profil `sites`, éteint) : jeton du noyau vers sites-chrome, posé une fois (le compose l'exige
# seulement quand le profil est allumé).
[ -n "$(getv NOKKER_SITES_BROWSER_TOKEN)" ] || setv NOKKER_SITES_BROWSER_TOKEN "$(rand 32)"
# Identité du noyau (étiquette nokker.instance de ses bacs) : le proxy docker ne laisse agir que sur les bacs de CETTE
# instance (NOKKER_INSTANCE). Même valeur que state/instance-id, que le noyau relit : une installation existante garde
# l'id déjà tiré par le noyau ; sinon 16 hexa aléatoires, écrits dans le fichier (0600) AVANT le premier démarrage.
if [ -z "$(getv NOKKER_INSTANCE)" ]; then
  inst=$(cat "$DIR/state/instance-id" 2>/dev/null | tr -d '[:space:]' || true)
  printf %s "$inst" | grep -Eqx '[0-9a-f]{16}' || inst=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
  printf '%s\n' "$inst" > "$DIR/state/instance-id"; chmod 600 "$DIR/state/instance-id"
  setv NOKKER_INSTANCE "$inst"
fi
# Image privée : clé publique épinglée et digest de CETTE version, posés depuis le paquet à chaque installation ou mise
# à jour (§3.1.1). Le profil n'est allumé qu'APRÈS le tirage vérifié (sites_enable, une fois le noyau démarré) :
# une mise à jour l'éteint d'abord, puis renouvelle les identifiants, retire et revérifie la nouvelle image.
mkdir -p "$DIR/keys"; chmod 755 "$DIR/keys"
if [ -f "$TMP/nokker/keys/sites-chrome.pub" ]; then cp "$TMP/nokker/keys/sites-chrome.pub" "$DIR/keys/"; chmod 644 "$DIR/keys/sites-chrome.pub"; fi
case ",$(getv COMPOSE_PROFILES)," in *,sites,*) [ "$(getv NOKKER_SITES)" = 1 ] && SITES_PREV_ON=1;; esac
SITES_PREV_DG=$(getv NOKKER_SITES_CHROME_DIGEST)
dg=$(cat "$TMP/nokker/sites-chrome.digest" 2>/dev/null || true)
case "$dg" in sha256:*) printf %s "${dg#sha256:}" | grep -Eqx '[0-9a-f]{64}' || dg="";; *) dg="";; esac
setv NOKKER_SITES_CHROME_DIGEST "${dg:-$SITES_ZERO}"
case ",$(getv COMPOSE_PROFILES)," in *,sites,*) SITES=1;; esac
[ "$SITES" = 1 ] && [ "$arch" != x64 ] && die '--sites: connected sites need an amd64 (x86_64) machine; nothing was touched'
if [ "$SITES" = 1 ]; then
  grep -q '^  sites-chrome:' "$DIR/docker-compose.yml" || die '--sites: this Lokkal version has no connected sites'
  remove_profile sites; setv NOKKER_SITES 0
fi
# Bureau (opt-in, profil `desktop`) : gardé à la mise à jour ; ~/.nokker/desktop/files est le seul dossier qu'il voit,
# state garde la clé du nœud (inscription approuvée une fois par la personne : carte « Relier le bureau »).
# Mode gardé à la mise à jour : virtual (profil desktop) ou session (NOKKER_DESKTOP_MODE=session, unité posée).
if [ -z "$DESKTOP_MODE" ] && [ "$(getv NOKKER_DESKTOP)" = 1 ]; then
  DESKTOP=1; DESKTOP_MODE=$(getv NOKKER_DESKTOP_MODE); [ "$DESKTOP_MODE" = session ] || DESKTOP_MODE=virtual
fi
if [ "$DESKTOP_MODE" = session ]; then   # gelé : une installation antérieure en session est éteinte, jamais rallumée
  desktop_session_off; setv NOKKER_DESKTOP 0; setv NOKKER_DESKTOP_MODE virtual; setv NOKKER_DESKTOP_SESSION_TEXT ""
  DESKTOP=0; DESKTOP_MODE=""
  warn '--desktop-session: the Desktop session mode is not available yet; for the isolated Desktop, use --desktop. Nothing was changed.'
fi
[ "$DESKTOP" = 1 ] && [ "$arch" != x64 ] && die '--desktop: the Desktop needs an amd64 (x86_64) machine; nothing was touched'
if [ "$DESKTOP" = 1 ]; then
  grep -q '^  desktop:' "$DIR/docker-compose.yml" || die '--desktop: this Lokkal version has no Desktop'
  mkdir -p "$DIR/desktop/state" "$DIR/desktop/files"; chmod 700 "$DIR/desktop" "$DIR/desktop/state"
  if [ "$DESKTOP_MODE" = session ]; then
    # Accord demandé à la première mise en route et chaque fois que le texte change (empreinte gardée dans .env).
    ST="$TMP/nokker/desktop-session/session-setup.json"
    [ -f "$ST" ] || die '--desktop-session: the setup text is missing from this Lokkal version'
    need systemctl && systemctl --user show-environment >/dev/null 2>&1 \
      || die '--desktop-session: systemd --user is required (graphical session of your account)'
    fp=$(sha256 "$ST")
    if [ "$(getv NOKKER_DESKTOP_SESSION_TEXT)" = "$fp" ] || desktop_session_consent "$ST"; then
      setv NOKKER_DESKTOP_SESSION_TEXT "$fp"
      remove_profile desktop; setv NOKKER_DESKTOP 1; setv NOKKER_DESKTOP_MODE session
    else
      desktop_session_off; remove_profile desktop; setv NOKKER_DESKTOP 0; setv NOKKER_DESKTOP_MODE virtual
      setv NOKKER_DESKTOP_SESSION_TEXT ""; DESKTOP=0; DESKTOP_MODE=""
      warn 'Desktop session mode NOT turned on (no yes in the terminal); the rest of Lokkal works'
    fi
  else
    desktop_session_off   # un seul mode par machine
    add_profile desktop; setv NOKKER_DESKTOP 1; setv NOKKER_DESKTOP_MODE virtual; setv NOKKER_DESKTOP_SESSION_TEXT ""
  fi
fi
# Passerelle du foyer (opt-in) : core reste fermé au LAN ; seule home-bridge le joint, sur une liste blanche vide.
is_private_v4() { case "$1" in 10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0;; *) return 1;; esac; }
if [ "$HOME_BRIDGE" = 1 ]; then
  if [ -z "$HOME_LAN_IP" ]; then
    if [ "$os" = darwin ]; then HOME_LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
    else HOME_LAN_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -1); fi
  fi
  is_private_v4 "$HOME_LAN_IP" \
    || die '--home: private LAN address of the host not found (%s); set --home-lan-ip 192.168.x.y' "${HOME_LAN_IP:-$(msg 'none')}"
  [ "$HOME_MDNS" = 1 ] && [ "$os" != linux ] && die '--home-mdns: Linux only (multicast on the host network)'
  setv NOKKER_HOME_LAN_IP "$HOME_LAN_IP"
  [ -n "$(getv NOKKER_HOME_BRIDGE_TOKEN)" ] || setv NOKKER_HOME_BRIDGE_TOKEN "$(rand 32)"
  setv NOKKER_HOME_BRIDGE_URL http://home-bridge:8800
  mkdir -p "$DIR/home/secrets"; chmod 700 "$DIR/home" "$DIR/home/secrets"
  [ -f "$DIR/home/home.yaml" ] || { msg '# Lokkal home gateway (docs/LOKKAL-HOME-BRIDGE.md). NO destination is allowed while `allow` is empty:'; echo
                                      msg '# add the devices of your network (private addresses only), then: docker compose ... restart home-bridge'; echo
                                      cat <<'HOMEYAML'
allow: []
#  - {host: 192.168.1.10, ports: [8123]}                     # Home Assistant
#  - {cidr: 192.168.1.0/24, ports: [1400, 1705, 4953, 7000]} # Sonos, Snapcast, AirPlay
# home_assistant: {url: http://192.168.1.10:8123, token_file: secrets/ha_token}
cast: {enabled: false}
HOMEYAML
  } > "$DIR/home/home.yaml"
  chmod 600 "$DIR/home/home.yaml"
  add_profile home
  [ "$HOME_MDNS" = 1 ] && add_profile home-mdns
  say 'home gateway: listening on %s:8801/8802; destinations in %s/home/home.yaml (empty)' "$HOME_LAN_IP" "$DIR"
fi
chmod 600 "$DIR/.env"; chmod 700 "$DIR/state"
WEB_PORT=$(getv NOKKER_PORT); API_PORT=$(getv NOKKER_API_PORT)

cat > "$CONF_DIR/lokkal.json" <<EOF
{"compose": "$DIR/docker-compose.yml", "project": "$PROJECT", "version": "$VERSION", "core_service": "core",
 "api_url": "http://127.0.0.1:$API_PORT", "web_url": "http://127.0.0.1:$WEB_PORT",
 "token_path": "$DIR/state/local-token", "env_file": "$DIR/.env", "install_dir": "$DIR"}
EOF
chmod 600 "$CONF_DIR/lokkal.json"

# ------------------------------------------------------------------------------------------------- démarrage
if [ "${NOKKER_SKIP_PULL:-0}" != 1 ]; then
  say 'downloading the images (first time: a few minutes)'
  compose pull -q
  # Image des bacs à sable : pas un service du compose, donc absente de `compose pull`. Sans elle, le premier tour
  # qui ouvre un bac à sable la télécharge (98 s mesurées sur une machine de test le 05.11) : tirée ici, une fois.
  SB_IMAGE=$(getv NOKKER_SANDBOX_IMAGE); SB_IMAGE=${SB_IMAGE:-ghcr.io/nokker-ai/sandbox:$(getv NOKKER_VERSION)}
  docker pull -q "$SB_IMAGE" >/dev/null 2>&1 || warn 'sandbox image not pulled (%s): the first turn will pull it' "$SB_IMAGE"
fi
if [ "$(getv NOKKER_SECRETS_PROVIDER)" = openbao ]; then
  # Coffre d'abord, initialisé HORS du noyau (vault-init), pour que core démarre avec son AppRole.
  say 'OpenBao vault'
  compose up -d openbao vault-unseal >/dev/null 2>"$TMP/up.log" || { cat "$TMP/up.log" >&2; die 'the vault does not start'; }
  rc=0; compose --profile vault-init run --rm vault-init || rc=$?
  case $rc in
    0) ;;
    3) warn 'vault initialised without an AppRole in %s/vault/approle: the core will not have access (LOKKAL section 7, generate-root)' "$DIR";;
    *) die 'vault initialisation failed (docker compose -p %s logs openbao)' "$PROJECT";;
  esac
  [ "$(getv NOKKER_VAULT_UNSEAL)" = manual ] && ! compose exec -T openbao bao status >/dev/null 2>&1 \
    && warn 'vault sealed (manual mode): docker compose -p %s exec openbao bao operator unseal' "$PROJECT"
fi
say 'starting'
[ "$MODE" = update ] && KEEP_STOPPED=$(stopped_optional)
legacy_reap
start_stack
[ -n "$KEEP_STOPPED" ] && say 'kept stopped as before the update: %s (start: docker compose -p %s start <service>)' "$(echo $KEEP_STOPPED)" "$PROJECT"
case ",$(getv COMPOSE_PROFILES)," in *,local-llm,*)
  m=$(getv NOKKER_LOCAL_LLM_MODEL)
  say 'local model %s downloading in the background (docker compose -p %s logs -f local-llm-pull)' "${m:-qwen3:4b}" "$PROJECT";;
esac
i=0
until [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$API_PORT/healthz")" = 200 ]; do
  i=$((i + 1)); [ $i -gt 120 ] && die 'the core does not answer after 6 min: docker compose -p %s logs core' "$PROJECT"
  # Clé require_sandbox ajoutée par cette mise à jour et refusée par le noyau : copie d'avant remise, noyau relancé.
  if [ $((i % 5)) = 0 ] && rs_restore_if_refused; then compose restart core >/dev/null 2>&1 || true; i=0; fi
  sleep 3
done
for p in $RESERVED; do rmdir "$PORT_RES/$p" 2>/dev/null || true; done   # ports publiés : réservation rendue
if [ "$SITES" = 1 ]; then
  if sites_enable && up_kept >/dev/null 2>"$TMP/up.log"; then say 'connected sites on: Connectors > Connected sites'
  elif sites_keep_prev && up_kept >/dev/null 2>"$TMP/up.log"; then :
  else warn 'connected sites NOT turned on (reason above); the rest of Lokkal works. Next: nokker sites enable'; fi
fi
[ "$DESKTOP_MODE" = virtual ] && case " $KEEP_STOPPED " in *" desktop "*) false;; *) true;; esac && say 'Desktop on: link this machine to your account (Account tab of the web app, if not done yet), then approve the "Relier le bureau" (link the desktop) card on your phone or on the web; the only folder it can see: %s/desktop/files' "$DIR"
if [ "$DESKTOP_MODE" = session ]; then
  DI="$(getv NOKKER_DESKTOP_IMAGE)"; DI="${DI:-ghcr.io/nokker-ai/nokker-desktop}:$(getv NOKKER_VERSION)"
  # l'image n'est pas un service allumé du compose (pas de profil en session) : tirée ici
  [ "${NOKKER_SKIP_PULL:-0}" = 1 ] || docker pull -q "$DI" >/dev/null || die 'Desktop image not pulled (%s)' "$DI"
  mkdir -p "$DIR/bin" "$UNIT_DIR"
  install -m 755 "$TMP/nokker/desktop-session/session-run.sh" "$DIR/bin/nokker-desktop-session-run"
  sed -e "s|^Environment=NOKKER_DESKTOP_IMAGE=.*|Environment=NOKKER_DESKTOP_IMAGE=$DI NOKKER_DESKTOP_NETWORK=${PROJECT}_desktopnet NOKKER_DESKTOP_STATE_DIR=$DIR/desktop/state|" \
      -e "s|^ExecStart=.*|ExecStart=$DIR/bin/nokker-desktop-session-run|" \
      "$TMP/nokker/desktop-session/nokker-desktop.service" > "$UNIT"
  chmod 644 "$UNIT"
  systemctl --user daemon-reload && systemctl --user enable nokker-desktop.service >/dev/null 2>&1 \
    || die 'Desktop session mode: systemctl --user enable failed'
  systemctl --user restart nokker-desktop.service >/dev/null 2>&1 || true   # sans écran X maintenant : au prochain login graphique
  say 'Desktop, session mode on: in Nokker, approve the card that links the desktop, then allow your apps in the settings'
fi

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
    warn 'nokker command not installed for %s-%s: missing from this version, or wrong checksum or signature (the web works)' "$os" "$arch"
  fi
fi

URL="http://127.0.0.1:$WEB_PORT/"
# Premier lancement : la racine du web sert la connexion du cloud ; le setup local s'ouvre avec un CODE d'accès dans le
# fragment (/setup#code=…, usage unique, 60 s ; la page l'efface puis l'échange contre un jeton de session,
# nokker/local/access_sessions.py). Le jeton local n'est plus jamais dans une URL ni affiché : un `#token=` finissait tôt
# ou tard dans un journal ou une erreur (fuite du 18.11.2026). Code impossible à obtenir (noyau d'avant) : la commande
# `nokker open /setup` le fera plus tard.
SETUP_TODO=0
grep -Eq '"done": *true' "$DIR/state/setup.json" 2>/dev/null || SETUP_TODO=1
TOKF="$DIR/state/local-token"
if [ "$SETUP_TODO" = 1 ]; then
  i=0; while [ ! -s "$TOKF" ] && [ $i -lt 20 ]; do i=$((i + 1)); sleep 1; done
  # `done` n'est posé que par l'assistant : une machine déjà reliée au Router (lien du compte nokker.ai) ou avec un
  # modèle par défaut a un modèle utilisable. L'état réel vient du noyau (répétition du 07.11 : faux « premier réglage »).
  if [ -s "$TOKF" ]; then
    sv=$(curl -s -m 10 -H "Authorization: Bearer $(cat "$TOKF")" \
      "http://127.0.0.1:$(getv NOKKER_API_PORT)/v1/local/setup" 2>/dev/null) || sv=""
    printf %s "$sv" | grep -Eq '"done": *true|"linked": *true|"default": *"' && SETUP_TODO=0
  fi
fi
say 'Nokker Lokkal %s is ready: %s' "$VERSION" "$URL"
say 'work folder(s): %s' "$(getv NOKKER_HOST_WORKSPACE)"
[ -x "$DIR/bin/nokker" ] && say 'terminal: nokker   (new shell, or export PATH="%s/bin:$PATH")' "$DIR"
mint_code() { # un code d'accès (usage unique, 60 s), émis par le noyau avec le jeton local ; vide si impossible
  [ -r "$TOKF" ] || return 0
  curl -s -m 10 -X POST -H "Authorization: Bearer $(cat "$TOKF")" \
    "http://127.0.0.1:$(getv NOKKER_API_PORT)/v1/local/session-code" 2>/dev/null \
    | sed -n 's/.*"code": *"\([A-Za-z0-9_-]\{16,64\}\)".*/\1/p'
}
OPEN_URL=$URL
if [ "$SETUP_TODO" = 1 ]; then
  if [ ! -r "$TOKF" ]; then
    warn 'first setup (model): local token not found (%s); run the installer again once the core has started' "$TOKF"
  elif _code=$(mint_code) && [ -n "$_code" ]; then
    OPEN_URL="${URL}setup#code=$_code"
    say 'first setup (choose a model): %s' "$OPEN_URL"
    say '  this link works once, for 60 seconds; later: nokker open /setup'
  else
    say 'first setup (choose a model): run nokker open /setup'
  fi
elif [ "$OPEN" = 1 ] && [ "$MODE" = install ] && _code=$(mint_code) && [ -n "$_code" ]; then
  # Setup déjà fait (réinstallation) : la racine avec un code d'accès, seulement passé au navigateur (jamais affiché).
  OPEN_URL="${URL}#code=$_code"
fi
remote_hint "$WEB_PORT"
if [ "$OPEN" = 1 ] && { [ "$MODE" = install ] || [ "$SETUP_TODO" = 1 ]; }; then
  if [ "$os" = darwin ]; then open "$OPEN_URL" 2>/dev/null || true
  elif need xdg-open && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then xdg-open "$OPEN_URL" >/dev/null 2>&1 || true; fi
fi
