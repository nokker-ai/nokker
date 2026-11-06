# syntax=docker/dockerfile:1
# Agent de paiement Nokker (docs/PAY-AGENT.md) : image SÉPARÉE du noyau et de Lokkal. Ni hermes, ni modèle, ni outils :
# la bibliothèque standard, `cryptography` (vérification des passkeys) et `psycopg` (ses tables, 4 colonnes de
# `devices`). Du noyau, seuls des modules sans état sont copiés : la vérification des mandats et les primitives WebAuthn,
# la détection des champs de carte (navigateur de checkout) et le client du coffre (own_card).
#   docker build -f docker/payagent/Dockerfile -t nokker-pay-agent .
FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# own_card (Lokkal) : httpx pour l'OpenBao local (nokker/secrets), Playwright SANS navigateur (il se joint au Chromium
# dédié du compose par CDP, profil `pay`).
RUN pip install --no-cache-dir "cryptography>=42" "psycopg[binary]>=3.2" "httpx>=0.27" "playwright>=1.48" \
    && useradd --system --uid 10090 --home /app payagent
WORKDIR /app
COPY nokker/__init__.py nokker/__init__.py
COPY nokker/approvals/__init__.py nokker/approvals/crypto.py nokker/approvals/
COPY nokker/payments/__init__.py nokker/payments/mandate.py nokker/payments/detect.py nokker/payments/cardmask.py nokker/payments/
COPY nokker/secrets/ nokker/secrets/
# dépendances sans état du client du coffre ; `nokker/connectors/__init__.py` vide : rien du noyau n'est importé
COPY nokker/qa/__init__.py nokker/qa/secrets.py nokker/qa/
COPY nokker/connectors/crypto.py nokker/connectors/errors.py nokker/connectors/
RUN touch nokker/connectors/__init__.py
COPY payagent/ payagent/
USER 10090
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=2)" || exit 1
CMD ["python", "-m", "payagent"]
