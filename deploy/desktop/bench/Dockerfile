# B1b — variante de TEST du Dockerfile de référence (deploy/desktop/Dockerfile) : le tarball déjà vérifié au spike est
# copié (pas de téléchargement), mêmes contrôles sha256 ; + Nautilus, Writer, appli GTK4 de test et systemd-analyze
# pour les mesures. Image jetable, supprimée à la fin.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb dbus dbus-x11 at-spi2-core libatk-adaptor gir1.2-atspi-2.0 metacity \
      gnome-text-editor thunar nautilus libreoffice-writer libreoffice-gtk3 \
      python3-gi gir1.2-gtk-4.0 fonts-dejavu-core x11-utils libglib2.0-bin \
      python3 python3-cryptography python3-httpx ca-certificates systemd \
 && rm -rf /var/lib/apt/lists/*
# le tarball épinglé, posé à côté du contexte (jamais téléchargé par ce banc)
COPY ad.tar.gz /tmp/ad.tar.gz
RUN echo "591ae7f4a38479dc045df97e27447c0c8b530441730ab6b990e27c813162af44  /tmp/ad.tar.gz" | sha256sum -c - \
 && mkdir /tmp/ad && tar -xzf /tmp/ad.tar.gz -C /tmp/ad agent-desktop agent-desktop-linux-clipboard-owner \
 && echo "da52a1eb2152cdc21b756ab20e6208d242da8062c73cd20bc8a8ae2a0384392c  /tmp/ad/agent-desktop" | sha256sum -c - \
 && echo "3e96f92b805c0d1f9de309935d62f4189b8e11e1385ad022bb878487bb3942ec  /tmp/ad/agent-desktop-linux-clipboard-owner" | sha256sum -c - \
 && install -m 0755 /tmp/ad/agent-desktop /tmp/ad/agent-desktop-linux-clipboard-owner /usr/local/bin/ \
 && rm -rf /tmp/ad /tmp/ad.tar.gz
COPY deploy/desktop/agent-desktop.lock.json /usr/local/share/nokker-desktop/agent-desktop.lock.json
COPY deploy/desktop/entrypoint.sh /usr/local/bin/nokker-desktop-entrypoint
COPY deploy/desktop/entrypoint-session.sh /usr/local/bin/nokker-desktop-session
COPY deploy/desktop/private-bus.conf /usr/local/share/nokker-desktop/private-bus.conf
COPY deploy/desktop/session-run.sh /opt/b1b/session-run.sh
COPY nokker /opt/nokker/nokker
COPY deploy/desktop/bench/gtk4app.py deploy/desktop/bench/b1b.py deploy/desktop/bench/session_bus.py deploy/desktop/nokker-desktop.service /opt/b1b/
ENV PYTHONPATH=/opt/nokker
RUN useradd -m -u 10010 -s /bin/bash desk && mkdir -p /home/desk/files && chown desk:desk /home/desk/files
USER desk
WORKDIR /home/desk
