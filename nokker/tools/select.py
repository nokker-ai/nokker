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
from nokker.tools.essentials.texts import ESS_CALC, ESS_METEO, ESS_TRANSIT, ESS_WIKI
from nokker.tools.essentials.texts_v2 import ESS2_METEO, ESS2_MOBILITY, ESS2_SCHOOL, ESS2_WATER


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
    # Pack Essentiels (drapeau `essentials`, docs/ESSENTIALS.md), quatre familles : sans ces outils dans le registre
    # (drapeau éteint), elles ne produisent aucune ligne et ne chargent rien.
    Family("Météo", ESS_METEO + ESS2_METEO, ESS_METEO + ESS2_METEO, _rx(
        r"\bm[ée]t[ée]o", r"\bweather\b", r"\bwetter\b", r"\bquel temps\b", r"\btemp[ée]rature", r"\bpleuv",
        r"\bpluie\b", r"\bneige", r"\bsoleil\b", r"\borage", r"\bpollens?\b", r"\ballergi", r"\bavalanches?\b",
        r"\blawinen?", r"\bski\b", r"\brandonn[ée]e", r"\bforecast\b", r"\bvigilance\b", r"\balertes? m[ée]t[ée]o", r"\bqualit[ée] de l'air\b",
        r"\bpollution\b", r"\bindice atmo\b", r"\bair quality\b")),
    # Vague 2 (`essentials_v2`) : sans ces outils dans le registre, ces familles ne produisent rien. « vacances » seul ne
    # charge JAMAIS les vacances scolaires (contrainte relecture, 05.11.2026).
    Family("Mobilité", ESS2_MOBILITY, ESS2_MOBILITY, _rx(
        r"\bv[ée]los? (en )?(libre-service|partag)", r"\btrottinettes?\b", r"\bcar ?sharing\b", r"\bmobility\b",
        r"\bpublibike\b", r"\bdonkey\b", r"\bvoitures? (en )?partag", r"\bv[ée]hicules? partag", r"\bbornes? de recharge",
        r"\brecharger\b", r"\bvoiture [ée]lectrique", r"\bladestation", r"\bcharging stations?\b")),
    Family("Eau", ESS2_WATER, ESS2_WATER, _rx(
        r"\brivi[èe]res?\b", r"\bfleuves?\b", r"\blacs?\b", r"\bd[ée]bit\b", r"\bniveau (du|de la|de l'|des)\b",
        r"\btemp[ée]rature de l'eau\b", r"\baar[e]?\b", r"\bl[ée]man\b", r"\bbaignade", r"\bcrues?\b")),
    Family("Vacances scolaires", ESS2_SCHOOL, ESS2_SCHOOL, _rx(
        r"\bvacances scolaires\b", r"\bcong[ée]s scolaires\b", r"\bschulferien\b", r"\brel[âa]ches\b")),
    Family("Transports", ESS_TRANSIT, ESS_TRANSIT, _rx(
        r"\btrains?\b", r"\btrams?\b", r"\bbus\b", r"\bcars? postal", r"\bbateaux?\b", r"\bcff\b", r"\bsbb\b",
        r"\bgares?\b", r"\barr[êe]ts?\b", r"\btrajets?\b", r"\bitin[ée]raire", r"\bcorrespondances?\b",
        r"\bretards? (du|des|de la|d'un|d'une) (trains?|bus|trams?|bateaux?|m[ée]tro)\b", r"\btransports? publics?\b",
        r"\bfahrplan", r"\btimetable\b")),
    Family("Encyclopédie", ESS_WIKI, ESS_WIKI, _rx(
        r"\bwikip", r"\bqui est\b", r"\bqui [ée]tait\b", r"\bqu'est-ce que c'est\b", r"\bqu'est-ce qu'une?\b",
        r"\bc'est quoi (un|une|le|la)\b", r"\bwho is\b", r"\bwhat is (a|an|the)\b", r"\bencyclop")),
    Family("Calculs", ESS_CALC, ESS_CALC, _rx(
        r"\btaux de change\b", r"\bexchange rates?\b", r"\bdevises?\b", r"\ben (euros?|dollars?|francs?|chf)\b",
        r"\bconverti", r"\d+\s*(chf|eur|usd)\s*(en|in|to)\b", r"\bf[ée]ri[ée]", r"\bholidays?\b", r"\bfeiertag", r"\bconvert", r"\bcalcul",
        r"\bcombien (font|fait)\b", r"\d\s*(km|mi|kg|lb|°[cf]|ft|oz)\b", r"\bmiles?\b", r"\bfahrenheit",
        r"\bcelsius", r"\bjours? (entre|avant|apr[eè]s)\b", r"\bquel jour\b", r"\bdans \d+ jours\b")),
    Family("Mail", ("mail_*",), ("mail_*",), _rx(
        r"\be-?mails?\b", r"\bmails?\b", r"\bcourriels?\b", r"\bbo[iî]te\b", r"\binbox\b", r"\br[ée]pon[ds]",
        r"\bbrouillon", r"\bdraft", r"\bmessages? (re[çc]us?|non lus?)", r"\bunread\b", r"\bexp[ée]diteur",
        r"\bspam\b", r"\bnewsletter")),
    Family("Agenda", ("calendar_*",), ("calendar_*",), _rx(
        r"\bagenda\b", r"\bcalendri", r"\bcalendar\b", r"\brdv\b", r"\brendez-vous\b", r"\br[ée]union", r"\bmeeting",
        r"\b[ée]v[ée]nements?\b", r"\bevents?\b", r"\bplanifi", r"\bschedule", r"\bcr[ée]neau", r"\bdispo(nible|nibilit)",
        r"\bcale[rz]?\b", r"\bd[ée]place", r"\bannule")),
    # Calendrier familial (drapeau `family_calendar`, docs/FAMILY-CALENDAR.md) : sans les outils `family_calendar_*`
    # dans le registre (drapeau éteint), la famille ne produit aucune ligne et ne charge rien.
    Family("Calendrier familial", ("family_calendar_*",), ("family_calendar_*",), _rx(
        r"\bagenda\b", r"\bcalendri", r"\bcalendar\b", r"\bplannings?\b", r"\bprogrammes?\b", r"\bconvocations?\b",
        r"\bhoraires?\b", r"\bd[ée]ch[eè]terie", r"\bentra[îi]nements?\b", r"\b[ée]cole\b", r"\bclub\b",
        r"\b[ée]v[ée]nements?\b", r"\bbrouillon\b", r"\bschedule")),
    # Paiement (drapeau `payments`, docs/PAYMENTS.md) : sans l'outil `pay_request` dans le registre (drapeau éteint), la
    # famille ne produit aucune ligne et ne charge rien. Avant le navigateur : « commande » charge les deux.
    Family("Paiement", ("pay_request",), ("pay_request",), _rx(
        r"\bpaie", r"\bpaye", r"\bpayer\b", r"\bpaiements?\b", r"\bcommande", r"\bach[eè]te", r"\bcommander\b",
        r"\bbestell", r"\bkauf", r"\bbezahl", r"\bordina", r"\bcompra", r"\bpaga",
        r"\border\b", r"\bbuy\b", r"\bpurchase", r"\bpay\b", r"\bcheckout\b")),
    Family("Navigateur", ("browser_*",), (
        "browser_lane", "browser_navigate", "browser_snapshot", "browser_find", "browser_read_text", "browser_click",
        "browser_type", "browser_fill_form", "browser_press_key", "browser_select_option", "browser_wait_for",
        "browser_tabs", "browser_handoff", "browser_close"), _rx(
        r"\bnavigat", r"\bbrowser\b", r"\bouvre\b", r"\bva sur\b", r"\bva voir\b", r"\bconnecte", r"\bconnexion\b",
        r"\blog ?in\b", r"\bclique", r"\bclick", r"\bformulaire", r"\bremplis", r"\binscri", r"\br[ée]serv",
        r"\bcommande", r"\bach[eè]te", r"\bcapture", r"\bscreenshot", r"\bonglets?\b", r"\btabs?\b", r"\bpage\b")),
    # Papiers du foyer (drapeau `household_paper`, docs/HOUSEHOLD-PAPER.md) : sans les outils `paper_*` dans le
    # registre (drapeau éteint), la famille ne produit aucune ligne et ne charge rien.
    Family("Papiers", ("paper_*",), ("paper_*",), _rx(
        r"\bpapiers?\b", r"\bcourriers?\b", r"\blettres?\b", r"\bfactures?\b", r"\bqr\b", r"\bpaie", r"\bpaye",
        r"\bpaiements?\b", r"\bvirements?\b", r"\bpain\.?001\b", r"\bimp[oô]ts?\b", r"\bfiscal", r"\brelev[ée]s?\b",
        r"\b[ée]ch[ée]ances?\b", r"\bprogramme\b", r"\b[ée]cole\b", r"\bclub\b", r"\bscann?", r"\bdocuments?\b",
        r"\bbills?\b", r"\binvoices?\b", r"\btax\b")),
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
    Family("Packs", ("pack_*",), ("pack_list", "pack_install", "pack_remove"), _rx(
        r"\bpacks?\b", r"\bmod[eè]les? d'agents?\b", r"\bagent templates?\b")),
    Family("Serveurs MCP", ("mcp_server_*",), ("mcp_server_*",), _rx(
        r"\bmcp\b", r"\bserveurs? mcp\b", r"\bmcp servers?\b")),
    Family("Points de retour", ("checkpoint_*",), ("checkpoint_*",), _rx(
        r"\brollback\b", r"\broll back\b", r"\brevien[st]?\b", r"\brevenir\b", r"\ben arri[eè]re\b",
        r"\bannule", r"\bd[ée]fai[st]", r"\bcheckpoints?\b", r"\brestor", r"\brestaure", r"\bundo\b",
        r"\brevert\b", r"\bpoints? de retour\b", r"\bversion pr[ée]c[ée]dente\b")),
    Family("Sous-agents", ("delegate", "delegate_*"), ("delegate", "delegate_*"), _rx(
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
