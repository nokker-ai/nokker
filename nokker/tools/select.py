"""Choix des schémas d'outils envoyés au modèle, par tour (plan breakthrough du 21.10.2026, chantier B).

Deux politiques, selon l'amont du palier Ship (`TierConfig.cache`) :

- `relevance` (amont sans cache de préfixe, ex. OVH au token : chaque token est payé à chaque appel) : les outils cœur
  toujours ; les outils déjà utilisés dans la fenêtre de la conversation ; les familles que le message demande, par
  mots-clés (jamais un appel modèle pour choisir) ; `tool_find` pour le reste. Un outil appelé sans être chargé est
  exécuté ET chargé pour la suite du tour (`core/loop.py`). Le jeu ne fait que grandir dans un tour.
- `all` (amont avec cache de préfixe, nos GPU) : tous les schémas à chaque appel, sans plafond — le bloc est stable
  d'un appel à l'autre, donc payé une fois.

L'index des outils (message système) est compact et le même pour les deux politiques : les noms par famille, stable
d'un tour à l'autre (il ne dépend pas de ce qui est chargé).
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from nokker.core.ports import ToolSpec


@dataclass(frozen=True)
class Family:
    label: str
    patterns: tuple[str, ...]  # noms d'outils de la famille (fnmatch), pour l'index
    load: tuple[str, ...]  # ce que le message charge (fnmatch) — sous-ensemble courant pour le navigateur
    match: re.Pattern[str]


def _rx(*words: str) -> re.Pattern[str]:
    return re.compile("|".join(words), re.IGNORECASE)


# Mots-clés en français et en anglais (les testeurs écrivent dans les deux). Un faux positif coûte quelques centaines
# de tokens ; un faux négatif coûte un appel de plus (tool_find) — on penche vers le chargement.
FAMILIES: tuple[Family, ...] = (
    Family("Web", ("web_*",), ("web_search", "web_fetch"), _rx(
        r"https?://", r"\bwww\.", r"\b(re)?cherch", r"\btrouve", r"\bgoogle", r"\binternet\b", r"\bweb\b",
        r"\ben ligne\b", r"\bsites?\b", r"\barticles?\b", r"\bactu", r"\bnews\b", r"\bm[ée]t[ée]o", r"\bweather\b",
        r"\bquel temps\b", r"\btemp[ée]rature", r"\bpleuv", r"\bpluie\b", r"\bneige", r"\bprix\b", r"\btarifs?\b", r"\bhoraires?\b", r"\bscores?\b", r"\bcours de\b", r"\bwikip", r"\bqui est\b",
        r"\bc'est quoi\b", r"\bqu'est-ce qu", r"\bliens?\b", r"\bsources?\b", r"\bsearch\b", r"\blook up\b",
        r"\bwhat is\b", r"\bwho is\b", r"\blatest\b", r"\baujourd'hui\b", r"\bderni[eè]re?s?\b")),
    Family("Mail", ("mail_*",), ("mail_*",), _rx(
        r"\be-?mails?\b", r"\bmails?\b", r"\bcourriels?\b", r"\bbo[iî]te\b", r"\binbox\b", r"\br[ée]pon[ds]",
        r"\bbrouillon", r"\bdraft", r"\bmessages? (re[çc]us?|non lus?)", r"\bunread\b", r"\bexp[ée]diteur",
        r"\bspam\b", r"\bnewsletter")),
    Family("Agenda", ("calendar_*",), ("calendar_*",), _rx(
        r"\bagenda\b", r"\bcalendri", r"\bcalendar\b", r"\brdv\b", r"\brendez-vous\b", r"\br[ée]union", r"\bmeeting",
        r"\b[ée]v[ée]nements?\b", r"\bevents?\b", r"\bplanifi", r"\bschedule", r"\bcr[ée]neau", r"\bdispo(nible|nibilit)",
        r"\bcale[rz]?\b", r"\bd[ée]place", r"\bannule")),
    Family("Navigateur", ("browser_*",), (
        "browser_lane", "browser_navigate", "browser_snapshot", "browser_find", "browser_read_text", "browser_click",
        "browser_type", "browser_fill_form", "browser_press_key", "browser_select_option", "browser_wait_for",
        "browser_tabs", "browser_handoff", "browser_close"), _rx(
        r"\bnavigat", r"\bbrowser\b", r"\bouvre\b", r"\bva sur\b", r"\bva voir\b", r"\bconnecte", r"\bconnexion\b",
        r"\blog ?in\b", r"\bclique", r"\bclick", r"\bformulaire", r"\bremplis", r"\binscri", r"\br[ée]serv",
        r"\bcommande", r"\bach[eè]te", r"\bcapture", r"\bscreenshot", r"\bonglets?\b", r"\btabs?\b", r"\bpage\b")),
    Family("Bulles", ("bubble_*",), ("bubble_*",), _rx(
        r"\bmontre", r"\baffiche", r"\bfais voir", r"\bbulles?\b", r"\baper[çc]u", r"\binstagram", r"\blinkedin",
        r"\bprofil\b", r"\bshow me\b")),
    Family("Sessions", ("session_*",), ("session_*",), _rx(
        r"\bsessions?\b", r"\bautre fil\b", r"\btransmets", r"\bfais passer", r"\bpr[ée]viens")),
    Family("Agents", ("agent_*",), ("agent_*",), _rx(
        r"\bagents?\b", r"\bcrew\b", r"\bautomatis", r"\btous les (jours|matins|soirs|lundis)", r"\bchaque (jour|matin|soir|semaine)",
        r"\bevery (day|morning|week)")),
    Family("Skills", ("skill_*",), ("skill_list", "skill_view"), _rx(
        r"\bskills?\b", r"\bcomp[ée]tences?\b", r"\bproc[ée]dures?\b", r"\bmode d'emploi\b",
        r"\bcomment (faire|on fait|proc[ée]der)\b", r"\bcomment faire pour\b", r"\bhow (do i|to)\b",
        r"\bplaybook\b", r"\bworkflow\b")),
    # Packs d'agents (lot X16) : compétences + modèles d'agents proposés.
    Family("Packs", ("pack_*",), ("pack_list", "pack_install"), _rx(
        r"\bpacks?\b", r"\bmod[eè]les? d'agents?\b", r"\bagent templates?\b")),
    Family("Serveurs MCP", ("mcp_server_*",), ("mcp_server_*",), _rx(
        r"\bmcp\b", r"\bserveurs? mcp\b", r"\bmcp servers?\b")),
    Family("Points de retour", ("checkpoint_*",), ("checkpoint_*",), _rx(
        r"\brollback\b", r"\broll back\b", r"\brevien[st]?\b", r"\brevenir\b", r"\ben arri[eè]re\b",
        r"\bannule", r"\bd[ée]fai[st]", r"\bcheckpoints?\b", r"\brestor", r"\brestaure", r"\bundo\b",
        r"\brevert\b", r"\bpoints? de retour\b", r"\bversion pr[ée]c[ée]dente\b")),
    Family("Sous-agents", ("delegate",), ("delegate",), _rx(
        r"\bd[ée]l[èe]gu", r"\bsous-?agents?\b", r"\ben parall[èe]le\b", r"\bfais faire\b", r"\bfaire faire\b",
        r"\bsubagents?\b", r"\bsub-agents?\b", r"\bdelegat", r"\bparallel\b", r"\bin parallel\b")),
)


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def relevant_tools(text: str, used: set[str], non_core: list[ToolSpec]) -> list[ToolSpec]:
    """Schémas à charger pour ce tour (politique `relevance`), dans l'ordre du registre (stable)."""
    wanted: set[str] = set(used)
    for fam in FAMILIES:
        if fam.match.search(text or ""):
            wanted.update(s.name for s in non_core if _matches(s.name, fam.load))
    return [s for s in non_core if s.name in wanted]


def compact_index(names: list[str]) -> str:
    """Index des outils non cœur, par famille, noms seuls (≈ 10 fois plus court que nom + description). Ne dépend
    que du registre : identique d'un tour à l'autre."""
    if not names:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for fam in FAMILIES:
        group = [n for n in names if _matches(n, fam.patterns) and n not in seen]
        if group:
            seen.update(group)
            lines.append(f"- {fam.label} : {', '.join(group)}")
    rest = [n for n in names if n not in seen]
    if rest:
        lines.append(f"- Autres : {', '.join(rest)}")
    return ("[Outils disponibles — schéma fourni quand ta demande en parle ; sinon tool_find(\"<nom ou besoin>\") "
            "le charge, ou appelle l'outil directement par son nom]\n" + "\n".join(lines) + "\n[/Outils]")


__all__ = ["FAMILIES", "Family", "compact_index", "relevant_tools"]
