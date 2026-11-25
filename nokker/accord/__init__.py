"""ACCORD — appareil d'accord physique (Autoriser / Refuser) qui tranche les cartes d'accord de Lokkal sur le LAN.

Contrat : docs/ACCORD-DEVICE.md (v1.1). Hors du noyau, derrière le drapeau `local.accord.enabled` (faux par défaut) :
`protocol` (octets canoniques, vérifications pures), `policy` (niveau et titre d'une carte), `store` (clés d'appareil),
`hub` (appairage, file d'une carte à la fois, décisions par l'API d'approbation existante), `server` (WSS sur le LAN),
`simulator` (appareil émulé en Python, tests et démo)."""
