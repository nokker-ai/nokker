# Image des deux gardiens du bac à sable Lokkal : `dockerproxy` (socket docker filtré pour le noyau) et `egress`
# (seule sortie des bacs à sable, plages privées refusées). Bibliothèque standard Python seulement.
FROM python:3.13-alpine
RUN adduser -D -H -u 10001 sandboxd
COPY dockerproxy.py egress.py /opt/sandboxd/
WORKDIR /opt/sandboxd
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# `egress` tourne sous sandboxd ; `dockerproxy` tourne en root (propriétaire du socket), sans aucune capacité (compose).
USER sandboxd
CMD ["python3", "egress.py"]
