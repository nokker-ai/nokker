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
import unicodedata
from dataclasses import dataclass

from nokker.core import model_texts as mt
from nokker.core.ports import ToolSpec
from nokker.tools.essentials.texts import ESS_CALC, ESS_METEO, ESS_TRANSIT, ESS_WIKI
from nokker.tools.essentials.texts_v2 import ESS2_METEO, ESS2_MOBILITY, ESS2_SCHOOL, ESS2_WATER
from nokker.jobs.texts_v2 import FAMILY_LABEL as JOBS_LABEL
from nokker.jobs.texts_v2 import TRIGGERS as JOBS_TRIGGERS


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
    # Projets du foyer (drapeau `family_projects`, docs/FAMILY-PROJECTS.md) : sans les outils `family_project_*` dans le
    # registre (drapeau éteint), la famille ne produit aucune ligne et ne charge rien.
    Family("Projets du foyer", ("family_project_*",), ("family_project_*",), _rx(
        r"\bprojets?\b", r"\bt[âa]ches?\b", r"\banniversaire", r"\bvacances\b", r"\bd[ée]m[ée]nag", r"\brentr[ée]e\b",
        r"\btravaux\b", r"\bimp[ôo]ts?\b", r"\bjalons?\b", r"\b[ée]ch[ée]ances?\b", r"\brelance", r"\bcoche",
        r"\ben retard\b", r"\bpoint (de la )?semaine\b", r"\bqui fait quoi\b")),
    # Paiement (drapeau `payments`, docs/PAYMENTS.md) : sans l'outil `pay_request` dans le registre (drapeau éteint), la
    # famille ne produit aucune ligne et ne charge rien. Avant le navigateur : « commande » charge les deux.
    Family("Paiement", ("pay_request",), ("pay_request",), _rx(
        r"\bpaie", r"\bpaye", r"\bpayer\b", r"\bpaiements?\b", r"\bcommande", r"\bach[eè]te", r"\bcommander\b",
        r"\bbestell", r"\bkauf", r"\bbezahl", r"\bordina", r"\bcompra", r"\bpaga",
        r"\border\b", r"\bbuy\b", r"\bpurchase", r"\bpay\b", r"\bcheckout\b")),
    Family("Navigateur", ("browser_*",), (
        "browser_lane", "browser_navigate", "browser_snapshot", "browser_find", "browser_read_text", "browser_click",
        "browser_type", "browser_fill_form", "browser_press_key", "browser_select_option", "browser_wait_for",
        "browser_tabs", "browser_handoff", "browser_close", "browser_point"), _rx(
        r"\bnavigat", r"\bd[ée]sign", r"\bpointe", r"\bbrowser\b", r"\bouvre\b", r"\bva sur\b", r"\bva voir\b", r"\bconnecte", r"\bconnexion\b",
        r"\blog ?in\b", r"\bclique", r"\bclick", r"\bformulaire", r"\bremplis", r"\binscri", r"\br[ée]serv",
        r"\bcommande", r"\bach[eè]te", r"\bcapture", r"\bscreenshot", r"\bonglets?\b", r"\btabs?\b", r"\bpage\b")),
    # Papiers du foyer (drapeau `household_paper`, docs/HOUSEHOLD-PAPER.md) : sans les outils `paper_*` dans le
    # registre (drapeau éteint), la famille ne produit aucune ligne et ne charge rien.
    Family("Papiers", ("paper_*",), ("paper_*",), _rx(
        r"\bpapiers?\b", r"\bcourriers?\b", r"\blettres?\b", r"\bfactures?\b", r"\bqr\b", r"\bpaie", r"\bpaye",
        r"\bpaiements?\b", r"\bvirements?\b", r"\bpain\.?001\b", r"\bimp[oô]ts?\b", r"\bfiscal", r"\brelev[ée]s?\b",
        r"\b[ée]ch[ée]ances?\b", r"\bprogramme\b", r"\b[ée]cole\b", r"\bclub\b", r"\bscann?", r"\bdocuments?\b",
        r"\bbills?\b", r"\binvoices?\b", r"\btax\b")),
    # Écrans du mur (docs/SCREENS.md, V7, `ambient.screens.enabled`) : « le mur », « un écran pour le mur ».
    Family("Écrans du mur", ("screen_draft",), ("screen_draft",), _rx(
        r"\bmur\b", r"\bmurale?\b", r"\bwall\b",
        # « fais-moi un écran avec… » sans le mot mur ; jamais « écran » seul (capture d'écran, temps d'écran…)
        r"\b(fais|faire|compose|crée|créer|prépare|affiche)\b[^.?!]{0,30}\bécran\b",
        r"\bécran\b[^.?!]{0,20}\b(du foyer|de la (cuisine|maison)|familial)\b",
        r"\b(make|build|create|show)\b[^.?!]{0,30}\bscreen\b")),
    Family("Bulles", ("bubble_*",), ("bubble_*",), _rx(
        r"\bmontre", r"\baffiche", r"\bfais voir", r"\bbulles?\b", r"\baper[çc]u",
        r"\bprofil\b", r"\bshow me\b")),
    # Sites connectés (docs/CONNECTED-SITES.md §8.7, 05.11) : le nom d'un site suffit ; « mes notifications »,
    # « mes messages », « publier », « inviter » jamais seuls ; « X » seul jamais. `instagram`/`linkedin` ont quitté
    # les Bulles. Sans site connecté, seul `sites_connect` est au registre.
    Family("Sites connectés", ("sites_*",), ("sites_*",), _rx(
        r"\blinked\s?in\b", r"\binstagram\b", r"\binsta\b", r"\bfacebook\b", r"\bx\.com\b", r"\btwitter\b",
        r"\btweets?\b", r"\bpublier sur\b", r"\binviter sur linked\s?in\b", r"\bsites? connect[ée]s?\b")),
    Family("Sessions", ("session_*",), ("session_*",), _rx(
        r"\bsessions?\b", r"\bautre fil\b", r"\btransmets", r"\bfais passer", r"\bpr[ée]viens")),
    Family("Agents", ("agent_*",), ("agent_*",), _rx(
        r"\bagents?\b", r"\bcrew\b", r"\bautomatis", r"\btous les (jours|matins|soirs|lundis)", r"\bchaque (jour|matin|soir|semaine)",
        r"\bevery (day|morning|week)")),
    # Pack Emploi (drapeau `jobs_pack`, docs/JOBS-PACK.md §5.9, déclencheurs figés) : sans outil jobs_* au registre,
    # aucune ligne, rien n'est chargé.
    # Apostrophe droite ou typographique (« offre d’emploi », saisie mobile) : la liste figée garde la droite.
    Family(JOBS_LABEL, ("jobs_*",), ("jobs_*",), _rx(*(r"(?<!\w)" + re.escape(w).replace("'", "['’]") + r"(?!\w)"
                                                     for w in JOBS_TRIGGERS))),
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


