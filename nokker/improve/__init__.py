"""Auto-amélioration hors ligne sous contrôle humain (docs/SELF-IMPROVE.md) — prototype, drapeau `self_improve` éteint.

Une boucle lancée la nuit (`nokker improve run --target tools|skills|router|blocks`) fait muter les artefacts du
harness par réflexion sur des traces d'échecs (à la GEPA, arXiv 2507.19457), note chaque variante sur nos bancs, garde
un front de Pareto qualité / coût, élimine toute variante qui fait baisser une métrique de sûreté, puis propose la
meilleure par une carte d'approbation (diff, scores avant/après, coût). Elle ne modifie JAMAIS la production d'elle-même :
seule une carte approuvée écrit une version (versionnée, réversible).

Gel du format : les prompts de blocs (`--target blocks`) peuvent être PROPOSÉS, jamais appliqués tant que le gel n'est pas
levé (`self_improve_settings.block_freeze: false`) ET que la relecture du format n'a pas donné son accord sur l'empreinte exacte de la
variante (`self_improve_settings.format_ack`). Voir `proposal.py::apply_blocks`.

Modules : `overlay` (application des variantes approuvées, empreintes), `store` (versions), `traces` (lecture des
échecs), `reflect` (réflexion : modèle ou faux modèle), `benches` (bancs), `guards` (garde-fous), `loop` (boucle et
Pareto), `proposal` (carte et geste du noyau), `cli`.
"""
from __future__ import annotations

TARGETS = ("tools", "skills", "router", "blocks")
# Ordre de priorité des artefacts mutables (docs/SELF-IMPROVE.md §2) ; `blocks` : proposition seulement (gel du format).
APPLICABLE_TARGETS = ("tools", "skills", "router")

__all__ = ["APPLICABLE_TARGETS", "TARGETS"]
