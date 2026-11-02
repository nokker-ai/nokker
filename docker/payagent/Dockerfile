# syntax=docker/dockerfile:1
# Agent de paiement Nokker (docs/PAY-AGENT.md) : image SÉPARÉE du noyau et de Lokkal. Ni hermes, ni modèle, ni outils :
# la bibliothèque standard, `cryptography` (vérification des passkeys) et `psycopg` (ses tables, 4 colonnes de
# `devices`). Du noyau, seuls deux modules PURS sont copiés : la vérification des mandats et les primitives WebAuthn.
#   docker build -f docker/payagent/Dockerfile -t nokker-pay-agent .
FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "cryptography>=42" "psycopg[binary]>=3.2" \
    && useradd --system --uid 10090 --home /app payagent
WORKDIR /app
COPY nokker/__init__.py nokker/__init__.py
COPY nokker/approvals/__init__.py nokker/approvals/crypto.py nokker/approvals/
COPY nokker/payments/__init__.py nokker/payments/mandate.py nokker/payments/
COPY payagent/ payagent/
USER 10090
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=2)" || exit 1
CMD ["python", "-m", "payagent"]
