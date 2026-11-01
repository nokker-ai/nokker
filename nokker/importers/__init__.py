"""nokker/importers — import d'une installation d'un autre agent (Claude Code, Cursor, Hermes, OpenClaw) vers Nokker.

Parcours d'arrivée (« tu viens de X ? on importe ») : un `scan` à blanc dresse la liste de ce qui sera repris et de ce
qui ne le sera pas (avec la raison), puis `apply` écrit. Rien n'est sauté en silence : une passe de réconciliation
donne une ligne au rapport à tout fichier source qu'aucune étape n'a lu (leçon de docs/CLAW-MIGRATE.md).
Jamais une valeur de secret n'est copiée : seulement les NOMS, à ressaisir dans le coffre.
"""
