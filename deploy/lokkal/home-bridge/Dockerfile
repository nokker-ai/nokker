# Image de la passerelle du foyer Lokkal (docs/LOKKAL-HOME-BRIDGE.md) : `python -m nokker.homebridge serve|discovery|audio`
# (profils compose home, home-mdns, home-mic). N'embarque que les paquets du noyau dont la passerelle a besoin — ni
# la base, ni le moteur, ni les secrets du noyau. Contexte de build : la racine du dépôt.
# Alpine (353 Mo, contre 916 Mo sur Debian slim : le ffmpeg de bookworm tire ~550 Mo de bibliothèques). ffmpeg :
# flux Snapcast et extraits audio ; pyatv (AirPlay) et PyChromecast (Cast), licence MIT, aux versions vérifiées par
# p2/cast-display (extra `cast` de pyproject.toml) ; PyYAML pour home.yaml ; websockets pour l'API de Home Assistant.
FROM python:3.13-alpine
# linux/arm64 : miniaudio (dépendance de pyatv) n'a pas de roue musllinux aarch64, pip la compile → compilateur C/C++
# le temps du pip install seulement (paquets virtuels .build-deps retirés dans la même couche).
RUN apk add --no-cache ffmpeg ca-certificates \
    && apk add --no-cache --virtual .build-deps build-base \
    && pip install --no-cache-dir "pyatv==0.18.0" "PyChromecast==14.1.0" "PyYAML==6.0.2" "websockets==15.0.1" \
    && apk del .build-deps \
    && adduser -D -H -u 10002 homebridge
COPY nokker/__init__.py /opt/home/nokker/__init__.py
# arrêt idempotent des fils de fond (7ba8d961, déplacé hors de nokker.core par d35e2cc1) : importé par homebridge/server.py
COPY nokker/bg_stop.py /opt/home/nokker/bg_stop.py
COPY nokker/homebridge /opt/home/nokker/homebridge
COPY nokker/ambient /opt/home/nokker/ambient
COPY nokker/cast /opt/home/nokker/cast
WORKDIR /opt/home
ENV PYTHONPATH=/opt/home PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# websockets : API WebSocket de Home Assistant (registres, appels de service, abonnement ; nokker/homebridge/ha.py).
# Absent jusqu'au 07.11 : toute opération ha.* par WebSocket répondait 502 (kiosque « maison vide », acte 5).
RUN python -c "import nokker.homebridge.server, nokker.homebridge.audio, nokker.cast.bridge, pyatv, pychromecast, websockets.sync.client"
USER homebridge
# 8800 : API du noyau (homeapi, jamais publiée) ; 8801 : écoute du foyer ; 8802 : pages des écrans (cast.*, CAST_LISTEN).
EXPOSE 8800 8801 8802
ENTRYPOINT ["python", "-m", "nokker.homebridge"]
CMD ["serve"]
