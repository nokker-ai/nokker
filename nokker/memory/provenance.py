"""Provenance des épisodes et des faits de la mémoire (consolidation, rappel) — docs/PROVENANCE.md §4.4.

Le trou fermé ici : la consolidation (résumés d'épisode, faits proposés, notes de la nuit) peut ranger du texte venu
d'un TIERS (fichier joint, page web, mail, résultat d'outil). Ce texte ressortait plus tard dans `[Rappel mémoire]`,
bloc de confiance hors clôture : une injection rangée un jour devenait une « consigne » le lendemain.

Chaque épisode et chaque fait porte donc sa provenance, une chaîne :

  - `person`             : les mots de la personne (note écrite dans l'app, proposition d'un membre du foyer) ;
  - `assistant`          : dérivé des tours de la personne et des réponses de l'assistant, sans contenu de tiers ;
  - `third_party:<sous-type>` : dérivé, même en partie, d'un contenu de tiers. Sous-types : `attachment:file`,
    `attachment:folder`, `web:page`, `mail`, `tool:<nom>`, `context:injected`, `crew:event`, `unknown` ;
  - `unknown_legacy`     : rangé avant la provenance, sans trace d'un contenu de tiers. Rendu comme `person` (aucun
    changement à la bascule), gardé à part pour être reclassé plus tard.

Règles : pour un NOUVEAU fait, au moindre doute, tiers (provenance absente ou illisible → `third_party:unknown`, un
mélange → tiers dès qu'une part l'est). L'existant n'est classé tiers que si sa trace (les tours de l'épisode) montre un
résultat d'outil ou une pièce jointe ; aucun classement par modèle.

Au rappel, un fait de tiers ressort avec un marqueur de source juste avant le texte, et son texte passe par `inline()`
(une ligne, crochets et chevrons neutralisés, coupé à `recall_text_limit`). Une ligne `person`, `assistant` ou
`unknown_legacy` garde EXACTEMENT sa forme d'avant, octet pour octet."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

PERSON = "person"
ASSISTANT = "assistant"
THIRD_PARTY = "third_party"
UNKNOWN_LEGACY = "unknown_legacy"

# Sous-types de tiers.
ATTACHMENT_FILE = "attachment:file"
ATTACHMENT_FOLDER = "attachment:folder"
WEB_PAGE = "web:page"
MAIL = "mail"
CONTEXT_INJECTED = "context:injected"
CREW_EVENT = "crew:event"
UNKNOWN = "unknown"


def third_party(subtype: str) -> str:
    return f"{THIRD_PARTY}:{subtype or UNKNOWN}"


def tool(name: str) -> str:
    return third_party(f"tool:{name or 'outil'}")


THIRD_PARTY_UNKNOWN = third_party(UNKNOWN)

# ─────────────────────────────── textes vus par le modèle ───────────────────────────────
# Validés mot pour mot par la relecture des textes (06.11.2026) ; empreinte figée dans
# tests/unit/memory/test_provenance.py::test_marker_texts_fingerprint. Un seul marqueur par ligne, juste avant le texte.
# « (source non vérifiée) » : seulement un fait NOUVEAU d'origine inconnue ; `unknown_legacy` reste rendu comme personne.
MARKER_ATTACHMENT = "(tiré d'un document joint)"
MARKER_WEB = "(tiré d'une page web)"
MARKER_MAIL = "(tiré d'un mail reçu)"
MARKER_TOOL = "(tiré du résultat d'un outil)"
MARKER_UNVERIFIED = "(source non vérifiée)"  # tiers sans source précise : unknown, crew:event, context:injected
MARKERS = (MARKER_ATTACHMENT, MARKER_WEB, MARKER_MAIL, MARKER_TOOL, MARKER_UNVERIFIED)


# ─────────────────────────────── lecture d'une provenance ───────────────────────────────


def kind_of(p: str | None) -> str:
    """`person` | `assistant` | `unknown_legacy` | `third_party` ; `""` pour une provenance absente (rappel d'une
    mémoire qui n'en porte pas : rendu d'avant). Toute autre valeur non vide est illisible : tiers."""
    p = str(p or "")
    if not p:
        return ""
    if p in (PERSON, ASSISTANT, UNKNOWN_LEGACY):
        return p
    return THIRD_PARTY


def subtype_of(p: str | None) -> str:
    p = str(p or "")
    if kind_of(p) != THIRD_PARTY:
        return ""
    return p.split(":", 1)[1] if p.startswith(THIRD_PARTY + ":") and len(p) > len(THIRD_PARTY) + 1 else UNKNOWN


def is_third_party(p: str | None) -> bool:
    return kind_of(p) == THIRD_PARTY


def marker(p: str | None) -> str:
    """Marqueur de source d'un fait de tiers (`""` sinon)."""
    if not is_third_party(p):
        return ""
    sub = subtype_of(p)
    if sub.startswith("attachment"):
        return MARKER_ATTACHMENT
    if sub.startswith("web"):
        return MARKER_WEB
    if sub.startswith("mail"):
        return MARKER_MAIL
    if sub.startswith("tool:"):
        return MARKER_TOOL
    return MARKER_UNVERIFIED  # unknown, crew:event, context:injected, sous-type illisible


