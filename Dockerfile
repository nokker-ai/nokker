# syntax=docker/dockerfile:1
# Nokker (nokker) — image minimale pour le socle F0.
#
# Contrairement à l'image P0 (hermes-nokker, construite depuis la SOURCE complète
# de la bibliothèque d'agent amont avec le runtime s6-overlay amont), cette image installe hermes-agent comme
# DÉPENDANCE ÉPINGLÉE (pip, depuis la branche patchée hermes-nokker — D0, PLAN-P1
# §2.1/§2.2). Le binaire `nokker` que fournit cette dépendance (alias de couche de marque,
# docs/BRAND.md) suffit à tenir le contrat de sortie F0 : `gateway run` + `/healthz` 200,
# comme l'image P0. Le noyau propre (nokker/core, context, qa, route…) n'est pas encore
# écrit (squelette vide posé par F0, cf. docs/PLAN-P1.md) — les chantiers F2+ brancheront
# la vraie CMD de ce Dockerfile sur `nokker/api/server.py` au lieu de la passerelle amont brute.
#
# hermes-nokker (public : https://github.com/nokker-ai/hermes-nokker, tag nokker-hermes-v0.22.2-n1) est cloné
# EN AMONT du build, comme étape du workflow (.github/workflows/lokkal-images.yml), dans `hermes-src/`, pas dans
# une RUN de ce Dockerfile.
# Essayé d'abord en RUN BuildKit (secret mount + `--network=host` + `--allow network.host`) :
# échoue systématiquement (exit 128, ~90 s) — le mode « sandbox » du worker OCI de BuildKit
# (confirmé : `org.mobyproject.buildkit.worker.oci.process-mode:sandbox`) retire l'accès
# réseau d'une étape RUN même avec l'entitlement accordée des deux côtés. Cloner dans le
# contexte du job (même réseau que `docker login`/`actions/checkout`, qui marchent) puis
# `COPY` le résultat dans l'image est plus simple ET plus fiable — zéro dépendance réseau
# pendant `docker build`.
#
# hermes-agent pins its CORE dependencies (ruamel.yaml, httpx, openai, …) with a
# `python_version >= '3.14'` marker and nothing for lower versions (hermes-nokker/.python-version
# = 3.14; its own CI pins `uv python install 3.14`) — Python 3.12 silently resolves to zero core
# deps and crashes on import. Must match: 3.14.
# --- hermes-src élagué AVANT d'entrer dans l'image : un `rm` dans une RUN de `runtime` laisserait tout dans la couche du
# COPY. On ne garde que ce qui s'exécute (paquets de [tool.setuptools.packages.find], modules racine, skills, assets,
# locales, ui) : docs, déploiement, CI, scripts, site, tests et apps desktop de hermes-nokker ne sont pas du runtime et
# décrivent notre infra interne (audit des images rc.3, 24.10.2026). Images Lokkal ET bêta.
FROM python:3.14-slim-bookworm AS hermes-pruned
COPY hermes-src /h
RUN set -eu; cd /h; \
    rm -rf .forgejo .github docs deploy scripts site website tests tests-js evals apps contributors nix \
           .coderabbit.yaml .envrc .env.example justfile lefthook.yml flake.nix flake.lock Dockerfile \
           docker-compose.yml docker-compose.windows.yml AGENTS.md UPSTREAM.md CONTRIBUTING.md CONTRIBUTING.es.md \
           SECURITY.md SECURITY.es.md README.es.md README.ur-pk.md README.zh-CN.md; \
    test -f pyproject.toml && test -d agent && test -d gateway && test -f README.md

FROM python:3.14-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates build-essential curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/nokker

# --- hermes-agent épinglé (branche patchée hermes-nokker, clonée en amont par le workflow) ---
# hermes-nokker/setup.py blocks `bdist_wheel`/`sdist` on purpose (upstream distributes via
# shell installer / Docker image / Nix only — see pyproject.toml comment). The only install
# that works is an EDITABLE one (`build_editable`, never `bdist_wheel`), with extras passed on
# a plain local path (`-e "<path>[extras]"` — unambiguous, always supported; a VCS `#egg=` URL
# with extras is rejected by modern pip, "invalid-egg-fragment"). `hermes-src/` arrives via
# `COPY` (déjà clonée, `.git` déjà retiré par le workflow) : un install éditable importe
# depuis cet arbre au runtime — ne jamais le supprimer après ce COPY.
COPY --from=hermes-pruned /h ./hermes-src
RUN pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir -e "./hermes-src[messaging,stt-whisper]"

# --- le paquet nokker (noyau, F2+) --------------------------------------------------------
# `--no-deps` sur l'install de `nokker` lui-même : on ne laisse PAS pip re-résoudre les dépendances
# cœur de hermes-agent (exact-pinnées ci-dessus) à partir des bornes plus larges de nokker. Les
# dépendances RÉELLES de nokker (providers/sokkan.py, tools/mcp.py, memory/corthexis.py,
# store/pg.py, config.py — F2) sont donc installées en plus, explicitement : sans ça le conteneur
# démarre puis crashe à l'import (`ModuleNotFoundError: mcp`) — régression trouvée en réel au smoke
# CI du 17.10.2026 (build job échoué), pas qu'une note en l'air.
COPY pyproject.toml README.md ./
COPY nokker ./nokker
RUN pip install --no-cache-dir --no-deps -e .; \
    pip install --no-cache-dir "mcp==2.0.0" "psycopg[binary]>=3.2" "httpx>=0.28" "pyyaml>=6.0" "cryptography>=42" "httpcore>=1.0" "trafilatura>=2.3"

