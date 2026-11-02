"""Injection des secrets à la sortie (docs/EGRESS-SECRETS.md) : le bac à sable n'écrit que des références
`secret:NOM`, le proxy de sortie pose la valeur au départ de la requête (hôtes et emplacements déclarés par secret)
et la remet en référence dans les réponses. Drapeau `egress_secrets.enabled`, éteint par défaut.

- `policy` : déclarations, substitution, refus, caviardage (pur, sans réseau) ;
- `proxy`  : le proxy (`python -m nokker.egress serve`) et son canal de contrôle ;
- `ca`     : l'autorité de certification locale du proxy ;
- `kernel` : côté noyau (ouverture d'une session au proxy, environnement du bac à sable, journal).
"""
