# Bureau : ce que l'emballage Lokkal doit livrer (docs/DESKTOP.md)

Ce dossier est la **référence** pour l'emballage (compose, `install.sh` / `install.ps1`, signature). Le nœud, sa
politique et les routes du noyau sont dans `nokker/desktop/` ; ils sont testés (`tests/unit/desktop/`) et ont tourné sur
le vrai binaire (banc `bench/`, DESKTOP.md §11 : virtual 26/26, session simulée 22/22).

Décisions (06.11.2026) : image par défaut = gnome-text-editor + **Thunar** ; `desktop-office` (Writer) = variante
facultative ; mode `session` = facultatif, seulement sur choix explicite ; mode physique interdit en session jusqu'à B2.

## 1. Binaire épinglé

| Fichier | sha256 |
|---|---|
| `agent-desktop-v1.2.0-x86_64-unknown-linux-gnu.tar.gz` (release GitHub `vercel-labs/agent-desktop`, tag `v1.2.0`) | `591ae7f4a38479dc045df97e27447c0c8b530441730ab6b990e27c813162af44` |
| `agent-desktop` (extrait du tarball) | `da52a1eb2152cdc21b756ab20e6208d242da8062c73cd20bc8a8ae2a0384392c` |
| `agent-desktop-linux-clipboard-owner` (extrait) | `3e96f92b805c0d1f9de309935d62f4189b8e11e1385ad022bb878487bb3942ec` |