# --- moteur CortHeXis : mémoire longue directe de Lokkal (nokker/memory/corthexis.py, X1b), mémoire de session du
# « never clear » (nokker/context/corthexis_index.py), mémoire de projet (X1) ---
# Dépôt public, archive du COMMIT du tag v2.1.2 (un tag se déplace, un commit non) vérifiée par sha256 (pas de git dans
# l'image). `--no-deps` : ses dépendances lourdes ne servent pas ici (le noyau n'utilise que l'indexeur, la recherche
# hybride pgvector et les clients d'embeddings HTTP ; son `mcp<2` contredirait notre mcp 2.0) — les seules manquantes
# sont installées à part (vérifié dans un venv propre le 19.10.2026). Le build échoue si l'un des modules du mode direct
# ne s'importe pas : une image sans mémoire longue ne sort plus (trou trouvé le 25.10, lot X1).
ARG CORTHEXIS_COMMIT=8607271381c7045661834c2103366640d5ef2749
ARG CORTHEXIS_SHA256=fb1c7772629bb8eb1667b84294bbacb5448d17afc392cd8a70878b4d14750d95
RUN set -eu; \
    curl -fsSL -o /tmp/corthexis.tar.gz "https://github.com/ninabot-ch/corthexis/archive/${CORTHEXIS_COMMIT}.tar.gz"; \
    echo "${CORTHEXIS_SHA256}  /tmp/corthexis.tar.gz" | sha256sum -c -; \
    pip install --no-cache-dir --no-deps /tmp/corthexis.tar.gz; rm /tmp/corthexis.tar.gz; \
    pip install --no-cache-dir "psycopg-pool>=3.2" "pgvector>=0.3" "numpy>=1.26"; \
    python3 -c "import corthexis.service, corthexis.indexer, corthexis.embed, corthexis.store, pgvector.psycopg, \
psycopg_pool, numpy; from importlib.metadata import version; assert version('corthexis') == '2.1.2', version('corthexis')"

# --- /healthz additif (nokker, pas l'amont — voir docker/nokker/healthz_server.py) ---
COPY docker/nokker/healthz_server.py ./docker/nokker/healthz_server.py
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV API_SERVER_HOST=0.0.0.0 \
    API_SERVER_PORT=8642 \
    NOKKER_HEALTHZ_HOST=0.0.0.0 \
    NOKKER_HEALTHZ_PORT=8643

EXPOSE 8642 8643

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gateway", "run"]

# --- Lokkal : la même image, plus le CLI docker (sans dockerd) pour le bac à sable par fil (nokker/tools/code.py) ---
# Construit seulement avec `--target lokkal` (workflow des images Lokkal). Le CLI parle au démon par le proxy filtrant
# du compose (DOCKER_HOST=tcp://dockerproxy:2375, deploy/lokkal/sandboxd), jamais au socket. Binaire statique officiel,
# épinglé par sha256 (version de plus de deux semaines au moment de l'épinglage).
FROM runtime AS lokkal
ARG TARGETARCH
ARG DOCKER_CLI_VERSION=29.8.2
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) a=x86_64;  sum=995d1ef289677f74fd58d8d2c35727b6a4ee389c69db8638a3e42d0487aa5b0f ;; \
      arm64) a=aarch64; sum=76a624e4a8e5da654d1150e808175125efb5a6f1b6aa1cbd9caee18f51047a50 ;; \
      *) echo "architecture non prise en charge : $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/docker.tgz "https://download.docker.com/linux/static/stable/$a/docker-${DOCKER_CLI_VERSION}.tgz"; \
    echo "$sum  /tmp/docker.tgz" | sha256sum -c -; \
    tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1 docker/docker; \
    rm /tmp/docker.tgz; docker --version
# kubectl : bac à sable Kubernetes (X19, nokker/tools/k8s_sandbox.py, chart deploy/helm/nokker) — pods de session créés
# et pilotés (apply, wait, exec, delete) avec le compte de service du pod du noyau. Binaire officiel épinglé par sha256 ;
# 1.36 couvre les API 1.35 à 1.37, et les verbes utilisés (Pod v1, exec) sont stables bien en deçà.
ARG KUBECTL_VERSION=v1.36.5
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) sum=33bc88a24c3b09cf55bfd59e6ca977881b61fc3f25ab65bc80c65b430248648e ;; \
      arm64) sum=88fc1aca8fd0c1b44fe379f67c39bd6858bdfc50241dc55775f84d0f5b31da3a ;; \
      *) echo "architecture non prise en charge : $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    echo "$sum  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 755 /usr/local/bin/kubectl; kubectl version --client
# git : `nokker review` (X13, action GitHub/Forgejo) lit le diff de la PR dans cette image ; pas dans l'image bêta.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* && git --version

# --- Stage par défaut (dernier du fichier) = l'image de la BÊTA, identique à `runtime` : rien de docker en prod ---
FROM runtime AS beta