_RANK = {THIRD_PARTY: 3, UNKNOWN_LEGACY: 2, ASSISTANT: 1, PERSON: 0}


def combine(values: Iterable[str | None]) -> str:
    """Provenance d'un texte dérivé de plusieurs sources : tiers dès qu'une part l'est (la première rencontrée garde son
    sous-type), sinon `unknown_legacy`, sinon `assistant`, sinon `person`. Une part sans provenance (`""`, None) est un
    doute : tiers. Rien du tout : tiers (on ne sait pas d'où vient le texte)."""
    best: str | None = None
    for v in values:
        v = str(v or "") or THIRD_PARTY_UNKNOWN
        if best is None or _RANK[kind_of(v)] > _RANK[kind_of(best)]:
            best = v
    return best if best is not None else THIRD_PARTY_UNKNOWN


def for_new_fact(p: str | None) -> str:
    """Provenance d'un NOUVEAU fait : absente ou illisible → tiers ; les valeurs connues passent telles quelles."""
    p = str(p or "")
    return p if p in (PERSON, ASSISTANT, UNKNOWN_LEGACY) or p.startswith(THIRD_PARTY + ":") else THIRD_PARTY_UNKNOWN


# ─────────────────────────────── trace : les tours d'un épisode ───────────────────────────────

_BLOCK_KIND = re.compile(r'<untrusted-data kind="([^"]*)"')
_FILE_MARKS = ("[Fichier :", "[Pièces jointes]", "[Pièce jointe")
_FOLDER_MARK = "[Dossier :"
_RECALLED = ((ATTACHMENT_FILE, MARKER_ATTACHMENT), (WEB_PAGE, MARKER_WEB), (MAIL, MARKER_MAIL),
             ("tool:rappel", MARKER_TOOL), (UNKNOWN, MARKER_UNVERIFIED))


def tool_result(name: str, content: str = "", arguments: dict[str, Any] | None = None) -> str | None:
    """Provenance d'un résultat d'outil d'après le registre CaMeL (`qa/provenance.py` : `source_of`, `SOURCES`, réglages
    par défaut) ; None = source de confiance (fiable ou interne : `todo`, `memory_search`, espace de travail…), qui ne
    teinte pas l'épisode. Non fiable (web, mail, site, agenda, sous-agent, MCP, connecteur relayé, outil absent du
    registre → `outil`) : tiers. Un résultat qui porte une clôture `<untrusted-data>` (contenu de tiers clôturé par
    l'outil : fichier lu, sortie de commande, MCP, navigateur) est tiers quelle que soit la source."""
    from nokker.qa.provenance import SOURCES, ProvenanceGuard, Trust

    source = ProvenanceGuard().source_of(name, None, arguments or {})
    trust = SOURCES.get(source, SOURCES["outil"])[1]
    if trust is Trust.NON_FIABLE:
        if source == "mail":
            return third_party(MAIL)
        if source in ("web", "site"):
            return third_party(WEB_PAGE)
        return tool(name)
    if "<untrusted-data" in (content or ""):
        return tool(name)
    return None


