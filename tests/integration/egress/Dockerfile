# Image de TEST du compose local de l'injection des secrets (docs/EGRESS-SECRETS.md §8) : le paquet `nokker` tel quel
# `cryptography` et `httpx` (le noyau simulé lit nokker.secrets ; le proxy y reprend la garde SSRF du noyau).
FROM python:3.13-alpine
RUN pip install --no-cache-dir "cryptography>=42" httpx && apk add --no-cache curl
COPY --from=src . /opt/nokker/nokker
COPY . /opt/nokker/egress-test
WORKDIR /opt/nokker
ENV PYTHONPATH=/opt/nokker PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
