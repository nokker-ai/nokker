# syntax=docker/dockerfile:1
# Navigateur de checkout DÉDIÉ de l'agent de paiement (Lokkal, profil `pay`, docs/PAY-AGENT.md §11) : Chromium sans
# tête, joint par l'agent en CDP sur le réseau `paynet` (interne) seulement ; jamais le navigateur du modèle.
#   docker build -f docker/paychrome/Dockerfile -t nokker-paychrome .
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-dejavu-core ca-certificates socat \
    && rm -rf /var/lib/apt/lists/* && useradd --system --uid 10091 --create-home paychrome
USER 10091
EXPOSE 9223
# Chromium n'écoute qu'en boucle locale (--remote-debugging-address n'est plus respecté) : socat relaie 9223 → 9222
# sur le réseau interne du conteneur. Profil jetable dans /tmp (tmpfs) : aucune session ne survit à un redémarrage.
CMD ["sh", "-c", "chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --no-first-run --user-data-dir=/tmp/profile --remote-debugging-port=9222 about:blank & exec socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222"]
