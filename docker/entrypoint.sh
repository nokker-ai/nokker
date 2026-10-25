#!/bin/sh
# Nokker (nokker) — entrypoint minimal.
#
# Ce dépôt n'embarque pas le runtime s6-overlay de la bibliothèque amont (il vit dans
# hermes-nokker, construit depuis la source). Cette image installe
# hermes-agent comme DÉPENDANCE ÉPINGLÉE (pip, voir pyproject.toml et
# Dockerfile) : le binaire `nokker` qu'elle fournit (alias de couche de
# marque posé dans hermes-nokker, cf. docs/BRAND.md) suffit pour le contrat
# de sortie F0 (`gateway run` + `/healthz`), sans réinventer le noyau ici
# (cf. docs/PLAN-P1.md §2.2 — nokker/core etc. sont posés en squelette vide).
set -eu

python3 /opt/nokker/docker/nokker/healthz_server.py &

exec nokker "$@"