- Source de vérité : `agent-desktop.lock.json` (copié dans l'image : le nœud recalcule le sha256 du binaire à CHAQUE
  démarrage et refuse de démarrer s'il diffère, code 2).
- Le build vérifie le tarball (`ADD --checksum`) PUIS les deux binaires (`sha256sum -c`), contre les valeurs épinglées,
  jamais contre le `checksums.txt` de la release.
- Jamais : postinstall npm, `agent-desktop skills`, `scripts/jev` (TypeSafe, OpenRouter), `agent-desktop mcp`. Seuls
  les deux binaires entrent dans l'image.
- x86_64 seulement (arm64 refusé : `unsupported_arch`, tant qu'il n'est pas épinglé et testé).
- Licence : Apache-2.0 (ligne dans docs/LICENSES-DEPENDENCIES.md).

## 2. Image `desktop` (mode virtual, par défaut)

`Dockerfile` (référence ; base à épingler par digest à l'emballage) :
- paquets : `xvfb dbus dbus-x11 at-spi2-core libatk-adaptor gir1.2-atspi-2.0 metacity gnome-text-editor thunar
  fonts-dejavu-core python3 python3-cryptography python3-httpx ca-certificates` (base Ubuntu 24.04 : ≈ 720 Mo) ;
  `python3-httpx` est requis (nokker.redact.rules l'importe par nokker.tools.web ; jamais appelé par le nœud) ;
- le paquet `nokker` copié dans `/opt/nokker` (`PYTHONPATH`), utilisateur `desk` uid **10010**, `/state` 0700 ;
- `entrypoint.sh` : Xvfb `:99` (`-nolisten tcp`), bus de session (`dbus-run-session`), bus a11y
  (`at-spi-bus-launcher`), **WM EWMH (metacity) obligatoire** (sans lui : `native_window_unproven`), puis
  `python3 -m nokker.desktop.node` ;
- `HEALTHCHECK` : `python3 -m nokker.desktop.node --health` (lit le JSON de `status` + `list-windows` ; le code retour
  de `agent-desktop status` vaut 0 même en panne).

Variante **`desktop-office`** (facultative, jamais par défaut) : `--build-arg OFFICE="libreoffice-writer
libreoffice-gtk3"`, tag distinct. Writer est trop gros en `-i` (1 623 refs) : le nœud passe seul au palier skeleton
(3,3 Ko mesurés). Appli à permettre côté noyau : `soffice`.

## 3. Compose

```yaml
services:
  core:
    networks: [..., desktopnet]          # en plus de ses réseaux actuels
  desktop:
    image: <registre>/nokker-desktop@sha256:<digest>
    profiles: [desktop]
    user: "10010:10010"
    read_only: true
    tmpfs: ["/run/nokker-desktop:mode=0700,uid=10010", "/tmp"]
    volumes: ["desktop-state:/state", "${NOKKER_HOME}/desktop/files:/home/desk/files"]
    environment:
      NOKKER_CORE_URL: "http://core:8742"
      NOKKER_CORE_HOST: "127.0.0.1:8742"   # Host admis par le noyau (déjà dans NOKKER_ALLOWED_HOSTS)
      NOKKER_DESKTOP_NAME: "bureau"
    networks: [desktopnet]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
networks:
  desktopnet: {internal: true}           # core ↔ desktop seulement : aucune sortie
volumes:
  desktop-state: {}
```

- `NOKKER_CORE_HOST` : le noyau refuse tout `Host` non déclaré (protection DNS rebinding) ; le nœud joint `core:8742`
  en annonçant `127.0.0.1:8742`, comme `PAYAGENT_CALLBACK_HOST`, **sur `desktopnet` seulement** (jamais un port publié).
- Le dossier `${NOKKER_HOME}/desktop/files` est le seul dossier de la personne que le bureau virtual voit.

## 4. Noyau (config)

```yaml
local:
  desktop:
    enabled: true
    mode: virtual                        # ou session, écrit tel quel (une autre valeur = virtual)
    apps:
      virtual: [gnome-text-editor, thunar]   # + soffice avec desktop-office
      session: []                        # vide par défaut : la personne l'allonge (écran des réglages)
    # physical: {virtual: allow, session: never}   (défauts ; session reste never jusqu'à B2)
    # snapshot_max_kib: 12  (1 à 32)      wait_s: 20 (1 à 60)
```

- Un seul mode par machine, **aucun repli** : seul un nœud de ce mode s'inscrit (`409 mode_not_allowed` sinon).
- Aucun outil n'est offert au modèle tant qu'aucun nœud du mode n'est inscrit.
- Refusées en permanence, même listées : terminaux, gestionnaires de mots de passe, réglages, verrouillage,
  navigateurs, Nokker (`nokker/desktop/policy.py::DENIED_APPS`).

## 5. Première mise en route (les deux modes)

1. Le nœud démarre, crée sa clé P-256 (`/state/node-key.pem`, 0600) et demande son inscription :
   `POST /v1/desktop/node/enroll` → **carte forte** « Relier le bureau … » chez la personne.
2. La personne l'approuve (`nokker` sur la machine ou l'app) ; le nœud retire son jeton UNE fois, sous preuve de sa clé
   (`/state/node-token`, 0600 ; gardé haché côté noyau).
3. Battement toutes les 60 s ; relève de la file en long poll. Retrait : `DELETE /v1/local/desktop/nodes/{id}` (jeton
   local, sur la machine), puis effacer `desktop-state`.

`install.sh --desktop` (proposition) : profil compose `desktop`, `local.desktop.enabled: true`, mode virtual ; puis
attendre la carte et dire à la personne de l'approuver.

## 6. Mode `session` (facultatif)

Seulement si la personne le choisit explicitement (`install.sh --desktop-session` ou l'écran des réglages), après le
**texte de mise en place** (§7), confirmé par elle. **X11 seulement** (`XDG_SESSION_TYPE=wayland` : refusé).

Le conteneur ne reçoit **jamais** le bus de session de la personne (ni Secret Service, ni portails, ni
notifications) : il a son propre bus privé, sans activation, où seul `org.a11y.Bus` existe (DESKTOP.md §11.1).

- À poser par l'installeur :
  - `session-run.sh` → `~/.nokker/bin/nokker-desktop-session-run` (0755) ; il lit, SUR L'HÔTE, l'adresse du bus
    d'accessibilité (`gdbus`, sinon `busctl --user`), refuse un socket abstrait ou une session non X11, puis lance le
    conteneur avec : l'écran X (`/tmp/.X11-unix`, ro), le cookie X s'il existe (`$XAUTHORITY`, ro), le dossier du socket
    d'accessibilité (ro, même chemin), l'état `~/.nokker/desktop/state`, `--network <projet>_desktopnet`,
    `--read-only`, `--cap-drop ALL`, `--user <uid>:<gid>`, point d'entrée `/usr/local/bin/nokker-desktop-session` ;
  - `nokker-desktop.service` → `~/.config/systemd/user/`, puis
    `systemctl --user daemon-reload && systemctl --user enable --now nokker-desktop.service` ;
  - variables : `NOKKER_DESKTOP_IMAGE`, `NOKKER_DESKTOP_NETWORK` (défaut `nokker_desktopnet`, selon
    `COMPOSE_PROJECT_NAME`).
- Dans l'image : `entrypoint-session.sh` (→ `/usr/local/bin/nokker-desktop-session`), `private-bus.conf`
  (→ `/usr/local/share/nokker-desktop/`), `python3-gi` et `libglib2.0-bin` (déjà dans le `Dockerfile`).
- Noyau : `local.desktop.mode: session` et `apps.session` choisies par la personne.
- `press` et tout repli physique y sont refusés jusqu'à B2 (politique du noyau, rien à faire dans l'unité).
- Désinstallation : `systemctl --user disable --now nokker-desktop.service`, supprimer l'unité et le script, retirer
  le nœud.

## 7. Texte de mise en place du mode session (FIGÉ, relu le 06.11.2026 ; à afficher tel quel)

Ce que l'accès donne réellement (docs/DESKTOP.md §7) : le bus d'accessibilité de la session (lire ET agir dans toutes
les applis accessibles) et l'écran X (lire la frappe de toutes les fenêtres et taper dans n'importe laquelle, terminal
compris). La liste des applis permises et le refus du terminal sont la politique de Nokker, pas une barrière technique.
Le bus de session de la personne n'est PAS donné. La personne lit avant de dire oui. Source exacte à reprendre par
l'installeur : `session-setup.json` (une ligne par langue ; ci-dessous, retours à la ligne de mise en page seulement) :

EN :
> Session mode lets Nokker work in the apps of YOUR desktop, in front of you. To do this, its container gets access to
> your session's accessibility bus (what your apps show, what you type in their fields, passwords included in some
> apps, and the ability to act in them) and to your X display (which can type into any window, terminal included). It
> does not get your session bus: neither your keyring nor your other services. Under X11, any program with access to
> the display can read what you type in all your windows. Nokker only drives the apps you allowed, never the terminal,
> and never reads nor types a password. It asks for your approval before it writes, except in an app where you allowed
> it for the session, and always before it sends or deletes anything. But the container keeps these accesses across
> all your apps: a flaw in Nokker or in an app it drives could use them. Turn this mode on only if you accept these
> accesses on this account. The isolated mode (default) has none of these accesses. Turn on session mode? [y/N]

