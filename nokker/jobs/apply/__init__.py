"""Pack Emploi, lot L2 : dépôt des candidatures par Lokkal (`jobsd`, docs/JOBS-PACK.md §6-§7).

`jobsd` est une couche des sites connectés (`sitesd`) : mêmes pilotes (vrai Chrome de `sites-chrome` ou site simulé),
même masquage, même coffre, mêmes demandes (code, passation de l'écran), même gouverneur. Il ajoute :
- les recettes de portails par ATS (`portals.py` : Workday, SuccessFactors), un geste de lecture `form_scan` et un
  geste d'écriture `apply` ;
- le pré-remplissage à blanc, la carte qui montre tout le plan, le rejeu exact à l'approbation (`desk.py`) ;
- la fiche de candidature de la personne (`sheet.py`) et le journal des dépôts avec leurs preuves (`journal.py`) ;
- l'outil `jobs_apply` (Lokkal seulement) et sa porte (`tool.py`).

Portails SIMULÉS seulement dans ce lot (`fakeats.py`, `fakeats_http.py`) : aucun portail réel n'est touché.
"""
