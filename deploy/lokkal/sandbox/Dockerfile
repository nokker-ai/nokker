# Image des bacs à sable de Lokkal : un container jetable par fil, le dossier de l'utilisateur sur /workspace
# (nokker/tools/code.py). Les outils d'un poste de développeur courant ; le réseau passe par le proxy `egress`
# (HTTP(S)_PROXY posé par dockerproxy), pip / npm / git / curl le suivent.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-client less jq ripgrep fd-find patch file unzip xz-utils procps \
      build-essential pkg-config python3 python3-pip python3-venv python3-dev nodejs npm \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*
# pip sans --break-system-packages dans un container jetable ; pas de cache disque inutile
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    npm_config_update_notifier=false npm_config_fund=false
WORKDIR /workspace
CMD ["sleep", "infinity"]