def _block_subtype(kind: str) -> str:
    k = kind.lower()
    if k.startswith("attachment:folder"):
        return third_party(ATTACHMENT_FOLDER)
    if k.startswith("attachment") or k in ("file", "fichier"):
        return third_party(ATTACHMENT_FILE)
    if k in ("url", "web", "page") or k.startswith("web"):
        return third_party(WEB_PAGE)
    if "mail" in k:
        return third_party(MAIL)
    return third_party(CREW_EVENT)


def _attached_sources(text: str) -> list[str]:
    out = [_block_subtype(k) for k in _BLOCK_KIND.findall(text)]
    if _FOLDER_MARK in text:
        out.append(third_party(ATTACHMENT_FOLDER))
    if any(m in text for m in _FILE_MARKS):
        out.append(third_party(ATTACHMENT_FILE))
    return out


def turn_sources(turn: Any) -> list[str]:
    """Contenus de tiers d'un tour, dans l'ordre : pièces jointes (bloc du tour ou en-tête figé), blocs
    `<untrusted-data>` du message, contexte reçu d'un autre fil, faits de tiers déjà rappelés (marqueur dans l'en-tête
    figé), résultats d'outils."""
    out: list[str] = []
    attached = str(getattr(turn, "attached", "") or "")
    if attached:
        out += _attached_sources(attached) or [third_party(ATTACHMENT_FILE)]
    head = str(getattr(turn, "head", "") or "")
    if head:
        out += _attached_sources(head)
        # un fait de tiers déjà rappelé dans ce tour a pu nourrir la réponse : la teinte suit
        out += [third_party(sub) for sub, m in _RECALLED if m in head]
    inp = getattr(turn, "input", None)
    text = str(getattr(inp, "text", "") or "") if inp is not None else ""
    out += _attached_sources(text)
    if getattr(turn, "injected", None):
        out.append(third_party(CONTEXT_INJECTED))
    args: dict[str, Any] = {}
    for m in getattr(turn, "messages", None) or []:
        for tc in getattr(m, "tool_calls", None) or []:
            if isinstance(getattr(tc, "arguments", None), dict):
                args[str(getattr(tc, "id", ""))] = tc.arguments
        if getattr(m, "role", "") == "tool":
            p = tool_result(str(getattr(m, "name", "") or ""), str(getattr(m, "content", "") or ""),
                            args.get(str(getattr(m, "tool_call_id", "") or "")))
            if p is not None:
                out.append(p)
    return out


def of_turns(turns: Iterable[Any]) -> str:
    """Provenance de ce qu'on tire de ces tours (résumé d'épisode, faits) : tiers au premier contenu de tiers rencontré,
    sinon `assistant` s'il y a une réponse, sinon `person`. Aucun tour lisible : tiers (rien ne prouve l'origine)."""
    seen = False
    replied = False
    for t in turns:
        if t is None:
            continue
        seen = True
        found = turn_sources(t)
        if found:
            return found[0]
        if str(getattr(t, "reply", "") or "").strip() or any(
                getattr(m, "role", "") == "assistant" and (getattr(m, "content", "") or "").strip()
                for m in getattr(t, "messages", None) or []):
            replied = True
    if not seen:
        return THIRD_PARTY_UNKNOWN
    return ASSISTANT if replied else PERSON