# Outils hors famille (serveurs MCP ajoutés par l'utilisateur) : l'index ne donne que leur nom, donc le modèle ne
# les trouvait que si la demande les nommait (canari, 11.11.2026 : « envoie-moi le film promo » sans video_envoyer).
# Rapprochement lexical entre la demande et le nom + la description de l'outil, sans appel modèle.
_LEX_WORD = re.compile(r"[a-z0-9]{4,}")
_LEX_STOP = frozenset(
    "avec pour dans cett sans plus tout tous comm quan quel mais donc alor aussi auss votr notr leur sont etre fair "
    "fais fait peux peut veux veut doit dois merc bonj stp svp that this with from have what when your will woul "
    "plea than them they into mois moi".split())
LEXICAL_MIN = 2  # mots communs (racine de 4 lettres) exigés
LEXICAL_MAX = 4  # schémas chargés au plus par ce chemin


def _stems(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return {w[:4] for w in _LEX_WORD.findall(folded)} - _LEX_STOP


def _lexical(text: str, non_core: list[ToolSpec]) -> list[str]:
    words = _stems(text)
    if len(words) < LEXICAL_MIN:
        return []
    scored: list[tuple[int, int, str]] = []
    for i, s in enumerate(non_core):
        if any(_matches(s.name, fam.patterns) for fam in FAMILIES):
            continue
        n = len(words & _stems(s.name.replace("_", " ") + " " + (s.description or "")))
        if n >= LEXICAL_MIN:
            scored.append((-n, i, s.name))
    return [name for _, _, name in sorted(scored)[:LEXICAL_MAX]]


def relevant_tools(text: str, used: set[str], non_core: list[ToolSpec]) -> list[ToolSpec]:
    """Schémas à charger pour ce tour (politique `relevance`), dans l'ordre du registre (stable)."""
    wanted: set[str] = set(used)
    for fam in FAMILIES:
        if fam.match.search(text or ""):
            wanted.update(s.name for s in non_core if _matches(s.name, fam.load))
    wanted.update(_lexical(text or "", non_core))
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
            lines.append(f"- {mt.pick(mt.FAMILY_LABELS[fam.label]) if fam.label in mt.FAMILY_LABELS else fam.label} : {', '.join(group)}")
    rest = [n for n in names if n not in seen]
    if rest:
        lines.append(f"- {mt.pick(mt.TOOL_INDEX_OTHER)} : {', '.join(rest)}")
    return mt.pick(mt.TOOL_INDEX).format(lines="\n".join(lines))


def unavailable_line(servers: list[str]) -> str:
    """UNE ligne vue du modèle quand un ou des serveurs d'outils (MCP) ne répondent pas pour ce tour."""
    if len(servers) == 1:
        return mt.pick(mt.UNAVAILABLE_ONE).format(server=servers[0])
    quote = {"fr": "« {} »", "en": "\"{}\"", "de": "«{}»", "it": "«{}»"}[mt.current_lang()]
    names = ", ".join(quote.format(n) for n in servers)
    return mt.pick(mt.UNAVAILABLE_MANY).format(servers=names)


__all__ = ["FAMILIES", "Family", "compact_index", "relevant_tools", "unavailable_line"]