FR :
> Le mode session permet à Nokker de travailler dans les applis de VOTRE bureau, sous vos yeux. Pour cela, son
> conteneur reçoit l'accès au bus d'accessibilité de votre session (ce que vos applis affichent, ce que vous tapez
> dans leurs champs, mots de passe compris dans certaines applis, et la possibilité d'y agir) et à votre écran X (qui
> permet de taper dans n'importe quelle fenêtre, terminal compris). Il ne reçoit pas votre bus de session : ni votre
> trousseau, ni vos autres services. Sous X11, tout programme qui a accès à l'écran peut lire ce que vous tapez dans
> toutes vos fenêtres. Nokker ne pilote que les applis que vous avez permises, jamais le terminal, et ne lit ni ne
> tape jamais un mot de passe. Il demande votre accord avant d'écrire, sauf dans une appli où vous l'avez accordé pour
> la session, et toujours avant d'envoyer ou de supprimer. Mais le conteneur garde ces accès sur toutes vos applis :
> une faille de Nokker ou d'une appli pilotée pourrait s'en servir. N'activez ce mode que si vous acceptez ces accès
> sur ce compte. Le mode isolé (par défaut) n'a aucun de ces accès. Activer le mode session ? [o/N]

## 8. Ce qui est vérifié, et où

| Exigence | Où |
|---|---|
| sha256 du tarball et des binaires, au build | `Dockerfile` |
| sha256 du binaire à chaque démarrage | `nokker/desktop/node.py::check_pin` |
| Santé par le JSON (bus absent → `no_dbus`, WM absent → `no_wm`) | `node.py::health`, banc §11 |
| État amont éphémère par tâche (tmpfs, 0700), balayé au démarrage | `node.py::Jobs` |
| Aucune capture d'écran, aucune session ni trace amont | `node.py` (aucune commande `screenshot`, `session`, `trace`) |
| `press` (XTEST) : physique exigé, focus vérifié sur la fenêtre cible | `node.py::_press`, banc §11 |
| Mode session : jamais le bus de session de la personne (bus privé, seul `org.a11y.Bus`) | `a11y_shim.py`, `session-run.sh`, `entrypoint-session.sh`, `private-bus.conf`, banc `bench/session_bus.py` |
| Banc rejouable sur le vrai binaire (tarball posé à côté, jamais téléchargé) | `bench/` |