# ─────────────────────────────── par fait : recouvrement lexical ───────────────────────────────
# Un épisode mêle souvent une décision de la personne et un contenu de tiers (« je choisis le vol de 7 h » après un
# `web_search`). Chaque fait extrait reçoit donc SA provenance, de façon déterministe (le modèle ne se classe jamais
# lui-même), d'après le recouvrement de ses mots significatifs avec trois sources de l'épisode :
#   (a) les messages de la personne (blocs clôturés et pièces jointes retirés) ;
#   (b) les réponses de l'assistant ;
#   (c) les contenus de tiers (pièces jointes, blocs `<untrusted-data>`, contexte reçu, résultats d'outils non fiables
#       ou clôturés — `tool_result`).
# Règle exacte (`fact_provenance`) :
#   1. moins de FACT_MIN_TOKENS mots significatifs → repli : provenance de l'épisode ;
#   2. porté par (c) (couverture ≥ CARRIED) → tiers (sous-type du contenu qui le couvre le mieux), SAUF si un seul
#      message de la personne le porte aussi (couverture ≥ CARRIED) ET l'exprime comme une décision à la première
#      personne (`DECISION`, hors blocs clôturés), message d'au plus DECISION_MAX_CHARS caractères et couvert à moins
#      de DECISION_MAX_COPY par (c) (pas une page collée) → `person` ;
#   3. porté par (a) → `person` ;
#   4. porté par une réponse de l'assistant → `assistant`, sauf si cette réponse reprenait un contenu de tiers
#      (couverture de la réponse par (c) ≥ REPLY_TAINT) → tiers ;
#   5. sinon → repli : provenance de l'épisode.
# Couverture = part des mots significatifs du fait présents dans la source. Mot significatif : minuscules NFKC, mots
# outils et mots de rôle retirés (`_STOP`), pluriel en -s/-x retiré, radical des STEM premiers caractères, nombres
# normalisés (« 07 » = « 7 »).
FACT_MIN_TOKENS = 2
CARRIED = 0.6
REPLY_TAINT = 0.5
STEM = 5
# Exception « décision » de la règle 2 : seulement un message COURT (≤ DECISION_MAX_CHARS caractères, hors blocs clôturés)
# qui n'est pas une copie du contenu de tiers (moins de DECISION_MAX_COPY de ses mots couverts par (c) : mots
# significatifs, plus les mots de la formule de décision, `_decision_not_copied`).
DECISION_MAX_CHARS = 500
DECISION_MAX_COPY = 0.8

_STOP = frozenset([
    "les", "des", "une", "uns", "dans", "pour", "par", "avec", "sans", "sur", "sous", "chez", "vers", "mais",
    "donc", "car", "que", "qui", "quoi", "dont", "elle", "elles", "ils", "lui", "leur", "leurs", "nous", "vous",
    "ton", "tes", "mes", "ses", "son", "mon", "nos", "vos", "est", "sont", "été", "être", "avoir", "ont", "avait",
    "avais", "fait", "faire", "cette", "ces", "cet", "celui", "celle", "aussi", "plus", "moins", "très", "tout",
    "tous", "toute", "toutes", "comme", "bien", "alors", "encore", "déjà", "ici", "même", "entre", "après",
    "avant", "pas", "rien", "oui", "non", "the", "and", "for", "with", "that", "this", "from", "are", "was",
    "were", "has", "have", "had", "will", "would", "can", "could", "you", "your", "our", "personne",
    "utilisateur", "utilisatrice", "user", "assistant", "assistante", "nina", "nokker"
])
_WORD = re.compile(r"\w+")
DECISION = re.compile(
    r"(?i)(?:\b(?:je|j['’]|on|nous)\s*(?:ai\s+|avons\s+|vais\s+|va\s+|veux\s+)?"
    r"(?:choisi\w*|pren\w*|pris|décid\w*|retien\w*|retenu|confirm\w*|valid\w*|veux|voudrai\w*|gard\w*|opt\w*"
    r"|préf[eè]r\w*|réserv\w*|achèt\w*|achet\w*)\b"
    r"|\b(?:va pour|ok pour|d['’]accord pour|c['’]est décidé|on part sur|i choose|i['’]ll take|let['’]s go with"
    r"|i decide|i want)\b)")


def tokens(text: str) -> set[str]:
    import unicodedata

    out: set[str] = set()
    for w in _WORD.findall(unicodedata.normalize("NFKC", str(text or "")).lower()):
        if w.isdigit():
            out.add(str(int(w)))
        elif len(w) >= 3 and w not in _STOP:
            out.add((w[:-1] if len(w) > 3 and w[-1] in "sx" else w)[:STEM])  # pluriel retiré, puis radical
    return out


def _cover(fact: set[str], src: set[str]) -> float:
    return len(fact & src) / len(fact) if fact else 0.0


