"""Passerelle du foyer `home-bridge` (docs/LOKKAL-HOME-BRIDGE.md) : le seul composant de Lokkal qui touche le réseau
local. Le noyau lui parle par des opérations nommées (`POST /v1/op/<nom>`, jeton `HOME_BRIDGE_TOKEN`). Ce paquet ne
lit ni la base ni les secrets du noyau : seulement `home.yaml` et ses propres secrets (`/etc/home/secrets/`)."""
