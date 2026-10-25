"""Sessions de code multi-utilisateur (déploiement entreprise, docs/LOKKAL.md §7.28, docs/en/code-server.md).

Le pendant multi-locataire des sessions de code de Lokkal (`nokker/local/api.py`, `SessionCodeTools`) : un compte
authentifié (session d'appareil ou SSO) ouvre une session de code dans SON locataire ; une session = un fil, ses outils
de code (`nokker/tools/code.py`) sur SA racine `<workspace_root>/<tenant>/<user>/<session>`, un bac à sable docker ou
un pod kubernetes, les portes, cartes, points de retour et hooks de Lokkal. Drapeau : le bloc `code.enabled` de la
config (faux par défaut) ; éteint, aucune route, aucun outil, rien ne change.
"""