def _strip_fenced(text: str) -> str:
    text = re.sub(r"<untrusted-data[^>]*>.*?</untrusted-data[^>]*>", " ", text, flags=re.DOTALL)
    return re.sub(r"\[(Fichier|Dossier|Page web) :.*?\[/(Fichier|Dossier|Page web)\]", " ", text, flags=re.DOTALL)


def _third_chunks(turn: Any) -> list[tuple[str, str]]:
    """(provenance, texte) des contenus de tiers d'un tour."""
    out: list[tuple[str, str]] = []
    attached = str(getattr(turn, "attached", "") or "")
    if attached:
        out.append(((_attached_sources(attached) or [third_party(ATTACHMENT_FILE)])[0], attached))
    head = str(getattr(turn, "head", "") or "")
    for block in re.findall(r"\[Pièces jointes\].*?\[/Pièces jointes\]", head, re.DOTALL):
        if block not in attached:  # en-tête figé d'avant `attached` (r46) : ses pièces jointes
            out.append(((_attached_sources(block) or [third_party(ATTACHMENT_FILE)])[0], block))
    for line in head.splitlines():  # fait de tiers déjà rappelé dans ce tour
        found = [third_party(sub) for sub, m in _RECALLED if m in line]
        if found:
            out.append((found[0], line))
    inp = getattr(turn, "input", None)
    text = str(getattr(inp, "text", "") or "") if inp is not None else ""
    for kind, body in re.findall(r'<untrusted-data kind="([^"]*)"[^>]*>(.*?)</untrusted-data', text, re.DOTALL):
        out.append((_block_subtype(kind), body))
    for block in getattr(turn, "injected", None) or []:
        out.append((third_party(CONTEXT_INJECTED), str(block)))
    args: dict[str, Any] = {}
    for m in getattr(turn, "messages", None) or []:
        for tc in getattr(m, "tool_calls", None) or []:
            if isinstance(getattr(tc, "arguments", None), dict):
                args[str(getattr(tc, "id", ""))] = tc.arguments
        if getattr(m, "role", "") == "tool":
            content = str(getattr(m, "content", "") or "")
            p = tool_result(str(getattr(m, "name", "") or ""), content,
                            args.get(str(getattr(m, "tool_call_id", "") or "")))
            if p is not None:
                out.append((p, content))
    return out


def _replies(turn: Any) -> list[str]:
    out = [str(getattr(m, "content", "") or "") for m in getattr(turn, "messages", None) or []
           if getattr(m, "role", "") == "assistant" and (getattr(m, "content", "") or "").strip()]
    reply = str(getattr(turn, "reply", "") or "")
    if reply.strip() and reply.strip() not in (x.strip() for x in out):
        out.append(reply)
    return out


def _decision_not_copied(text: str, c_tokens: set[str], c_raw: set[str]) -> bool:
    """Le message porte une décision à la première personne (`DECISION`) et n'est pas une copie du contenu de tiers :
    moins de DECISION_MAX_COPY de ses mots — mots significatifs, plus les mots de la formule de décision elle-même
    (« ok pour », « nous avons décidé ») — se retrouvent dans (c)."""
    m = DECISION.search(text)
    if m is None:
        return False
    words = tokens(text)
    formula = {w for w in _WORD.findall(m.group(0).lower())}
    total = len(words) + len(formula)
    covered = len(words & c_tokens) + len(formula & c_raw)
    return total > 0 and covered / total < DECISION_MAX_COPY


