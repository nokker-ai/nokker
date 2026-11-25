# Image `nokker-desktop` du bureau (docs/DESKTOP.md §1.1, §1.3), construite par lokkal-images (amd64 seulement : le
# binaire amont n'existe qu'en x86_64 sous Linux), signée cosign comme les autres images Lokkal. Bureau minimal, binaire
# agent-desktop épinglé (tarball ET binaires), base épinglée par digest, utilisateur non root, aucun npm.
# Même image pour les deux modes : `virtual` (service `desktop` du compose : Xvfb, entrypoint) et `session` (unité
# systemd --user nokker-desktop.service : écran X et bus de la personne, `--entrypoint python3`).
# Applis : gnome-text-editor (GTK4, arbre compact) et Thunar (décision du 06.11 après mesure B1b : +5 Mo contre
# +135 Mo pour Nautilus). Variante FACULTATIVE `desktop-office` : `--build-arg OFFICE="libreoffice-writer
# libreoffice-gtk3"` (Writer : le nœud passe seul au palier skeleton, 3,3 Ko mesurés) ; jamais l'image par défaut.
ARG BASE=ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM ${BASE} AS fetch
ARG AD_URL=https://github.com/vercel-labs/agent-desktop/releases/download/v1.2.0/agent-desktop-v1.2.0-x86_64-unknown-linux-gnu.tar.gz
ARG AD_TARBALL_SHA256=591ae7f4a38479dc045df97e27447c0c8b530441730ab6b990e27c813162af44
ARG AD_BIN_SHA256=da52a1eb2152cdc21b756ab20e6208d242da8062c73cd20bc8a8ae2a0384392c
ARG AD_CLIP_SHA256=3e96f92b805c0d1f9de309935d62f4189b8e11e1385ad022bb878487bb3942ec
ADD --checksum=sha256:${AD_TARBALL_SHA256} ${AD_URL} /tmp/ad.tar.gz
RUN mkdir /tmp/ad && tar -xzf /tmp/ad.tar.gz -C /tmp/ad agent-desktop agent-desktop-linux-clipboard-owner \
 && echo "${AD_BIN_SHA256}  /tmp/ad/agent-desktop" | sha256sum -c - \
 && echo "${AD_CLIP_SHA256}  /tmp/ad/agent-desktop-linux-clipboard-owner" | sha256sum -c -

FROM ${BASE}
ARG FILE_MANAGER=thunar
ARG OFFICE=
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb dbus dbus-x11 at-spi2-core libatk-adaptor gir1.2-atspi-2.0 metacity \
      gnome-text-editor ${FILE_MANAGER} ${OFFICE} fonts-dejavu-core \
      python3 python3-cryptography python3-httpx python3-gi libglib2.0-bin ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /tmp/ad/agent-desktop /tmp/ad/agent-desktop-linux-clipboard-owner /usr/local/bin/
COPY deploy/desktop/agent-desktop.lock.json /usr/local/share/nokker-desktop/agent-desktop.lock.json
COPY deploy/desktop/entrypoint.sh /usr/local/bin/nokker-desktop-entrypoint
# mode session : bus de session privé (seul org.a11y.Bus), jamais celui de la personne
COPY deploy/desktop/entrypoint-session.sh /usr/local/bin/nokker-desktop-session
COPY deploy/desktop/private-bus.conf /usr/local/share/nokker-desktop/private-bus.conf
# Le nœud est le paquet nokker lui-même (nokker.desktop + ce qu'il importe : policy, redact, qa ; httpx vient de
# nokker.redact.rules → nokker.tools.web, importé seulement, jamais appelé par le nœud) ; pas de dépendance
# réseau à l'exécution.
COPY nokker /opt/nokker/nokker
# Bus de session à adresse FIXE (entrypoint) : le contrôle de santé (docker exec) le retrouve sans hériter de
# l'environnement du processus 1. En mode session, l'unité remplace DBUS_SESSION_BUS_ADDRESS et XDG_RUNTIME_DIR.
ENV PYTHONPATH=/opt/nokker NOKKER_DESKTOP_MODE=virtual NOKKER_DESKTOP_STATE=/state HOME=/home/desk DISPLAY=:99 \
    XDG_RUNTIME_DIR=/run/nokker-desktop DBUS_SESSION_BUS_ADDRESS=unix:path=/run/nokker-desktop/bus \
    NOKKER_DESKTOP_BIN=/usr/local/bin/agent-desktop \
    NOKKER_DESKTOP_LOCK=/usr/local/share/nokker-desktop/agent-desktop.lock.json
# Le compose lance l'image sous l'uid de la personne (NOKKER_UID) : /home/desk et /run/nokker-desktop en tmpfs à son
# uid, /state et files montés depuis ~/.nokker/desktop ; `desk` (10010) ne sert qu'aux lancements sans compose.
RUN useradd -m -u 10010 -s /usr/sbin/nologin desk && mkdir -p /state /home/desk/files /run/nokker-desktop \
 && chown desk:desk /state /home/desk/files /run/nokker-desktop && chmod 700 /state /run/nokker-desktop \
 && chmod 0755 /usr/local/bin/agent-desktop /usr/local/bin/agent-desktop-linux-clipboard-owner \
    /usr/local/bin/nokker-desktop-entrypoint /usr/local/bin/nokker-desktop-session
USER desk
WORKDIR /home/desk
# Contrôle de santé : le JSON de `status` + list-windows (le code retour de `status` vaut 0 même en panne).
HEALTHCHECK --interval=60s --timeout=20s --start-period=30s --retries=3 \
  CMD ["sh", "-c", "python3 -m nokker.desktop.node --health >/dev/null"]
ENTRYPOINT ["/usr/local/bin/nokker-desktop-entrypoint"]
