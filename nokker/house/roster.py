"""Agents de la salle Maison et leurs réglages (docs/HOUSE-ROOM.md §2).

Six agents par défaut, chacun avec son rôle, son palier de modèle, les profils du foyer qui peuvent lui parler et
l'audience de ses messages dans la salle :

| slug | nom | rôle | outils (fournisseur) |
|---|---|---|---|
| `intendant` | Intendant | répond sans mention, oriente, fait la synthèse | `consulter` (intégré) |
| `maison` | Maison | domotique | chantier p2/home-assistant |
| `courrier` | Courrier | courrier papier du foyer | chantier p2/household-paper |
| `veille` | Veille | caméras, réseau, NAS | chantier p2/home-watch |
| `agenda` | Agenda | agenda commun | fournisseur à brancher |
| `memoire` | Mémoire | mémoire commune, rapport de la consolidation | `memoire_foyer` (intégré) + p2/cx-consolidate |

Un agent dont aucun fournisseur d'outils n'est branché est listé « indisponible » : une mention lui répond par une
phrase fixe, sans appel modèle. Les textes ci-dessous (`PROMPTS`, `COMMON_RULES`) sont VUS PAR LE MODÈLE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from nokker.core.turn import Tier
from nokker.household.model import PROFILES

INTENDANT = "intendant"
SLUGS = ("intendant", "maison", "courrier", "veille", "agenda", "memoire")
AUDIENCES = ("all", "adults")
# Droits d'un membre sur les outils de la maison, du plus faible au plus fort : `read` (lectures), `act` (écritures
# ordinaires, carte standard selon le mode), `strong` (action sensible : carte FORTE individuelle).
RIGHTS = ("none", "read", "act", "strong")
DEFAULT_RIGHTS = {"adult": "strong", "teen": "act", "child": "read"}


@dataclass(frozen=True)
class AgentDef:
    slug: str
    name: str
    role: str
    tier: Tier = Tier.SHIP
    profiles: tuple[str, ...] = PROFILES  # profils du foyer qui peuvent lui parler (mention, consultation, ask_house)
    audience: str = "all"  # `adults` : ses messages ne sont jamais montrés à un profil mineur
    enabled: bool = True


PROMPTS: dict[str, str] = {
    "intendant": (
        "Tu es l'intendant de la salle Maison : tu réponds quand personne n'est mentionné. Si la demande relève d'un "
        "agent de la maison, consulte-le avec l'outil consulter (un agent par appel, trois consultations au plus). "
        "Ta réponse rapporte seulement ce que les agents consultés ont dit, résumé : n'ajoute aucun fait, aucune "
        "conclusion, aucune absence qu'ils n'ont pas affirmée (pas de « rien d'autre », « aucune autre alerte », "
        "« tout va bien » s'ils ne l'ont pas dit), et ne complète ni une date ni un montant. Si aucun agent ne "
        "convient, ou si l'agent est indisponible, dis-le simplement. Tu n'as aucun outil de la maison toi-même : ne promets aucune "
        "action qu'un agent n'a pas confirmée."),
    "maison": (
        "Tu es l'agent « Maison » de la salle Maison : la domotique du foyer (lumières, volets, chauffage, médias, "
        "serrures). Tu agis seulement par tes outils, et seulement à la demande de la personne qui parle ; ses droits "
        "sont vérifiés à chaque appel. Une action sensible attend une carte : dis-le sans insister."),
    "courrier": (
        "Tu es l'agent « Courrier » de la salle Maison : le courrier papier et administratif du foyer (factures, "
        "échéances, lettres reçues). Tu ne parles que du courrier commun que tes outils te rendent, jamais du courrier "
        "personnel d'un membre."),
    "veille": (
        "Tu es l'agent « Veille » de la salle Maison : caméras, réseau et stockage du foyer. Tu rapportes ce que tes "
        "outils constatent (incidents, appareils, état du stockage), sans dramatiser ni inventer, et tu ne décris "
        "personne au-delà de ce que dit l'incident."),
    "agenda": (
        "Tu es l'agent « Agenda » de la salle Maison : l'agenda commun du foyer (rendez-vous, activités, échéances "
        "partagées). Tu lis et tu proposes ; un ajout ou un changement passe par tes outils et peut attendre une "
        "carte."),
    "memoire": (
        "Tu es l'agent « Mémoire » de la salle Maison : la mémoire commune du foyer et le rapport de sa consolidation "
        "nocturne. Tu cites les notes du foyer que tes outils rendent, jamais une mémoire personnelle d'un membre."),
}

COMMON_RULES = (
    "Règles de la salle : plusieurs membres du foyer lisent ce que tu écris ; réponds en français, court, à la personne "
    "qui parle. N'y recopie jamais une information personnelle d'un membre qu'il n'a pas écrite lui-même dans la salle. "
    "Un fait sur la maison (état, incident, courrier, rendez-vous) ne vient que de ce que tes outils viennent de rendre "
    "ou de ce qu'un agent a écrit dans la salle ; sinon, dis que tu ne sais pas.")

UNAVAILABLE = "L'agent « {name} » n'est pas encore branché dans cette maison : il ne peut pas répondre pour l'instant."

DEFAULT_ROSTER: dict[str, AgentDef] = {
    "intendant": AgentDef("intendant", "Intendant", "oriente vers le bon agent et fait la synthèse"),
    "maison": AgentDef("maison", "Maison", "domotique : lumières, volets, chauffage, médias, serrures"),
    "courrier": AgentDef("courrier", "Courrier", "courrier papier et administratif du foyer",
                         profiles=("adult",), audience="adults"),
    "veille": AgentDef("veille", "Veille", "caméras, réseau et stockage du foyer", profiles=("adult", "teen")),
    "agenda": AgentDef("agenda", "Agenda", "agenda commun du foyer"),
    "memoire": AgentDef("memoire", "Mémoire", "mémoire commune du foyer et rapport de la consolidation nocturne",
                        tier=Tier.SHIP),
}

# @mention en tête de message ou n'importe où (« @veille », « @Mémoire ») ; la première mention connue gagne.
_MENTION = re.compile(r"(?<![\w@])@([A-Za-zÀ-ÿ_-]{2,24})")
_FOLD = str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")


def fold(s: str) -> str:
    return s.lower().translate(_FOLD)


@dataclass(frozen=True)
class Settings:
    agents: dict[str, AgentDef] = field(default_factory=lambda: dict(DEFAULT_ROSTER))
    rights: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_RIGHTS))
    transcript_messages: int = 20  # messages récents de la salle donnés à l'agent (donnée non fiable)
    transcript_chars: int = 6000
    max_consults: int = 3  # consultations de l'intendant par tour
    max_text: int = 2000  # message d'un membre (caractères)
    page: int = 100  # messages par page (GET /v1/house/messages)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> Settings:
        """`house_room_settings` : {agents: {<slug>: {tier, profiles, enabled}}, rights: {adult, teen, child},
        transcript_messages, max_consults, max_text}. Un réglage inconnu est refusé au démarrage."""
        raw = dict(raw or {})
        unknown = set(raw) - {"agents", "rights", "transcript_messages", "transcript_chars", "max_consults",
                              "max_text", "page"}
        if unknown:
            raise ValueError(f"house_room_settings : clés inconnues {sorted(unknown)}")
        agents = dict(DEFAULT_ROSTER)
        for slug, over in (raw.get("agents") or {}).items():
            if slug not in agents or not isinstance(over, dict):
                raise ValueError(f"house_room_settings.agents.{slug} : agent inconnu ({' | '.join(SLUGS)})")
            a = agents[slug]
            if "tier" in over:
                a = replace(a, tier=Tier(str(over["tier"])))
            if "profiles" in over:
                profs = tuple(str(p) for p in over["profiles"] or ())
                if any(p not in PROFILES for p in profs):
                    raise ValueError(f"house_room_settings.agents.{slug}.profiles : {' | '.join(PROFILES)}")
                a = replace(a, profiles=profs)
            if "enabled" in over:
                if slug == INTENDANT and not over["enabled"]:
                    raise ValueError("house_room_settings : l'intendant ne se désactive pas")
                a = replace(a, enabled=bool(over["enabled"]))
            agents[slug] = a
        rights = dict(DEFAULT_RIGHTS)
        for prof, level in (raw.get("rights") or {}).items():
            if prof not in PROFILES or level not in RIGHTS:
                raise ValueError(f"house_room_settings.rights.{prof} : {' | '.join(RIGHTS)}")
            rights[prof] = level
        if rights.get("teen") == "strong" or rights.get("child") in ("act", "strong"):
            raise ValueError("house_room_settings.rights : un profil mineur n'a jamais de carte forte (teen ≤ act, "
                             "child ≤ read)")
        n = {k: int(raw.get(k, getattr(cls, k))) for k in ("transcript_messages", "transcript_chars", "max_consults",
                                                         "max_text", "page")}
        return cls(agents=agents, rights=rights, **n)


def mention_of(text: str, agents: dict[str, AgentDef]) -> str | None:
    """Slug du premier agent mentionné (`@veille`, `@Mémoire`, `@memoire`), ou None."""
    names = {fold(a.slug): a.slug for a in agents.values()} | {fold(a.name): a.slug for a in agents.values()}
    for m in _MENTION.finditer(text or ""):
        slug = names.get(fold(m.group(1)))
        if slug is not None:
            return slug
    return None


__all__ = ["AUDIENCES", "COMMON_RULES", "DEFAULT_RIGHTS", "DEFAULT_ROSTER", "INTENDANT", "PROMPTS", "RIGHTS", "SLUGS",
           "UNAVAILABLE", "AgentDef", "Settings", "fold", "mention_of"]