def fact_provenance(fact: str, turns: Iterable[Any], fallback: str | None) -> str:
    """Provenance d'UN fait tiré de ces tours (règle et seuils ci-dessus). `fallback` : celle de l'épisode."""
    fb = for_new_fact(fallback) if fallback != UNKNOWN_LEGACY else UNKNOWN_LEGACY
    f = tokens(fact)
    turns = [t for t in turns if t is not None]
    if len(f) < FACT_MIN_TOKENS or not turns:
        return fb
    persons: list[str] = []
    replies: list[str] = []
    chunks: list[tuple[str, set[str]]] = []
    raw_c: set[str] = set()
    for t in turns:
        inp = getattr(t, "input", None)
        own = _strip_fenced(str(getattr(inp, "text", "") or "") if inp is not None else "")
        if own.strip():
            persons.append(own)
        replies += _replies(t)
        third = _third_chunks(t)
        chunks += [(p, tokens(text)) for p, text in third]
        raw_c |= {w for _, text in third for w in _WORD.findall(str(text).lower())}
    all_c = set().union(*(c for _, c in chunks)) if chunks else set()

    def best_chunk(words: set[str]) -> str:
        return max(chunks, key=lambda c: _cover(words, c[1]))[0] if chunks else THIRD_PARTY_UNKNOWN

    person_tokens = set().union(*(tokens(x) for x in persons)) if persons else set()
    if chunks and _cover(f, all_c) >= CARRIED:
        decided = any(_cover(f, tokens(x)) >= CARRIED and len(x) <= DECISION_MAX_CHARS
                      and _decision_not_copied(x, all_c, raw_c) for x in persons)
        return PERSON if decided else best_chunk(f)
    if _cover(f, person_tokens) >= CARRIED:
        return PERSON
    if replies:
        rep = max(replies, key=lambda x: _cover(f, tokens(x)))
        if _cover(f, tokens(rep)) >= CARRIED:
            rt = tokens(rep)
            return best_chunk(rt) if chunks and _cover(rt, all_c) >= REPLY_TAINT else ASSISTANT
    return fb


def reclassify_legacy(current: str | None, turns: Iterable[Any]) -> str:
    """Un épisode `unknown_legacy` devient tiers seulement si sa trace le montre ; sinon il reste `unknown_legacy`.
    Toute autre provenance est rendue telle quelle."""
    if str(current or "") != UNKNOWN_LEGACY:
        return str(current or "")
    turns = [t for t in turns if t is not None]
    if not turns:
        return UNKNOWN_LEGACY
    p = of_turns(turns)
    return p if is_third_party(p) else UNKNOWN_LEGACY


# ─────────────────────────────── rappel ───────────────────────────────


def recall_line(r: Any, limit: int) -> str:
    """Une ligne du bloc `[Rappel mémoire]`. Hors tiers : la forme d'avant, inchangée. Tiers : marqueur de source juste
    avant le texte, texte (et nom de note) passés par `inline()`, texte coupé à `limit`."""
    from nokker.qa.untrusted import inline, neutralize_date, neutralize_env

    p = getattr(r, "provenance", "") or ""
    if not is_third_party(p):
        return neutralize_date(neutralize_env(f"- [{r.note}] (score {r.score:.2f}) {r.text[:limit]}"))

    return f"- [{inline(r.note)}] (score {r.score:.2f}) {marker(p)} {inline(r.text, limit)}"


def propose_facts(memory: Any, thread_id: str, facts: list[str], provenance: str) -> None:
    """`MemoryPort.propose` avec la provenance des faits. Une mémoire dont `propose` ne la prend pas (port de test,
    mémoire qui ne range rien) la reçoit sans : elle ne range rien de rappelable, ou range au défaut sûr (tiers)."""
    import inspect

    fn = memory.propose
    try:
        params = inspect.signature(fn).parameters
        takes = "provenance" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        takes = False
    if takes:
        fn(thread_id, facts, provenance=provenance)
    else:
        fn(thread_id, facts)


__all__ = [
    "ASSISTANT",
    "MARKERS",
    "MARKER_ATTACHMENT",
    "MARKER_MAIL",
    "MARKER_TOOL",
    "MARKER_UNVERIFIED",
    "MARKER_WEB",
    "PERSON",
    "THIRD_PARTY",
    "THIRD_PARTY_UNKNOWN",
    "UNKNOWN_LEGACY",
    "combine",
    "fact_provenance",
    "for_new_fact",
    "is_third_party",
    "kind_of",
    "marker",
    "of_turns",
    "propose_facts",
    "recall_line",
    "reclassify_legacy",
    "subtype_of",
    "third_party",
    "tool",
    "tool_result",
    "turn_sources",
]
