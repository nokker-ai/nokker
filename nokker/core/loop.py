"""Boucle d'agent du noyau — l'ordre du tour de PLAN-P1 §2.3, et rien d'autre.

La boucle ne prend aucune décision de fond : elle appelle les ports dans l'ordre et applique la décision
la plus sévère. La sûreté est dans `Invariants` + `Gates` (pilier b), le choix du palier dans `Policy`
(pilier c), l'assemblage dans `ContextEngine` (pilier a). Issue de harness.py (banc P0 : 18/14/17 sur 20).

Outils natifs du noyau (jamais délégués à une source) :
  - `escalate_to_human` : exécuté par l'Escalator, marque le tour escaladé ;
  - `tool_find`         : charge des outils non cœur pour la suite du tour (schémas à la demande).
"""
from __future__ import annotations

from contextlib import ExitStack
from contextvars import ContextVar
import hashlib
import inspect
import json
import logging
import re
import ssl
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpcore
import httpx

from nokker.approvals.crypto import args_sha256
from nokker.approvals.rule_labels import (
    current_lang,
    installation_lang,
    lang_scope,
    norm_lang,
    request_lang,
    resolve_lang,
    rule_label,
)
from nokker.byok.context import active as byok_active
from nokker.byok.context import turn_scope as byok_turn_scope
from nokker.context.engine import _MONTHS
from nokker.context.turn_model import serving_model, turn_model_scope
from nokker.core import action_claims, note_claims, session_opts, tool_loop_guard, tool_name_scrub
from nokker.core import model_texts as mt
from nokker.core import person_texts as ptx
from nokker.core.model_served import clean_served, same_model
from nokker.core.approval_repeat import (
    APPROVAL_REPEAT_MAX,
    APPROVAL_REPEAT_RULE,
    ApprovalRepeat,
    safe_name,
)
from nokker.core.controls import STEER_MAX_CHARS, Steer, TurnControls
from nokker.core.deferred_args import deferred_args_error
from nokker.core.idforms import INTERNAL_ID
from nokker.core.lang_detect import detect_lang
from nokker.core.live_input import LiveToolInputs
from nokker.core.ports import (
    Approvals,
    ContextEngine,
    Escalator,
    Gates,
    Invariants,
    Ledger,
    MemoryPort,
    Policy,
    Provider,
    Recall,
    RouteDecision,
    RouteState,
    Store,
    StreamInterrupted,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    TurnObserver,
    TurnStream,
    call_effect,
)
from nokker.core.quota_error import QUOTA_STATUSES, quota_in_chain, quota_of
from nokker.core.refusal import refused_text
from nokker.core.stream import DEFAULT_HOLDBACK, ScrubbedDeltas, holdback_for
from nokker.core.tool_loop_guard import ToolLoopGuard
from nokker.core.turn import (
    APPROVAL_CONTINUE,
    Decision,
    Effect,
    Gate,
    ImageRef,
    InputCheck,
    LLMCall,
    Message,
    Tier,
    ToolCall,
    ToolResult,
    Turn,
    TurnInput,
    Verdict,
    strip_thinking,
)
from nokker.core.turn_lock import TurnLocks
from nokker.images.render import has_images, materialize
from nokker.ledger.attribution import merge_tags, spend_tag
from nokker.redact.guard import RULE as MASKED_VALUE
from nokker.redact.guard import refusal as masked_value_refusal
from nokker.tenancy.context import submit_in_context, tenant_of_thread
from nokker.memory import session_mode as _session_mode
from nokker.tools.select import relevant_tools

if TYPE_CHECKING:
    from nokker.tenancy.limits import TenantLimits

ESCALATE_TOOL = "escalate_to_human"
# Canal d'un appel d'outil fait par le propriétaire, hors tour (POST /v1/local/tools/call, docs/LOKKAL.md §7.4.4) : sa
# carte éventuelle ne relance pas la boucle après décision.
DIRECT_CHANNEL = "local-direct"
NOTE_CHARS = 600  # début du résultat repris dans la ligne « [hors tour] » du tour suivant
FIND_TOOL = "tool_find"
MEMORY_TOOLS = frozenset({"memory_search", "memory_get"})
CLARIFY_TOOL = "clarify"
# Ajoutée au dernier appel quand la limite d'appels est atteinte (jamais gardée dans l'historique).
# Fin de tâche sur tests verts (`conclude_on_green`) : la note du dernier appel ne parle pas d'une limite atteinte.
GREEN_WRAP_UP_NOTE = mt.GREEN_WRAP_UP["fr"]  # figé ; autres langues : `model_texts`
WRITE_TOOLS = frozenset({"write_file", "patch"})  # outils qui écrivent dans le dossier (règle `conclude_on_green`)
WRAP_UP_NOTE = mt.WRAP_UP["fr"]
BUDGET_NOTE = mt.BUDGET["fr"]
# Pause et steer (docs/CLIENT-API.md §Pause et steer), derrière `Kernel.steer_enabled` : messages `user` ajoutés au tour
# au point de contrôle qui précède un appel modèle, gardés dans l'historique (format : docs/CONTEXT-FORMAT.md).
STEER_BLOCK = mt.STEER["fr"]
RESUME_NOTE = mt.RESUME["fr"]
STALL_NOTE = mt.STALL["fr"]

_byok_leases: ContextVar[ExitStack | None] = ContextVar("nokker_byok_leases", default=None)

logger = logging.getLogger("nokker.core.loop")

# Rappel du tour lancé en parallèle de la préparation du contexte (20.10.2026) : mesuré sur le canari, le MCP
# `sokkan-memory` répond en 1,4-1,5 s à chaud (dont ~1,1 s de reranker), 2,1 s session froide, pics à 8 s.
_RECALL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nokker-recall")


ANNOUNCE_MAX_CHARS = 4000
ANNOUNCE_MAX_PENDING = 50
# Texte vu du modèle (au tour suivant) : un événement annoncé par l'application hôte, sans tour déclenché.
ANNOUNCE_PREFIX = mt.ANNOUNCE["fr"]  # figé ; autres langues : `model_texts` (langue du fil)


class TurnCancelled(Exception):
    """Annulation reçue PENDANT un appel modèle en flux (D6, e2e du 1.11) : levée par le relais des morceaux
    (`Kernel._complete`) au premier morceau qui arrive après `cancel()`. Elle remonte à travers le provider — qui sort
    de son `with client.stream(...)`, donc ferme la réponse HTTP de l'amont — jusqu'à la boucle, qui finit le tour sur
    « Annulé à ta demande. ». Jamais prise pour une panne d'amont (pas de repli de palier, pas d'escalade)."""


CANCEL_REPLY = ptx.CANCEL["fr"]  # figé ; autres langues : `person_texts`
# Résultat d'un appel mis en attente d'une carte (les deux chemins de `_tool_step`, avec ou sans id).
PENDING_MARK = mt.PENDING_BARE["fr"].split(" (")[0]  # français ; toutes les langues : `PENDING_MARKS`
PENDING_MARKS = mt.marks(mt.PENDING_BARE, head=" (")
# Réponse du tour (lue par la personne, jamais par le modèle) quand un 2e appel du même outil part en carte dans le même tour.
# `{what}` = `human_tool(name)` : jamais le nom brut de l'outil (lot C7). La règle bloque une 2e carte du MÊME OUTIL dans le tour.
PENDING_TURN_END = ptx.PENDING_END["fr"]  # figé (empreinte testée) ; autres langues : `person_texts`
# Libellés lisibles pour la personne ; tout autre nom d'outil (MCP, plugin, inconnu) n'est jamais montré : repli `HUMAN_TOOL_FALLBACK`.
# Table fermée de noms exacts (jamais de préfixe : un outil MCP tiers « mail_… » ne doit pas être capté). Mot de l'UI française : « e-mail ».
HUMAN_TOOLS = {"terminal": "terminal", "desktop_act": "bureau", "write_file": "fichier", "patch": "fichier",
               **dict.fromkeys(("mail_search", "mail_read", "mail_draft", "mail_move", "mail_trash", "mail_send"), "e-mail")}
HUMAN_TOOL_FALLBACK = "action non nommée"


def human_tool(name: str, lang: str | None = None) -> str:
    """Libellé d'un outil pour la personne : table fermée, repli neutre (jamais le nom brut, souvent MCP ou à guillemets).
    Sans `lang` : français (texte d'origine, aussi lu par le relecteur automatique) ; `lang` : langue du tour (`person_texts`)."""
    if lang is None:
        return HUMAN_TOOLS.get(name, HUMAN_TOOL_FALLBACK)
    return ptx.pick(ptx.HUMAN_TOOL[ptx.HUMAN_TOOL_KIND.get(name, "none")], lang)


# Suite d'une carte (APPROVAL_CONTINUE) exclue : elle suit TOUJOURS une décision de la personne sur la carte ; les suites de tâche du téléphone
# et de pas du bureau, elles, partent d'un événement du nœud.
FOLLOW_CONTROLS = frozenset({"phone_continue", "desktop_continue"})
FOLLOW_CHAIN_REPLY = ptx.FOLLOW_CHAIN["fr"]  # figé ; autres langues : `person_texts`
APPROVAL_ID_IN_RESULT = re.compile(r"\[approval:([^\]\s]+)\]")
PENDING_SKIPPED = mt.PENDING_SKIPPED["fr"]
# Détecteur inter-tours de cartes identiques : texte de la personne dans `core/approval_repeat.py`.
# Formule sans inférence Nokker (`byok`) sans point d'accès utilisable (docs/CLOUD-BYOK.md §3) : tour refusé.
BYOK_NOT_CONFIGURED_SHORT = "aucun modèle configuré pour ta formule BYOK"
BYOK_NOT_CONFIGURED_REPLY = ptx.BYOK_NOT_CONFIGURED["fr"]  # figé (FR puis EN) ; autres langues : `person_texts`
# Tour dont le modèle choisi pour CE fil (Lokkal) ne répond pas : mêmes règles (pas de repli), mais la personne n'a pas de « Mon compte ».
LANG_LOOKBACK = 40  # tours relus pour retrouver la dernière langue reconnue d'un fil

THREAD_MODEL_DOWN_REPLY = ptx.THREAD_MODEL_DOWN["fr"]  # figé (empreinte testée) ; autres langues : `person_texts`
# Tour BYOK dont le point d'accès du client ne répond pas (docs/CLOUD-BYOK.md §5) : pas de repli vers le Router.
# Gabarit PAR CLASSE de panne (`_FAILURE_LABELS`) : début commun, phrase d'action propre à la classe (FR, EN).
BYOK_DOWN_HEAD = ptx.BYOK_HEAD["fr"]  # figé ; autres langues : `person_texts`
BYOK_ACTIONS = {cls: (ptx.BYOK_ACTION[k]["fr"], ptx.BYOK_ACTION[k]["en"]) for cls, k in ptx.BYOK_ACTION_OF.items()}
# Gabarit de base (classe « erreur inattendue ») conservé pour les importeurs : `{name}`, `{why}`, `{why_en}`.
BYOK_DOWN_REPLY = ("Ton modèle « {name} » ne répond pas ({why}). Vérifie le point d'accès et la clé dans Mon compte → "
                   "Modèles, ou repasse sur le modèle de Nokker.\n\n"
                   "Your model \"{name}\" is not responding ({why_en}). Check the endpoint and key in My account → Models, "
                   "or switch back to Nokker's model.")

# Panne d'amont côté PERSONNE (escalade et message d'erreur du tour) : le libellé figé de `person_failure_label`, jamais
# le palier, l'adresse ni le texte de l'exception (ceux-ci restent dans `turn.failure["message"]` et les verdicts).
PERSON_REASON_DOWN = ptx.REASON_DOWN["fr"]  # figés (empreintes testées) ; les autres langues : `person_texts`
PERSON_ERROR_DOWN = ptx.ERROR_DOWN["fr"]
PERSON_ERROR_NEUTRAL = ptx.ERROR_NEUTRAL["fr"]
# Échecs dont le `message` est déjà une phrase écrite pour la personne (constante sans brut).
_PERSON_SAFE_CODES = frozenset({"no_model", "byok_not_configured"})


def failure_person_text(failure: dict | None, lang: str | None = None) -> str:
    """Texte d'un `turn.failure` pour la personne : `person_message` (panne de modèle, déjà dans la langue du tour), la
    phrase déjà écrite pour elle (codes sûrs), sinon le libellé neutre. Jamais le `message` brut (outil, budget,
    exception). `lang` : langue du tour (sinon celle du contexte, sinon fr)."""
    if not failure:
        return ""
    if failure.get("person_message"):
        return str(failure["person_message"])
    if failure.get("code") in _PERSON_SAFE_CODES and failure.get("message"):
        msg = str(failure["message"])
        known = ptx.SAFE_MESSAGES.get(msg)
        return ptx.pick(known, lang) if known is not None else msg
    return ptx.pick(ptx.ERROR_NEUTRAL, lang)

# Appel d'outil écrit en TEXTE par le modèle (vu en réel : 2 cas sur 150, `tool_calls` vide, `finish_reason: stop`). Deux motifs
# STRICTS, en tête du content entier rogné ; le noyau n'interprète ni n'exécute jamais ce texte (une relance, c'est tout).
_TEXT_TOOLCALL_MARKS = (("[tool_input]", "tool_input"), ("[Appel de l'outil : ", "appel_outil"))


def text_toolcall_kind(content: str | None) -> str | None:
    """`tool_input` | `appel_outil` si le content ENTIER ROGNÉ commence par un des deux motifs ; sinon None."""
    text = (content or "").strip()
    for mark, kind in _TEXT_TOOLCALL_MARKS:
        if text.startswith(mark):
            return kind
    return None


# Motif d'une conclusion forcée, tel qu'il s'affiche avec les remarques de la relecture (`_final_review`).
_FORCED_WHY = {"loop:max_calls": "le tour a atteint sa limite d'appels", "loop:stalled": "le tour tournait en rond",
               "loop:tests_green": "les tests sont verts"}


def refusal_why(rule: Verdict | None, decision: Decision) -> str:
    """Motif d'un refus tel que le lisent le modèle et la personne : le MOTIF seul pour un refus des permissions
    (`plan_mode`, `deny_rule`, `hardline`, `no_code_session`). Le nom de la règle (`permissions:…`) est interne : le
    modèle le recopiait à la personne (recette du 15.11). Idem pour la règle `readonly` (sous-agent en lecture seule).
    Les autres refus gardent « règle: motif »."""
    if rule is None:
        return decision.value
    if decision is Decision.DENY and (rule.rule.startswith("permissions:") or rule.rule == "readonly"):
        return rule.reason
    return f"{rule.rule}: {rule.reason}"


def person_why(rule: Verdict | None, decision: Decision) -> str:
    """Motif d'une porte tel que la PERSONNE le lit (escalade, carte) : le libellé de la règle dans sa langue + le
    motif utile, jamais l'identifiant brut (`provenance:…`). Même chemin que `refusal_why` pour les refus des
    permissions (motif seul) ; une règle sans libellé n'est pas recopiée. Le motif brut reste au journal."""
    if rule is None:
        return ""
    if decision is Decision.DENY and rule.rule.startswith("permissions:"):
        return refusal_why(rule, decision)
    label = rule_label(rule.rule)
    if not label or label == rule.rule:
        return rule.reason or ""
    return f"{label} : {rule.reason}" if rule.reason else label


def person_why_all(verdicts: list[Verdict], decision: Decision) -> str:
    return "; ".join(w for w in (person_why(v, decision) for v in verdicts if v.decision is decision) if w)


_INTERNAL_ID = INTERNAL_ID  # table unique des formes d'identifiants générés : nokker/core/idforms.py


def scrub_internal_ids(text: str) -> str:
    """Retire d'un motif écrit par le modèle les identifiants que la personne ne doit pas lire (locataire `tn_…`,
    fil `th_…`, bulle `bu_…`, carte `[approval:…]` ou UUID, sous-agent `sa-…`, agent, objectif, machine, session… :
    `nokker/core/idforms.py`, forme exacte du générateur) et referme les espaces et la ponctuation laissés."""
    out = _INTERNAL_ID.sub("", text or "")
    out = re.sub(r"\(\s*\)|«\s*»", "", out)
    out = re.sub(r"[ \t]+([,;:.)])", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _refusal_advice(rule: Verdict | None, decision: Decision) -> str:
    """Consigne après un refus : en mode plan, une sortie utile (décrire l'action dans le plan) ; ailleurs, la phrase
    d'origine."""
    if rule is not None and decision is Decision.DENY and rule.rule == "permissions:plan":
        if rule.reason in mt.PERM_PLAN_TODO.values():
            return mt.pick(mt.ADVICE_PLAN_TODO)
        return mt.pick(mt.ADVICE_PLAN)
    return mt.pick(mt.RETRY_ADVICE)


def worst(verdicts: list[Verdict]) -> Decision:
    return max((v.decision for v in verdicts), key=lambda d: d.severity, default=Decision.ALLOW)


def error_signature(result: ToolResult) -> str:
    return f"{result.name}:{result.content[:120]}"


_VOLATILE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:s|ms|sec|seconds)\b|0x[0-9a-f]{6,}|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
                       r"|\[approval:[^\]]*\]",
                       re.IGNORECASE)  # `[approval:<id>]` : l'id de la carte change à chaque appel mis en attente


def stall_signature(call: ToolCall, result: ToolResult) -> str:
    """Signature d'un appel d'outil et de son résultat, sans ce qui change d'une exécution à l'autre (durées
    « in 0.53s », adresses 0x…, heures) : deux pytest identiques sur un code inchangé ont la même signature."""
    args = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
    body = hashlib.sha1(_VOLATILE.sub("", result.content or "").encode("utf-8", "replace")).hexdigest()
    return f"{call.name}|{args}|{body}"


def _short_error(e: BaseException | None) -> str:
    """Motif lisible d'une panne d'amont : jamais une page HTML d'erreur recopiée (vu en réel 19.10 : le
    message d'escalade montrait le `<!DOCTYPE html>` d'un 502 du Router)."""
    text = str(e) if e is not None else "erreur inconnue"
    low = text.lower()
    cut = min((i for i in (low.find("<!doctype"), low.find("<html")) if i >= 0), default=-1)
    if cut >= 0:
        text = text[:cut].rstrip(" :") + " (page d'erreur HTML)"
    text = " ".join(text.split())
    return text if len(text) <= 200 else text[:199] + "…"


# Panne d'amont telle que la PERSONNE la lit : classes fermées, libellés figés (jamais le texte de l'exception, qui
# peut porter de l'anglais brut, une adresse ou un nom de classe). `_short_error` reste pour les journaux.
_FAILURE_LABELS = {
    "timeout": ("délai dépassé", "timed out"),
    "key": ("clé refusée", "key rejected"),
    "denied": ("accès refusé", "access denied"),
    "service": ("service indisponible", "service unavailable"),
    "limit": ("limite atteinte", "limit reached"),
    "quota": ("quota d'utilisation atteint", "usage quota reached"),
    "model": ("modèle introuvable", "model not found"),
    "unreachable": ("point d'accès injoignable", "server unreachable"),
    "other": ("erreur inattendue", "unexpected error"),
}
_HTTP_STATUS = re.compile(r"HTTP (\d{3})\b")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_TIMEOUTS = (httpx.TimeoutException, TimeoutError, FutureTimeout)
_UNREACHABLE = (httpx.TransportError, httpcore.NetworkError, ssl.SSLError, OSError)


def _status_of(e: BaseException) -> int | None:
    for obj in (e, getattr(e, "response", None)):
        for attr in ("status", "status_code"):
            v = getattr(obj, attr, None)
            if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599:
                return v
    m = _HTTP_STATUS.match(str(e))  # RuntimeError levées par le fournisseur : « HTTP 401 de … » en tête de message
    return int(m.group(1)) if m else None


def _mentions_model(e: BaseException) -> bool:
    body = getattr(e, "text", None) or getattr(getattr(e, "response", None), "text", None) or ""
    text = _URL.sub(" ", f"{e} {body if isinstance(body, str) else ''}")  # « model » dans une adresse ne compte pas
    return "model" in text.lower()


def _failure_class(e: BaseException) -> str | None:
    status = _status_of(e)
    if status is not None:
        if status in QUOTA_STATUSES and quota_of(e) is not None:
            return "quota"  # 403/429 dont le CORPS dit `quota_exceeded` : le quota est atteint, l'amont n'est pas en panne
        if status == 401:
            return "key"
        if status == 403:
            return "denied"
        if status == 429:
            return "limit"
        if 500 <= status <= 599:
            return "service"
        if status == 408:
            return "timeout"
        if status == 404:
            return "model" if _mentions_model(e) else "unreachable"
        return "other"
    if isinstance(e, _TIMEOUTS):
        return "timeout"
    if isinstance(e, _UNREACHABLE):
        return "unreachable"
    return None


def person_failure_class(failure: BaseException | None) -> str:
    """Clé de `_FAILURE_LABELS` d'une panne d'amont. Suit la chaîne `__cause__` (le fournisseur enrobe les erreurs
    réseau dans une RuntimeError)."""
    e, seen = failure, 0
    while e is not None and seen < 6:
        try:
            cls = _failure_class(e)
        except Exception:  # noqa: BLE001 — un libellé ne doit jamais faire échouer la réponse
            cls = None
        if cls is not None:
            return cls
        e, seen = e.__cause__, seen + 1
    if failure is not None and str(failure).startswith("échec réseau"):  # enrobage sans cause conservée
        return "unreachable"
    return "other"


def quota_reached(failure: BaseException | None) -> dict[str, str | None] | None:
    """`{"retry_at": ISO UTC | None}` si la panne est un quota d'inférence atteint (classe `quota`), None sinon : le
    tour échoue alors avec `quota_text` et SANS repli de palier ni escalade (un quota n'est pas une panne)."""
    return quota_in_chain(failure) if person_failure_class(failure) == "quota" else None


def quota_when(retry_at: str | None, lang: str | None = None, now: datetime | None = None) -> str | None:
    """Date/heure de renouvellement lisible (heure de Zurich, mois en lettres, `QUOTA_WHEN` dans la langue du tour ;
    l'année seulement si l'échéance n'est pas dans l'année de `now`, par défaut l'heure courante) ; None si `retry_at`
    est absent ou illisible."""
    if not retry_at:
        return None
    try:
        zone = ZoneInfo("Europe/Zurich")
        at = datetime.fromisoformat(retry_at.replace("Z", "+00:00")).astimezone(zone)
        year_now = (now.astimezone(zone) if now is not None else datetime.now(zone)).year
    except (ValueError, OverflowError):
        return None
    code = norm_lang(lang) or current_lang()
    table = ptx.QUOTA_WHEN if at.year == year_now else ptx.QUOTA_WHEN_YEAR
    return table[code].format(day=at.day, month=_MONTHS[code][at.month - 1], year=at.year, hour=at.strftime("%H:%M"))


def quota_text(retry_at: str | None, lang: str | None = None, now: datetime | None = None) -> str:
    """Phrase de PERSONNE d'un quota d'inférence atteint : avec la date de renouvellement si on la connaît, sinon la
    variante « bientôt »."""
    when = quota_when(retry_at, lang, now)
    if when is None:
        return ptx.pick(ptx.QUOTA_REACHED_SOON, lang)
    return ptx.pick(ptx.QUOTA_REACHED, lang).format(when=when)


def person_failure_label(failure: BaseException | None, lang: str | None = None) -> tuple[str, str]:
    """(libellé dans la langue, libellé anglais) d'une panne d'amont pour la personne : une des classes fermées de
    `_FAILURE_LABELS` (`person_texts.FAILURE_LABELS` pour de/it). Langue du tour par défaut ; en français : le couple
    (français, anglais) d'avant."""
    cls = person_failure_class(failure)
    table = ptx.FAILURE_LABELS[cls]
    return ptx.pick(table, lang), table["en"]


def byok_down_reply(name: str, failure: BaseException | None, lang: str | None = None) -> str:
    """Réponse BYOK d'un point d'accès muet : début commun, phrase d'action selon la classe de panne. Français : FR puis
    EN (figé) ; en/de/it : la langue de la personne seule."""
    cls = person_failure_class(failure)
    lg = norm_lang(lang) or current_lang()
    why, why_en = person_failure_label(failure, lg)
    if cls == "quota":  # le quota du fournisseur de la personne : motif propre à la phrase BYOK, pas le libellé Nokker
        why, why_en = ptx.pick(ptx.BYOK_QUOTA_WHY, lg), ptx.BYOK_QUOTA_WHY["en"]
    if lg == "fr":
        action, action_en = BYOK_ACTIONS[cls]
        return BYOK_DOWN_HEAD.format(name=name, why=why, why_en=why_en, action=action, action_en=action_en)
    return ptx.BYOK_HEAD[lg].format(name=name, why=why, action=ptx.BYOK_ACTION[ptx.BYOK_ACTION_OF[cls]][lg])


def thread_model_down_reply(name: str, failure: BaseException | None, lang: str | None = None) -> str:
    """Réponse d'un modèle choisi pour le fil qui ne répond pas (Lokkal), dans la langue du tour."""
    lg = norm_lang(lang) or current_lang()
    why, why_en = person_failure_label(failure, lg)
    return ptx.THREAD_MODEL_DOWN[lg].format(name=name, why=why, why_en=why_en)


def prompt_text(assembled) -> str:
    """Le prompt tel qu'un template de chat le rend, à l'ordre près : message système, schémas d'outils (Qwen les
    rend dans le bloc système, donc juste après lui), puis le reste des messages. Sert à mesurer le préfixe
    réutilisable d'un appel à l'autre — pas à construire la requête."""
    msgs = [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in assembled.messages]
    tools = json.dumps(assembled.tools, ensure_ascii=False, sort_keys=True)
    return "\n".join([*msgs[:1], tools, *msgs[1:]])


def reusable_tokens(previous: str | None, current: str, prompt_tokens: int) -> int | None:
    """Tokens du prompt `current` identiques en tête à `previous`, au prorata des caractères (le tokenizer de
    l'amont n'est pas connu du noyau). None sans appel précédent."""
    if previous is None or not current:
        return None
    n = 0
    for a, b in zip(previous, current):
        if a != b:
            break
        n += 1
    return round(prompt_tokens * n / len(current))


def accepts_on_delta(provider: object, keyword: str = "on_delta") -> bool:
    """Vrai si `provider.complete` DÉCLARE le mot-clé `on_delta` (streaming, `core/ports.py::Provider`), ou `keyword`
    (`on_tool_delta`, écriture en direct X22). Pas de `**kwargs` accepté comme preuve : un faux provider ou un Mock ne
    doit pas recevoir un argument qu'il ne comprend pas."""
    try:
        return keyword in inspect.signature(provider.complete).parameters  # type: ignore[attr-defined]
    except (TypeError, ValueError, AttributeError):
        return False


def _served_of(comp: object) -> dict[str, str]:  # model_served, upstream
    """Modèle renvoyé par l'amont (`Completion.model_served`), borné ; absent : rien (`LLMCall.model_served` reste None).
    Jamais le modèle du palier : un provider qui ne lit pas la réponse ne dit rien."""
    got = clean_served(getattr(comp, "model_served", None))
    up = clean_served(getattr(comp, "upstream", None))
    return {**({"model_served": got} if got else {}), **({"upstream": up} if up else {})}


def _call_id_of(comp: object) -> dict[str, str]:
    """Identifiant d'appel posé par le provider (`Completion.call_id`), à passer à `LLMCall` ; absent ou vide : `LLMCall`
    en génère un (un appel = un identifiant, partout au ledger)."""
    got = getattr(comp, "call_id", "")
    return {"call_id": got} if isinstance(got, str) and got else {}


REASONING_MAX_CHARS = 20_000  # réflexion exposée au client, par appel modèle (drapeau `host_reasoning_stream`)


class _ReasoningSink:
    """Puits `TurnStream` minimal pour `ScrubbedDeltas` : les morceaux caviardés de la réflexion vont au puits
    `reasoning` du flux. Pas de `reset` à relayer : le texte complet de fin d'appel (`final=True`) remplace tout."""

    def __init__(self, sink: Callable[..., None], call_index: int) -> None:
        self._sink, self._call_index = sink, call_index

    def delta(self, text: str) -> None:
        self._sink(text, False, self._call_index)

    def reset(self) -> None:
        return None


class _MuteStream:
    """Puits de texte qui ne garde rien : l'amont est appelé en flux (`Kernel.stream_upstream`) sans que personne lise les jetons."""

    def delta(self, text: str) -> None:
        pass

    def reset(self) -> None:
        pass


_MUTE_STREAM = _MuteStream()


@dataclass
class Kernel:
    store: Store
    context: ContextEngine
    memory: MemoryPort
    invariants: Invariants
    gates: Gates
    escalator: Escalator
    policy: Policy
    providers: dict[Tier, Provider]
    tools: ToolRegistry
    ledger: Ledger
    # P1 (18.10.2026) : port optionnel (docs/APPROVALS.md). `None` = comportement inchangé (la carte
    # reste seulement dans `turn.pending_approvals`, aucun test existant ne casse). Branché, `_tool_step`
    # pose une VRAIE carte d'approbation (défi/signature/TOTP) sur `Decision.HITL`.
    approvals: Approvals | None = None
    # Carte posée par le tour : attendre sa décision au plus `approval_wait_s` secondes DANS le tour (réglage d'hôte
    # `local.approval_wait_s`). Décidée à temps, l'outil s'exécute ici, sous le verrou du fil déjà tenu, et le modèle voit le
    # vrai résultat (ou le refus) dans CE tour. 0 (défaut) = comportement d'avant : le tour finit « en attente », la suite
    # de la décision tourne ensuite. `approval_inline(carte, attente_s, annulé)` : `ApprovalExecutor.settle_in_turn`.
    approval_wait_s: float = 0.0
    approval_inline: Any = None
    # Appel amont TOUJOURS en flux (réglage d'hôte `local.stream_upstream`) : sans client qui lit les jetons
    # (`X-Nokker-Stream-Tokens: 1`), un puits muet reçoit le texte, de sorte que `cancel(keep_partial=True)`, demandé APRÈS le début
    # de l'appel, ait un partiel à garder. Ce que voit le client (ordre des événements SSE, jetons) ne change pas. Faux : comme avant.
    stream_upstream: bool = False
    max_calls: int = 8
    # Garde « c'est fait » sans action faite (core/action_claims.py) : réglage d'hôte `local.action_claims_guard`, éteint par défaut.
    action_claims_guard: bool = False
    # Garde « c'est fait » PAR PALIER (nom de palier -> allumée ou non), lue sur le palier du DERNIER appel du tour : `nina` l'a allumée
    # d'office ; `tiers.<nom>.action_claims_guard` de la config la pose ou la retire pour un palier (prime sur `action_claims_guard`).
    # Un palier absent d'ici suit `action_claims_guard`.
    tier_action_claims: dict[str, bool] = field(default_factory=lambda: {"nina": True})
    # Plafond de jetons de sortie par appel modèle (None = défaut du provider, 2048). Session de code (Nokker Lokkal) :
    # 16384, comme l'agent de référence du banc G1 ; à 2048, le 397B était coupé en pleine réflexion (L01, 23.10).
    # Un palier qui pose `tiers.<nom>.max_tokens` prime sur ce défaut (`_loop_max_tokens`).
    max_tokens: int | None = None
    # Réponse coupée par la limite (finish_reason « length ») SANS appel d'outil : ce n'est pas une réponse, le
    # noyau demande de continuer, au plus ce nombre de fois par tour.
    truncation_retries: int = 2
    # Coupure par la limite ALORS QUE la réflexion est active (demandée pour l'appel, ou rendue par l'amont) : la fin du
    # raisonnement peut ressortir dans `content`. Le texte coupé n'entre pas dans l'historique et la relance part SANS
    # réflexion, sous ce plafond de jetons de sortie (« réponds en quelques lignes »). Sans coupure, rien ne change.
    truncation_retry_max_tokens: int = 1024
    # Longue tâche (session de code) : à cette fraction de `max_calls`, le noyau rappelle au modèle qu'il doit conclure
    # (ranger, tester, répondre) ; None = jamais. Banc SWE-bench du 24.10 : 12 tâches sur 31 allaient jusqu'au plafond
    # sans conclure, avec 59 % des jetons du banc.
    budget_notice_at: float | None = None
    # Stagnation : un même appel d'outil (nom + arguments) qui rend le même résultat (durées et adresses ignorées)
    # compte comme une répétition. À la première, une note demande de changer d'approche ; à `stall_limit`
    # répétitions dans le tour, le noyau conclut sans outils (règle `loop:stalled`). None = pas de détection.
    stall_limit: int | None = None
    # MCP Apps (drapeau `mcp_apps`, docs/MCP-NEXT.md) : `nokker.tools.mcp_apps.AppRegistry`, posé par `build_kernel` ;
    # None = aucune app enregistrée, aucun `tool.app`, `app_tool_call` refuse.
    mcp_apps: Any = None
    # BYOK cloud (docs/CLOUD-BYOK.md, `nokker/byok/service.py::CloudByok`) : `route_for_turn()` rend le point d'accès du
    # locataire courant quand son défaut est `byok:<nom>` et qu'il y a droit. Le tour entier passe alors par lui (tous
    # paliers), sans repli vers le Router ni vers un autre palier, hors du quota du Router. None : rien ne change.
    byok: Any = None
    # Modèle choisi pour UN fil (Lokkal, `PUT /v1/local/sessions/{fil}/model`) : `thread_route(thread_id)` rend une
    # `ByokRoute` (même mécanisme que le BYOK : fournisseur de chaque appel du tour, ni palier ni repli) ou None.
    thread_route: Any = None
    # Langue du COMPTE (réglage de la personne) : `account_lang(sender)` rend fr/en/de/it, ou None quand elle n'y est pas
    # posée (inconnu, expéditeur non-compte, défaut). Posé par `build` avec la tenance ; None : aucune langue de compte.
    account_lang: Any = None
    # Fils dont le tour en cours doit s'arrêter (POST /v1/threads/{id}/cancel, client terminal) : vérifié avant chaque
    # appel modèle et chaque outil ; le tour finit sur « annulé », sans erreur d'amont.
    _cancelled: set[str] = field(default_factory=set, init=False, repr=False)
    _keep_partial: set[str] = field(default_factory=set, init=False, repr=False)  # fils dont l'annulation garde le partiel
    _unavailable_seen: dict[tuple[str, int], set[str]] = field(default_factory=dict, init=False, repr=False)
    # `route.model_mismatch` : (palier, modèle servi) déjà signalés, par (fil, tour).
    _fallback_seen: dict[tuple[str, int], set[tuple[str, str]]] = field(default_factory=dict, init=False, repr=False)
    _mismatch_seen: dict[tuple[str, int], set[tuple[str, str]]] = field(default_factory=dict, init=False, repr=False)
    # Appels à effet faits hors tour par le propriétaire : une ligne chacun, ajoutée au contexte reçu du prochain tour
    # du fil (sinon le modèle continuerait sur des fichiers restaurés sans le savoir).
    _owner_notes: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)
    _notes_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def cancel(self, thread_id: str, keep_partial: bool = False) -> None:
        """Arrête le tour du fil. `keep_partial` : la réponse partielle déjà streamée d'un appel modèle en cours est gardée dans
        l'historique (marquée interrompue) au lieu d'être effacée."""
        if keep_partial:
            self._keep_partial.add(thread_id)
        else:
            self._keep_partial.discard(thread_id)
        self._cancelled.add(thread_id)
        self.controls.poke()  # un tour en pause se réveille et finit sur « annulé »

    def _announce_lang(self, thread_id: str) -> str | None:
        """Langue d'une annonce, hors tour : requête de l'hôte, sinon dernière langue reconnue du fil ; None (français)
        quand le fil n'en a aucune."""
        try:
            return self.resolve_lang(thread_id, None)
        except Exception:  # noqa: BLE001 — la langue est un confort : jamais un obstacle à l'annonce
            return None

    def announce(self, thread_id: str, text: str) -> int | None:
        """Un événement annoncé par l'hôte à la session, SANS déclencher de tour : une ligne ajoutée au contexte reçu du
        PROCHAIN tour du fil (même chemin que les appels « hors tour » du propriétaire), caviardée, gardée telle quelle dans
        l'historique de ce tour (jamais réécrite ni délestée en cours de fenêtre). Rend le nombre d'annonces en attente, ou
        None si la file du fil est pleine (`ANNOUNCE_MAX_PENDING`). Un tour en cours ne la voit pas : elle attend le suivant."""
        with self._notes_lock:
            pending = self._owner_notes.setdefault(thread_id, [])
            if len(pending) >= ANNOUNCE_MAX_PENDING:
                return None
            pending.append(self.invariants.scrub_output(f"{mt.pick(mt.ANNOUNCE, self._announce_lang(thread_id))} {text}"))
            return len(pending)

    # ─────────────── pause, reprise, steer (core/controls.py) ───────────────

    def pause(self, thread_id: str) -> None:
        self.controls.pause(thread_id)

    def resume(self, thread_id: str) -> bool:
        return self.controls.resume(thread_id)

    def pause_account(self, tenant: str, account: str) -> None:
        self.controls.pause_account(tenant, account)

    def resume_account(self, tenant: str, account: str) -> bool:
        return self.controls.resume_account(tenant, account)

    def steer(self, thread_id: str, text: str) -> Steer | None:
        """Un message pour le tour en cours du fil, consommé avant son prochain appel modèle. None : steer éteint
        (`steer_enabled`), ou aucun tour ne tourne sur le fil. Caviardé comme un message (invariants d'entrée)."""
        if not self.steer_enabled:
            return None
        return self.controls.steer(thread_id, self.invariants.check_input(text[:STEER_MAX_CHARS]).text)

    def _report_unavailable(self, turn: Turn) -> None:
        """Événement hôte `mcp.server_unavailable {server, reason}`, une fois par serveur et par tour : un serveur d'outils
        injoignable est écarté par le registre, le tour continue avec les autres."""
        down = getattr(self.tools, "unavailable", None)
        if not callable(down):
            return
        try:
            servers = down()
        except Exception:  # jamais bloquant
            logger.debug("serveurs indisponibles illisibles", exc_info=True)
            return
        key = (turn.thread_id, turn.index)
        seen = self._unavailable_seen.setdefault(key, set())
        for name, why in servers:
            if name not in seen:
                seen.add(name)
                self._observe_opt("mcp_server_unavailable", turn, {"server": name, "reason": why})
        if len(self._unavailable_seen) > 256:
            self._unavailable_seen.pop(next(iter(self._unavailable_seen)))

    def _report_model_mismatch(self, turn: Turn, tier: Tier, call: LLMCall) -> None:
        """Événement hôte `route.model_mismatch {tier, model_expected, model_served, call_id}` quand le modèle RENVOYÉ par
        l'amont (en-tête `X-Model-Served` du Router, sinon corps d'un amont direct) diffère de celui du palier
        (`core/model_served.py` : comparaison tolérante ; `upstream` en plus quand l'amont l'a dit). Une fois par (palier, modèle
        servi) et par tour ; jamais bloquant, même si l'observateur lève ; rien sans modèle configuré ni servi."""
        try:
            served = call.model_served
            expected = getattr(self._provider_for(tier), "model", None)
            if not served or not isinstance(expected, str) or same_model(expected, served):
                return
            key = (turn.thread_id, turn.index)
            seen = self._mismatch_seen.setdefault(key, set())
            mark = (tier.value, served)
            if mark in seen:
                return
            seen.add(mark)
            if len(self._mismatch_seen) > 256:
                self._mismatch_seen.pop(next(iter(self._mismatch_seen)))
            logger.warning("route : modèle servi ≠ modèle du palier %s (attendu %s, servi %s)", tier.value, expected,
                           served)
            data = {"tier": tier.value, "model_expected": expected, "model_served": served, "call_id": call.call_id}
            if call.upstream:
                data["upstream"] = call.upstream
            self._observe_opt("route_model_mismatch", turn, data)
        except Exception:  # noqa: BLE001 — jamais bloquant : un tour ne s'arrête pas sur une observation
            logger.debug("route.model_mismatch : événement non émis", exc_info=True)

    @staticmethod
    def _fallback_reason(failure: BaseException | None) -> str:
        """Motif court d'un repli pour l'hôte : classe de panne fermée (`_FAILURE_LABELS`, anglais) + statut HTTP s'il y en a un.
        Jamais le texte de l'exception (`_short_error` peut porter une adresse ou un jeton : il reste pour les journaux)."""
        try:
            label = _FAILURE_LABELS[person_failure_class(failure)][1]
            status = _status_of(failure) if failure is not None else None
            if status is None and failure is not None:
                m = _HTTP_STATUS.search(str(failure))
                status = int(m.group(1)) if m else None
            return f"{label} (HTTP {status})" if status else label
        except Exception:  # noqa: BLE001
            return "unexpected error"

    def _report_route_fallback(self, turn: Turn, src: RouteDecision, dst: RouteDecision, failure: Exception | None) -> None:
        """Repli de palier (Deep indisponible -> Ship) : événement hôte `route.fallback {from, to, reason}` (une fois par
        (tour, from, to) ; `reason` = classe de panne fermée, voir `_fallback_reason`), `turn.fallback` pour `turn.done.fallback`, et
        `turn.notice` (ligne pour la personne) SEULEMENT si le palier voulu était Deep. Jamais bloquant, jamais dans les messages."""
        try:
            frm, to = src.tier.value, dst.tier.value
            turn.fallback = {"from": frm, "to": to}
            if src.tier is Tier.DEEP:
                turn.notice = ptx.pick(ptx.DEEP_FALLBACK_NOTICE, turn.lang or None)
            key = (turn.thread_id, turn.index)
            seen = self._fallback_seen.setdefault(key, set())
            if (frm, to) in seen:
                return
            seen.add((frm, to))
            if len(self._fallback_seen) > 256:
                self._fallback_seen.pop(next(iter(self._fallback_seen)))
            self._observe_opt("route_fallback", turn, {"from": frm, "to": to, "reason": self._fallback_reason(failure)})
        except Exception:  # noqa: BLE001 — jamais bloquant : un tour ne s'arrête pas sur une observation
            logger.debug("route.fallback : événement non émis", exc_info=True)

    def _required_mcp_missing(self, required: tuple[str, ...]) -> list[tuple[str, str]]:
        """(serveur, raison) des serveurs requis qui ne répondent pas, sondés MAINTENANT (une nouvelle tentative courte,
        `local.mcp_required_retry_s`). Un registre qui ne sait pas sonder, ou un sondage qui lève : rien de manquant (jamais bloquant)."""
        probe = getattr(self.tools, "required_unavailable", None)
        if not callable(probe):
            return []
        try:
            return list(probe(required, session_opts.mcp_required_retry_s(), session_opts.mcp_required_cache_s()))
        except Exception:  # noqa: BLE001 — un sondage en panne ne casse pas le tour : le comportement d'avant reprend
            logger.warning("serveurs MCP requis : sondage en échec", exc_info=True)
            return []

    def _mcp_required_failed(self, inp: TurnInput, checked, missing: list[tuple[str, str]]) -> Turn:
        """Tour échoué AVANT l'appel modèle (`mcp_required`) : code stable `mcp_required_unavailable`, serveurs manquants avec leur
        raison brève (sans adresse ni jeton), événement hôte, verdict au journal. Aucun message de l'assistant, rien n'est rangé
        au fil (le message de la personne se renvoie), aucun coût de modèle."""
        names = [n for n, _w in missing]
        turn = Turn(thread_id=inp.thread_id, index=self.store.next_index(inp.thread_id), input=checked,
                    channel=inp.channel, sender=inp.sender, lang=inp.lang or "", lang_signal=inp.lang_signal,
                    lang_seen=(inp.lang or "") if inp.lang_recognized else "", surface=inp.surface)
        servers = [{"server": n, "reason": w} for n, w in missing]
        message = f"serveur(s) MCP requis injoignable(s) : {', '.join(names)}"
        turn.error = "mcp_required_unavailable"
        turn.failure = {"code": "mcp_required_unavailable", "message": message, "servers": servers,
                        "person_message": ptx.pick(ptx.MCP_REQUIRED_UNAVAILABLE, inp.lang or None)}
        logger.warning("%s (fil %s) : le tour n'a pas lieu", message, inp.thread_id[:12])
        self._observe("turn_started", turn)
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule="mcp:required_unavailable",
                                          reason=message)])
        self._observe_opt("mcp_required_unavailable", turn, {"servers": servers})
        turn_done = getattr(self.tools, "turn_done", None)
        if callable(turn_done):
            try:
                turn_done(inp.thread_id)  # le tour suivant sonde de nouveau
            except Exception:  # noqa: BLE001
                logger.debug("turn_done des outils en échec", exc_info=True)
        self._observe("turn_done", turn)
        return turn

    def _observe_opt(self, method: str, *args) -> None:
        """Méthode d'observateur OPTIONNELLE (événements de pause et de steer) : appelée seulement si elle existe."""
        if self.observer is not None and hasattr(self.observer, method):
            self._observe(method, *args)

    def _pause_point(self, turn: Turn, at: str, tool: str | None = None) -> bool:
        """Point de contrôle de la boucle : en pause, le tour attend ici (rien à moitié fait) jusqu'à la reprise ou
        l'annulation. Rend vrai si le tour a attendu."""
        tenant = tenant_of_thread(turn.thread_id)
        if turn.thread_id in self._cancelled or not self.controls.is_paused(turn.thread_id, tenant, turn.sender):
            return False
        where = {"at": at, **({"tool": tool} if tool else {})}
        self._observe_opt("turn_paused", turn, dict(where))
        t0 = time.monotonic()
        self.controls.wait(turn.thread_id, tenant, turn.sender, lambda: turn.thread_id in self._cancelled)
        ms = int((time.monotonic() - t0) * 1000)
        cancelled = turn.thread_id in self._cancelled
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0, rule="loop:paused",
                                          reason=f"pause avant {'l’outil ' + tool if tool else 'l’appel modèle'}, "
                                                 f"{ms / 1000:.1f} s{' (annulé pendant la pause)' if cancelled else ''}")])
        if not cancelled:
            self._observe_opt("turn_resumed", turn, {**where, "paused_ms": ms})
        return True

    def _take_steers(self, turn: Turn, resumed: bool) -> None:
        """Avant un appel modèle : mention de reprise (si le tour a déjà commencé) puis steers en attente, dans l'ordre
        d'arrivée. Steer éteint : rien n'est ajouté (ce que voit le modèle ne change pas)."""
        if not self.steer_enabled:
            return
        if resumed and turn.messages:
            turn.messages.append(Message(role="user", content=mt.pick(mt.RESUME)))
        for s in self.controls.take_steers(turn.thread_id):
            turn.messages.append(Message(role="user", content=mt.pick(mt.STEER).format(text=s.text)))
            self._observe_opt("turn_steered", turn, {"steer_id": s.id})

    def cancel_requested(self, thread_id: str) -> bool:
        """Le tour du fil doit s'arrêter (sans consommer la demande : la boucle la lira après l'outil en cours)."""
        return thread_id in self._cancelled

    def note_owner_call(self, thread_id: str, name: str, arguments: dict, result: ToolResult) -> None:
        args = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        head = (result.content or "")[:NOTE_CHARS]
        self.add_owner_note(thread_id, mt.pick(mt.OWNER_CALL).format(
            name=name, args=args, failed=mt.pick(mt.OWNER_CALL_FAILED) if result.is_error else "", head=head))

    def add_owner_note(self, thread_id: str, line: str) -> None:
        with self._notes_lock:
            self._owner_notes.setdefault(thread_id, []).append(self.invariants.scrub_output(line))

    def direct_call(self, thread_id: str, name: str, arguments: dict, sender: str | None = None) -> ToolResult | str:
        """Un appel d'outil du propriétaire, sans tour modèle (docs/LOKKAL.md §7.4.4) : mêmes portes que la boucle
        (effet de CET appel, règles, invariants, caviardage). Rend le résultat, ou l'id de la carte si l'appel attend
        une approbation (le résultat arrive alors par l'exécuteur de cartes). Un refus est un résultat `is_error`."""
        from nokker.tools.scope import thread_scope

        with thread_scope(thread_id):
            return self._direct_call(thread_id, name, arguments, sender)

    def _direct_call(self, thread_id: str, name: str, arguments: dict, sender: str | None = None) -> ToolResult | str:
        spec = self.tools.spec(name)
        tc = ToolCall(id=f"direct-{time.monotonic_ns()}", name=name, arguments=arguments)
        masked = masked_value_refusal(name, spec, arguments)
        if masked is not None:
            return ToolResult(call_id=tc.id, name=name, is_error=True, content=masked, reason=MASKED_VALUE, refused=True)
        if spec is not None:
            arguments = tc.arguments = spec.normalized(thread_id, arguments)
        turn = Turn(thread_id=thread_id, index=self.store.next_index(thread_id), input=InputCheck(text=""),
                    channel=DIRECT_CHANNEL, sender=sender)
        verdicts = self.gates.action(tc, spec, turn)
        decision = worst(verdicts)
        rule = next((v for v in verdicts if v.decision is decision), None)
        if decision is Decision.ALLOW:
            ctx = ToolContext(thread_id=thread_id, turn_index=turn.index, channel=DIRECT_CHANNEL, sender=sender)
            result = self.invariants.redact_result(self.tools.call(name, arguments, ctx), spec)
            if call_effect(spec, arguments) is not Effect.READ:
                self.note_owner_call(thread_id, name, arguments, result)
            return result
        if decision is Decision.HITL and self.approvals is not None:
            return self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule)).id
        return ToolResult(call_id=tc.id, name=name, is_error=True, refused=True,
                          content=refused_text(refusal_why(rule, decision)),
                          reason=rule.code if rule is not None and decision is Decision.DENY else None)

    def app_tool_call(self, thread_id: str, server: str, name: str, arguments: dict,
                      sender: str | None = None) -> ToolResult | str:
        """Un `tools/call` demandé par une app MCP affichée (docs/MCP-NEXT.md §4) : mêmes portes que la boucle et que
        `direct_call` (effet de CET appel, règles, invariants, carte prouvée), même caviardage. Contrairement à
        `direct_call`, rien n'entre dans le contexte du modèle (ni note « hors tour », ni relance de tour après une
        carte) : l'app n'écrit jamais dans la fenêtre du modèle. Rend le résultat, ou l'id de la carte."""
        from nokker.tools.mcp_apps import APP_CHANNEL

        reg = self.mcp_apps
        spec = reg.spec(server, name) if reg is not None else None
        tc = ToolCall(id=f"app-{time.monotonic_ns()}", name=name, arguments=arguments)
        if spec is None:
            return ToolResult(call_id=tc.id, name=name, is_error=True, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name))
        masked = masked_value_refusal(name, spec, arguments)
        if masked is not None:
            return ToolResult(call_id=tc.id, name=name, is_error=True, content=masked, reason=MASKED_VALUE, refused=True)
        turn = Turn(thread_id=thread_id, index=self.store.next_index(thread_id), input=InputCheck(text=""),
                    channel=APP_CHANNEL, sender=sender)
        verdicts = self.gates.action(tc, spec, turn)
        decision = worst(verdicts)
        rule = next((v for v in verdicts if v.decision is decision), None)
        if decision is Decision.ALLOW:
            ctx = ToolContext(thread_id=thread_id, turn_index=turn.index, channel=APP_CHANNEL, sender=sender)
            result = self.invariants.redact_result(reg.call(server, name, arguments, ctx), spec)
            result.app = None  # une app n'en ouvre pas une autre (v1)
            return result
        if decision is Decision.HITL and self.approvals is not None:
            return self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule)).id
        return ToolResult(call_id=tc.id, name=name, is_error=True, refused=True,
                          content=refused_text(refusal_why(rule, decision)),
                          reason=rule.code if rule is not None and decision is Decision.DENY else None)

    def script_turn(self, ctx: ToolContext) -> Turn:
        """Le tour d'un appel d'outil fait par un script `execute_code` quand l'appel ne vient pas d'un tour (carte
        d'`execute_code` approuvée puis exécutée par `approvals/executor.py`) : même fil, même index, même canal, le
        décideur comme expéditeur — comme `direct_call`."""
        if isinstance(ctx.turn, Turn):
            return ctx.turn
        return Turn(thread_id=ctx.thread_id, index=ctx.turn_index, input=InputCheck(text=""), channel=ctx.channel,
                    sender=ctx.sender)

    def script_tool_call(self, name: str, arguments: dict, turn: Turn, ctx: ToolContext) -> dict[str, Any]:
        """Un appel d'outil venu du script d'`execute_code` (nokker/tools/execute_code.py) : les MÊMES portes que
        `_tool_step` (effet de CET appel, règles, invariants, carte prouvée, escalade), le MÊME caviardage
        (`redact_result`), sur le tour qui a lancé le script. Rend `{status, content[, approval_id]}` pour le script :
        `ok` | `error` (l'outil a échoué) | `pending` (carte posée, rien d'exécuté : le script ne l'attend pas) |
        `refused` (règle, mode plan, escalade). Ni cache de lecture, ni réservoir : le bloc d'`execute_code` est
        borné par lui-même et passe ensuite par l'admission ordinaire du tour."""
        spec = self.tools.spec(name)
        if spec is None:
            return {"status": "error", "content": mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name)}
        tc = ToolCall(id=f"xc-{time.monotonic_ns()}", name=name, arguments=arguments)
        masked = masked_value_refusal(name, spec, arguments)
        if masked is not None:
            return {"status": "refused", "content": masked}
        arguments = tc.arguments = spec.normalized(turn.thread_id, arguments)
        verdicts = self.gates.action(tc, spec, turn)
        if self.provenance is not None:  # un script qui lit un mail puis envoie : même politique que la boucle
            verdicts = self.provenance.check(tc, spec, turn) + verdicts
        self._log_verdicts(turn, verdicts)
        decision = worst(verdicts)
        if decision is Decision.ALLOW:
            inner = ToolContext(thread_id=ctx.thread_id, turn_index=ctx.turn_index, channel=ctx.channel,
                                sender=ctx.sender, kernel=self, turn=turn)
            result = self.invariants.redact_result(self.tools.call(name, arguments, inner), spec)
            if self.provenance is not None:
                self.provenance.record(turn, tc, spec, result)
            return {"status": "error" if result.is_error else "ok", "content": result.content or ""}
        rule = next((v for v in verdicts if v.decision is decision), None)
        why = f"{rule.rule}: {rule.reason}" if rule else decision.value
        if decision is Decision.HITL:
            turn.pending_approvals.append(tc)
            if self.approvals is not None:
                approval = self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule))
                return {"status": "pending", "approval_id": approval.id,
                        "content": self._with_lang(mt.pick(mt.PENDING_CARD).format(why=why, approval_id=approval.id))}
            return {"status": "pending", "content": self._with_lang(mt.pick(mt.PENDING_BARE).format(why=why))}
        if decision is Decision.ESCALATE:
            logger.info("escalade par une porte : fil=%s motif=%s", turn.thread_id, why)
            text = self.escalator.escalate(turn, person_why(rule, decision))
            turn.escalated = True
            return {"status": "refused", "content": mt.pick(mt.ESCALATED).format(why=why, text=text)}
        return {"status": "refused", "content": refused_text(refusal_why(rule, decision))}

    def _cannot_continue(self, turn: Turn, why: str) -> str:
        """Panne qui se règle seule (palier, boucle) : « Je ne peux pas continuer : … », pas une demande à un humain.
        Un escaladeur sans `cannot_continue` (ancien) garde `escalate`."""
        fn = getattr(self.escalator, "cannot_continue", None)
        return fn(turn, why) if fn is not None else self.escalator.escalate(turn, why)

    def _card_advice(self, turn: Turn, tc: ToolCall, rule: Verdict | None) -> str | None:
        """Avis du juge sur une carte (X8) : seulement si `advisor` est branché et que la carte est forte ; jamais une
        panne qui bloque la carte."""
        if self.advisor is None or rule is None:
            return None
        try:
            advice = self.advisor.advise(turn, tc.name, tc.arguments, rule.reason, rule.strength)
        except Exception:  # noqa: BLE001 — une carte sans avis reste une carte
            logger.warning("avis de carte en échec", exc_info=True)
            return None
        self._record_judge_call(turn, self.advisor, getattr(self.advisor, "last_model", ""),
                                getattr(self.advisor, "last_usage", None),
                                getattr(self.advisor, "last_latency_s", 0.0), "juge : avis sur une carte forte")
        return advice

    def _auto_review_eligible(self, rule: Verdict | None, tc: ToolCall, effect: Effect) -> bool:
        """La carte qui va être posée passe-t-elle par le relecteur automatique (§8) ? Drapeau, backend capable, et
        hors de la liste fermée (`auto_review.excluded`)."""
        if self.auto_reviewer is None or rule is None or not hasattr(self.approvals, "decide_auto"):
            return False
        from nokker.approvals.auto_review import excluded
        from nokker.core.ports import Approval

        probe = Approval(id="", thread_id="", turn_index=0, channel="", tool_name=tc.name,
                         arguments=dict(tc.arguments or {}), args_sha256="", strength=rule.strength or "standard",
                         rule=rule.rule)
        return excluded(probe, effect.value) is None

    def _snapshot_rule(self, tc: ToolCall, ctx: ToolContext) -> Any:
        """Écriture de fichier sans point de retour possible (`card_snapshots.preflight`) : escalade par règle, sans
        appel au modèle (pas de point, pas d'approbation automatique). None sinon."""
        from nokker.tools.card_snapshots import SNAPSHOT_TOOLS, preflight

        if tc.name not in SNAPSHOT_TOOLS:
            return None
        why = preflight(self.card_snapshots, ctx.thread_id, tc.name, dict(tc.arguments or {}))
        if why is None:
            return None
        from nokker.approvals.auto_review import ReviewDecision

        return ReviewDecision("escalate", f"écriture de fichier : {why}", by_rule=True)

    def _auto_review(self, turn: Turn, tc: ToolCall, spec: Any, ctx: ToolContext, approval: Any,
                     effect: Effect) -> ToolResult | None:
        """Relit la carte qui vient d'être posée. Rend le résultat à donner au modèle (outil exécuté, ou refus motivé),
        ou None quand la carte reste à l'humain (escalade, doute, panne) : le chemin habituel d'attente continue."""
        try:
            res = self._snapshot_rule(tc, ctx) or self.auto_reviewer.review(turn, approval, call_id=tc.id,
                                                                             approvals=self.approvals)
        except Exception:  # noqa: BLE001 — un relecteur qui tombe laisse la carte à l'humain
            logger.warning("relecteur automatique en échec", exc_info=True)
            res = None
        if res is not None and res.usage:
            self._record_judge_call(turn, self.auto_reviewer, res.model, res.usage, res.latency_s,
                                    "relecteur automatique : carte standard")
        decision = res.decision if res is not None else "escalate"
        reason = res.reason if res is not None else "relecteur en échec : à l'humain"
        try:
            decided = self.approvals.decide_auto(approval.id, decision, reason, model=getattr(res, "model", ""),
                                                 by_rule=bool(getattr(res, "by_rule", True)), effect=effect.value)
        except Exception:  # noqa: BLE001 — carte déjà décidée ailleurs, ou refusée par la liste fermée
            logger.warning("relecteur automatique : décision non appliquée", exc_info=True)
            self.approvals.notify_pending(approval)
            return None
        label = {"approve": "approuvée", "reject": "refusée", "escalate": "laissée à l'humain"}[decision]
        self._log_verdicts(turn, [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW if decision == "approve" else
                                          (Decision.DENY if decision == "reject" else Decision.HITL), layer=2,
                                          rule="approvals:auto_review", reason=f"{label} : {reason}",
                                          judge_model=getattr(res, "model", None) or None,
                                          latency_s=getattr(res, "latency_s", 0.0), subject=tc.id)])
        if decision == "escalate":
            self.approvals.notify_pending(decided)
            return None
        turn.approval_ids.append(approval.id)
        if tc in turn.pending_approvals:  # décidée : plus rien n'attend l'humain pour cet appel
            turn.pending_approvals.remove(tc)
        if decision == "reject":
            return ToolResult(call_id=tc.id, name=tc.name, content=mt.pick(mt.AUTO_REVIEW_REFUSED).format(
                reason=reason, approval_id=approval.id), is_error=False, reason="auto_review_rejected")
        # Approuvée : exécutée DANS le tour, comme une action permise (le modèle voit le résultat de l'outil). La carte
        # est passée dans le contexte : une écriture de fichier y gagne un point de retour par carte (révocation).
        import dataclasses as _dc

        try:
            result = self.tools.call(tc.name, tc.arguments, _dc.replace(ctx, auto_approval_id=approval.id))
        except Exception as e:  # noqa: BLE001 — même contrat que l'exécuteur de cartes
            self.approvals.mark_failed(approval.id, f"{type(e).__name__}: {e}"[:500])
            raise
        result.call_id = tc.id
        redacted = self.invariants.redact_result(result, spec)
        if result.is_error:
            self.approvals.mark_failed(approval.id, (redacted.content or "")[:500])
        else:
            self.approvals.mark_executed(approval.id, (redacted.content or "")[:2000])
        if self.observer is not None and hasattr(self.observer, "auto_review_decided"):  # bulle `approval` du fil
            try:
                self._observe("auto_review_decided", turn, self.approvals.get(approval.id))
            except Exception:  # noqa: BLE001 — une bulle en panne ne casse jamais le tour
                logger.warning("bulle de la décision automatique non posée", exc_info=True)
        suffix = self._observe("tool_executed", turn, tc, spec, redacted)
        admit_in = getattr(self.context, "admit_result_in", None)
        admitted = (admit_in(turn.thread_id, turn.index, redacted) if admit_in is not None
                    else self.context.admit_result(redacted))
        if suffix:
            admitted.content = admitted.content + str(suffix)
        return admitted

    def _record_judge_call(self, turn: Turn, role: Any, model: str, usage: dict | None, latency_s: float,
                           reason: str) -> LLMCall | None:
        """Les jetons du juge comptent au tour (`turn.cost`, X2) et au ledger, comme un appel modèle : palier
        `role.tier` (celui dont le modèle sert de juge), motif « juge : … ». Rend l'appel compté (None sans usage)."""
        if not usage:
            return None
        from nokker.providers.sokkan import _parse_usage

        call = LLMCall(tier=getattr(role, "tier", None) or Tier.SHIP, model=model or getattr(role, "model_name", ""),
                       reasoning=False, prefix_sha256="", layer_tokens={}, usage=_parse_usage(usage),
                       latency_s=latency_s, route_reason=reason)
        turn.calls.append(call)
        try:
            self.ledger.record(turn.thread_id, turn.index, call)
        except Exception:  # noqa: BLE001 — un ledger qui tombe ne coûte pas le tour
            logger.warning("ledger : appel du juge non écrit", exc_info=True)
        return call

    def _run_review(self, turn: Turn, final: bool = False):
        """Une relecture du diff du tour (X8), comptée au tour et tracée (`judge:review`). None : juge en panne ou
        réponse illisible (rien à relire aussi : `reviewer.review` rend None sans diff)."""
        try:
            res = self.reviewer.review(turn)
        except Exception:  # noqa: BLE001 — un juge qui tombe ne relit pas, le tour finit
            logger.warning("relecture en échec", exc_info=True)
            return None
        if res is None:
            return None
        self._record_judge_call(turn, self.reviewer, res.model, res.usage, res.latency_s,
                                "juge : relecture du diff avant « terminé »" if not final
                                else "juge : relecture du diff d'une conclusion forcée")
        verdict = "OK" if res.ok else ("à corriger, non renvoyé à l'auteur (plus d'appel pour corriger), remarques "
                                       "jointes à la réponse" if final else "à corriger")
        self._log_verdicts(turn, [Verdict(
            gate=Gate.SWITCH, decision=Decision.ALLOW if res.ok else Decision.REDO_DEEP, layer=2, rule="judge:review",
            reason=(f"relecture du diff : {verdict}" + ("" if res.ok else f"\n{res.remarks}"))[:1500],
            judge_model=res.model, latency_s=res.latency_s, subject="reply")])
        return res

    def _review(self, turn: Turn, candidate: str, rounds: int, call_index: int) -> str | None:
        """Relecture du diff avant « terminé » (X8). Rend la note à renvoyer à l'auteur, ou None (rien à relire,
        relecture OK, juge muet, ou plus de tour de relecture). Plus assez d'appels pour corriger : relecture finale
        sans renvoi (`_final_review`), faite par l'appelant."""
        if self.reviewer is None or turn.escalated or rounds >= int(getattr(self.reviewer, "rounds", 1)):
            return None
        if call_index + 2 > self.max_calls:
            return None
        res = self._run_review(turn)
        if res is None or res.ok:
            return None
        from nokker.qa.review import REVIEW_NOTE

        return REVIEW_NOTE.format(remarks=res.remarks or "(sans détail)")

    def _final_review(self, turn: Turn, reply: str, why: str) -> str:
        """Relecture d'une réponse qui ne peut plus être corrigée (lot fix-batch-1) : conclusion forcée (plafond
        d'appels, stagnation) ou réponse au dernier appel. UNE relecture, jamais de renvoi à l'auteur — le plafond est
        atteint, un renvoi serait une boucle sans budget ; les remarques sont jointes à la réponse, visibles. Juge muet
        ou en panne sur un tour qui a écrit : la réponse dit que le diff n'a pas été relu. Pas de relecteur, rien
        d'écrit, tour escaladé : la réponse part telle quelle."""
        from nokker.qa.review import FINAL_REVIEW_NOTE, NOT_REVIEWED_NOTE, turn_diff

        if self.reviewer is None or turn.escalated or not reply.strip():
            return reply
        if not turn_diff(turn, int(getattr(self.reviewer, "max_diff_chars", 24000))):
            return reply
        res = self._run_review(turn, final=True)
        if res is None:
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=2,
                                              rule="judge:review:skipped",
                                              reason=f"diff non relu ({why}) : relecteur muet ou en panne",
                                              subject="reply")])
            return reply + NOT_REVIEWED_NOTE
        if res.ok:
            return reply
        return reply + FINAL_REVIEW_NOTE.format(why=why, remarks=res.remarks or "(sans détail)")

    def _take_cancel(self, thread_id: str) -> bool:
        if thread_id in self._cancelled:
            self._cancelled.discard(thread_id)
            return True
        return False
    # Budget d'appels mémoire explicites par tour (le noyau fait déjà le rappel automatique) : au-delà, le noyau
    # répond sans exécuter. Mesuré au banc (17.10.2026) : identite-meta 9-14 memory_search/memory_get par tour.
    memory_calls_per_turn: int = 3
    # Schémas d'outils envoyés (tools/select.py) : « relevance » = cœurs + outils de la fenêtre + familles demandées par
    # le message (amont sans cache : chaque token payé à chaque appel) ; « all » = tout, sans plafond (amont à cache de
    # préfixe). Remplace preload_all_tools/preload_max_tools (21.10.2026) : avec les 4 outils des bulles, le canari
    # dépassait le plafond de 40 et ne préchargeait plus RIEN (trouvé par le banc de conversations).
    tool_loading: str = "relevance"
    # Outils utilisés dans les N derniers tours de la fenêtre : rechargés pour une relance (« et Lausanne ? »).
    tool_reuse_turns: int = 5
    recall_k: int = 5
    # Attente maximale du rappel, comptée depuis son lancement (début du tour) : au-delà, le tour part sans
    # rappel (verdict `memory:recall_late`) — le modèle garde memory_search s'il en a besoin. Un pic du serveur
    # mémoire ne retarde plus la réponse que de ce budget, pas des 20 s du timeout MCP.
    recall_budget_s: float = 2.5
    # P1 bulles (docs/BUBBLES.md §3/§6) : observateur optionnel (`nokker/bubbles/hub.py::BubbleHub`) —
    # contexte reçu d'autres fils, événements turn.*/tool.*, producteurs de bulles. `None` = inchangé.
    observer: TurnObserver | None = None
    # Magasin des images jointes (X7, `nokker/images/store.py::ImageStore`) : le noyau y lit les octets juste avant
    # l'appel modèle, pour un palier avec vision. `None` = aucune image ne peut être lue (note texte au modèle).
    images: Any = None
    # Pièces jointes texte/dossier/url (`nokker/attachments.py::TextAttachments`), posé quand
    # `text_attachments_enabled` est vrai. `None` = format X7 seul (images), comportement d'avant.
    text_attachments: Any = None
    # Plafonds des testeurs (docs/TENANCY.md §5, `nokker/tenancy/limits.py::TenantLimits`) : vérifiés AVANT le tour,
    # jamais pour le locataire interne. `None` = aucun plafond (tests, bancs).
    limits: TenantLimits | None = None
    # Budget des projets d'équipe (docs/LOKKAL.md §7.15, `nokker/team/budget.py::ProjectBudget`) : un tour rattaché à un
    # projet porte son étiquette au ledger ; au-delà du plafond du projet, refusé AVANT tout appel modèle (comme
    # `limits`). `None` = équipe éteinte, rien n'est attribué.
    project_budget: Any = None
    # Quota commun du foyer (lot X23, `nokker/household/quota.py::HouseholdQuota`) : un tour d'un membre porte
    # l'étiquette du foyer au ledger (même mécanisme que le budget) ; au-delà d'un plafond du foyer ou du membre, refusé
    # AVANT tout appel modèle. `None` = foyer éteint, rien n'est attribué.
    household: Any = None
    # Plafond de dépense par session (drapeau `host_session_budget`, `nokker/ledger/session_budget.py::SessionBudgets`) :
    # posé par le produit hôte sur un fil, relu au ledger avant chaque tour ; atteint, le tour est refusé AVANT tout
    # appel modèle (comme `project_budget`), avec un échec au code stable. `None` (défaut) : rien n'est vérifié.
    session_budget: Any = None
    # Arrêt propre demandé par l'appelant (passage d'un agent Crew, lot X14 : budget d'appels ou de coût atteint) :
    # appelé avant chaque appel modèle ; un motif (chaîne) termine le tour sur « Arrêté par le noyau : <motif>. »,
    # `turn.error = "stopped"`, sans nouvel appel. None = jamais.
    stop_check: Callable[[Turn], str | None] | None = None
    # Questions à choix (drapeau `host_clarify_options`, `nokker/tools/clarify.py::QuestionBoard`) : un `clarify` avec
    # options est inscrit ici et annoncé aux clients (`question_asked`). None (défaut) : rien ne change.
    questions: Any = None
    # Flux de réflexion vers le client (drapeau `host_reasoning_stream`, docs/NOKKAN-HOST-API.md §3) : la réflexion que
    # l'amont renvoie (`Completion.reasoning`) sort caviardée dans un champ séparé (puits `reasoning` du flux,
    # observateur `turn_reasoning`). Faux (défaut) : elle est ignorée, comme avant.
    reasoning_stream: bool = False
    # Juge allumé (X8, docs/LOKKAL.md §7.17, `nokker/qa/review.py`) : `reviewer` relit le diff du tour avant la réponse
    # (renvoi à l'auteur avec les remarques, `reviewer.rounds` fois au plus) ; `advisor` met un avis et un niveau de
    # risque sur les cartes fortes. `None` = éteints (défaut).
    reviewer: Any = None
    advisor: Any = None
    # Rubrique et objectifs (docs/OUTCOMES-GOAL.md, `nokker/goals/service.py::GoalService`, drapeaux `outcomes_enabled`
    # et `goals_enabled`) : commandes `/goal`, réponse au tour `goal_check`, passes notées par un correcteur séparé
    # dans le tour, objectif noté et enchaîné en fin de tour. `None` = éteints (défaut) : rien ne change.
    goals: Any = None
    # Capture d'un mot de passe ou d'un code écrit dans le chat (docs/CONNECTED-SITES.md §4.6, `nokker/sites/capture.py`) :
    # `intercept(thread_id, sender, text)` → `Capture` ou None, appelé AVANT tout le reste du tour (avant `check_input`,
    # le stockage, le rappel et le modèle, quotas compris). None = éteint (défaut : drapeau `sites`).
    secret_capture: Any = None
    # Relecteur automatique des cartes `standard` (docs/APPROVALS.md §8, `nokker/approvals/auto_review.py`,
    # drapeau `auto_review`) : approuve, refuse ou laisse à l'humain une carte de la liste permise. `None` = éteint
    # (défaut) : aucune carte relue, chemin et textes inchangés.
    auto_reviewer: Any = None
    # Points de retour PAR CARTE des écritures approuvées automatiquement (`nokker/tools/card_snapshots.py`) : un
    # magasin par enveloppe des points de retour ; lus par la révocation (`rollback`) et `…/auto-review/rollback`.
    card_snapshots: list[Any] = field(default_factory=list)
    # Traces d'entraînement (docs/TRACES.md, `nokker/traces/recorder.py::TraceRecorder`) : chaque appel modèle (messages
    # exacts envoyés, schémas, suite) gardé à part, écrit à la fin du tour. `None` = rien n'est enregistré (défaut ;
    # `traces_record: false`). Jamais d'effet sur ce que voit le modèle : l'enregistreur copie, et une erreur est avalée.
    traces: Any = None
    # Palier « nina » en ombre (route/nina.py::NinaShadow, `route.nina.mode: shadow`) : le premier appel d'une fraction des
    # tours du locataire interne est doublé en tâche de fond, après la réponse réelle ; rien ne revient dans la boucle.
    nina_shadow: Any = None
    # Invitation de fin de tour (drapeau `learning_nudges`, nokker/learning/nudge.py, docs/LEARNING.md §4) :
    # `after_turn(turn)` → une ligne fixe ajoutée au contexte du PROCHAIN tour du fil, ou None. None = éteinte.
    learning: Any = None
    # Provenance des valeurs (drapeau `provenance`, docs/PROVENANCE.md, `nokker/qa/provenance.py::ProvenanceGuard`) :
    # étiquette ce qui entre dans le tour et les résultats d'outils, et refuse ou soumet à une carte forte une action
    # sensible dont un argument critique ne vient que d'une source non fiable. Partagé par les copies du noyau
    # (`dataclasses.replace`, sous-agents). None = éteint (défaut) : aucun verdict ni texte de plus.
    provenance: Any = None
    # Dernier prompt rendu par fil (`prompt_text`), pour `LLMCall.reusable_tokens` ; borné aux fils récents.
    _last_prompts: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _last_prompts_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    last_prompts_max: int = 256
    # File par fil (docs/LOKKAL.md §7.4.9) : `run_turn` prend le verrou équitable du fil, quel que soit l'appelant (API,
    # Telegram, tour de fond, suite d'une décision, CLI, sous-agents). Le hub et l'exécuteur de cartes partagent ces
    # verrous (`BubbleHub.thread_lock`).
    turn_locks: TurnLocks = field(default_factory=TurnLocks, repr=False)
    # Plafond DUR de tours de SUITE consécutifs par fil sans message de la personne (tâche du téléphone, pas du bureau) : au-delà, le tour est refusé, journalisé, sans appel modèle. 0 = pas de plafond. Vu sur lokkal-test1 (12.11) : 85 tours
    # de suite enchaînés en 9 minutes, chacun posant une nouvelle carte. Un message de la personne remet le compte à zéro.
    follow_chain_max: int = 12
    _follow_chain: dict = field(default_factory=dict, repr=False)
    # Fin sûre d'une chaîne de tours (suites de cartes, `APPROVAL_CONTINUE`) — deux réglages d'hôte, éteints par défaut.
    # `chain_max_calls` : plafond CUMULÉ d'appels modèle depuis le dernier message de la personne ; sans lui, chaque tour
    # de suite repart à `max_calls` (jusqu'à `max_calls` × (1 + `follow_chain_max`) appels pour une même demande).
    # 0 = pas de plafond cumulé (comportement d'avant). `conclude_on_green` : une commande de tests reconnue qui rend 0
    # APRÈS une écriture de la chaîne clôt la tâche (conclusion sans outils, `loop:tests_green`) au lieu de laisser le
    # modèle tourner. Faux = comportement d'avant.
    chain_max_calls: int = 0
    conclude_on_green: bool = False
    _chain: dict = field(default_factory=dict, repr=False)
    # Détecteur inter-tours de cartes identiques (`core/approval_repeat.py`) : alimenté par l'exécuteur d'approbations ;
    # à `approval_repeat_max` exécutions identiques de suite sur le fil, la carte suivante n'est pas posée. 0 = éteint.
    approval_repeat_max: int = APPROVAL_REPEAT_MAX
    approval_repeat: ApprovalRepeat = field(default_factory=ApprovalRepeat, repr=False)
    # Garde-fou contre les boucles d'outils de lecture (`core/tool_loop_guard.py`) : `local.tool_loop_guard` ; None = défaut
    # (note à 4 appels stagnants, suspension à 8), False = éteint, `Thresholds` = seuils choisis. Réglable par session.
    tool_loop_guard: Any = None
    # Pause / reprise / steer (`core/controls.py`) : partagés par les copies du noyau (`dataclasses.replace`).
    controls: TurnControls = field(default_factory=TurnControls, repr=False)
    # Steer et mention de reprise vus par le modèle (STEER_BLOCK, RESUME_NOTE) : éteints par défaut, tant que les
    # traces d'entraînement ne les portent pas (docs/CONTEXT-FORMAT.md). La pause, elle, ne change rien au contexte.
    steer_enabled: bool = False
    # Fork de sous-agent (drapeau `delegate_fork`, `tools/delegate.py`, `context/fork.py`) : le noyau garde, par fil, le
    # contexte EXACT (messages assemblés + schémas) du dernier appel modèle, pour qu'un `delegate(fork=true)` du même
    # tour en fasse le préfixe de l'enfant (même préfixe, cache réutilisé). False (défaut) : rien n'est gardé.
    fork_bases: bool = False
    fork_bases_max: int = 64
    # Partagé par les copies du noyau (`dataclasses.replace`), comme `_last_prompts`.
    _fork_bases: OrderedDict = field(default_factory=OrderedDict, repr=False)

    def _observe(self, method: str, *args):
        if self.observer is None:
            return None
        try:
            return getattr(self.observer, method)(*args)
        except Exception:  # noqa: BLE001 — un observateur en panne ne casse jamais un tour
            logger.exception("observateur %s a levé", method)
            return None

    def _await_recall(self, turn: Turn, future, started: float) -> list:
        """Résultat du rappel lancé en tête de tour, attendu au plus `recall_budget_s` depuis son lancement.
        En retard ou en panne : tour sans rappel, jamais bloqué ni cassé (le résultat tardif est abandonné)."""
        if future is None:
            return []
        try:
            recall = future.result(timeout=max(0.0, self.recall_budget_s - (time.monotonic() - started)))
        except FutureTimeout:
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="memory:recall_late",
                                              reason=f"> {self.recall_budget_s:.1f} s, tour sans rappel")])
            return []
        except Exception:  # noqa: BLE001 — la mémoire en panne ne casse jamais un tour
            logger.exception("memory.recall a levé")
            return []
        if recall:
            # Signal de mesure (demandé P1-S1, 17.10.2026) : un verdict dédié, layer=0 (ni invariant ni
            # juge — un FAIT, pas une décision de sûreté), pour qu'un juge de banc puisse reconnaître
            # qu'un rappel automatique a eu lieu SANS que le modèle ait eu besoin d'appeler memory_search
            # lui-même. Décision ALLOW fixe : ceci ne bloque jamais rien, ça journalise seulement.
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="memory:recall_used",
                                              reason=f"{len(recall)} note(s), {time.monotonic() - started:.2f} s")])
        # Caviardé comme le contexte reçu (`turn.injected`) : le rappel entre dans `Turn.head`, figé, PERSISTÉ dans
        # `turns` (ajout seul) et rejoué dans l'historique — une valeur du coffre dans une note ne doit jamais y entrer
        # (invariant 5 de docs/NEVER-CLEAR.md ; trouvé en revue le 20.10.2026).
        # `source` et `provenance` survivent au caviardage : une donnée importée (Codex) retourne dans son bloc à part,
        # jamais dans `[Rappel mémoire]` ; un fait de tiers garde son marqueur.
        return [Recall(note=r.note, text=self.invariants.scrub_output(r.text), score=r.score, rerank=r.rerank,
                       source=getattr(r, "source", "") or "", provenance=getattr(r, "provenance", "") or "")
                for r in recall]

    def _record_call(self, turn: Turn, assembled, comp, route: RouteDecision, route_reason: str) -> LLMCall:
        """Un appel modèle au ledger, avec le préfixe réutilisable par rapport au DERNIER prompt envoyé sur ce fil."""
        rendered = prompt_text(assembled)
        with self._last_prompts_lock:  # un tour par thread côté API : plusieurs fils en même temps
            previous = self._last_prompts.get(turn.thread_id)
            self._last_prompts[turn.thread_id] = rendered
            self._last_prompts.move_to_end(turn.thread_id)
            while len(self._last_prompts) > self.last_prompts_max:
                self._last_prompts.popitem(last=False)
        call = LLMCall(tier=route.tier, model=comp.model, reasoning=route.reasoning,
                       prefix_sha256=assembled.prefix_sha256, layer_tokens=assembled.layer_tokens,
                       usage=comp.usage, latency_s=comp.latency_s, route_reason=route_reason,
                       reusable_tokens=reusable_tokens(previous, rendered, comp.usage.prompt),
                       **_call_id_of(comp), **_served_of(comp))
        turn.calls.append(call)
        self._report_model_mismatch(turn, route.tier, call)
        # Réflexion de la dernière réponse SANS appel d'outil : `_seal_thinking` la range sur le message final du tour.
        msg = getattr(comp, "message", None)
        turn.final_thinking = [] if getattr(msg, "tool_calls", None) else list(getattr(msg, "thinking_blocks", None) or [])
        self.ledger.record(turn.thread_id, turn.index, call)
        self._trace("answered", turn, comp, call)
        # Une ligne par appel, sans contenu (le fil est haché) : la répartition par palier et par classe se lit dans les
        # journaux du pod, sans accès à la base (mesure du routage du 01.11.2026, impossible avant cette ligne).
        u = comp.usage
        logger.info("route : fil %s tour %d appel %d palier %s modèle %s %d+%d jetons (cache %s) %.2f s — %s",
                    hashlib.sha256(turn.thread_id.encode()).hexdigest()[:8], turn.index, len(turn.calls) - 1,
                    route.tier.value, comp.model, u.prompt, u.completion,
                    u.cached if u.cached_reported else "?", comp.latency_s or 0.0, route_reason[:160])
        return call

    def record_side_call(self, turn: Turn, tier: Tier, comp, reason: str, messages: list[dict],
                         side: str = "advisor", meta: dict[str, Any] | None = None) -> LLMCall:
        """Un appel modèle fait À CÔTÉ de la boucle pendant un tour (outil `advise`, `route/advise.py`) : compté au tour
        (`turn.calls`, donc `turn.cost` et les budgets), écrit au ledger avec son motif, et tracé à part
        (`TraceRecorder.side`, sans toucher au découpage des appels de la boucle)."""
        call = LLMCall(tier=tier, model=comp.model, reasoning=False, prefix_sha256="", layer_tokens={},
                       usage=comp.usage, latency_s=comp.latency_s, route_reason=reason, **_call_id_of(comp),
                       **_served_of(comp))
        turn.calls.append(call)
        self._report_model_mismatch(turn, tier, call)
        try:
            self.ledger.record(turn.thread_id, turn.index, call)
        except Exception:  # noqa: BLE001 — un ledger qui tombe ne coûte pas le tour
            logger.warning("ledger : appel %s non écrit", side, exc_info=True)
        if self.traces is not None and hasattr(self.traces, "side"):
            self._trace("side", turn, side, messages, comp, call, meta)
        return call

    def fork_base(self, thread_id: str, turn_index: int):
        """Contexte du dernier appel modèle du fil (`fork_bases`), s'il appartient à CE tour et au locataire courant ;
        None sinon. Rend une copie (l'enfant ne peut pas modifier celle du parent)."""
        from nokker.tenancy.context import current_tenant_or_none

        with self._last_prompts_lock:
            got = self._fork_bases.get((current_tenant_or_none(), thread_id))
        if got is None or got[0] != turn_index:
            return None
        _, messages, tools, sha = got
        from nokker.core.ports import Assembled

        return Assembled(messages=json.loads(json.dumps(messages)), tools=json.loads(json.dumps(tools)),
                         prefix_sha256=sha)

    def forked(self, base, session_key: str, child_thread: str) -> "Kernel":
        """Copie du noyau dont le contexte part de `base` (fork, `context/fork.py`) ; affinité de cache sur le fil
        parent (`session_key`). Le préfixe réutilisable du premier appel de l'enfant se mesure contre le dernier prompt
        du parent (`LLMCall.reusable_tokens`)."""
        import dataclasses

        from nokker.context.fork import ForkedContext

        with self._last_prompts_lock:
            self._last_prompts[child_thread] = prompt_text(base)
        return dataclasses.replace(self, context=ForkedContext(inner=self.context, base_messages=base.messages,
                                                               base_tools=base.tools, session_key=session_key))

    def _keep_fork_base(self, turn: Turn | None, assembled) -> None:
        if not self.fork_bases or turn is None:
            return
        from nokker.tenancy.context import current_tenant_or_none

        key = (current_tenant_or_none(), turn.thread_id)
        try:
            entry = (turn.index, json.loads(json.dumps(assembled.messages)), json.loads(json.dumps(assembled.tools)),
                     assembled.prefix_sha256)
        except (TypeError, ValueError):  # contenu non sérialisable (faux contexte d'un test) : pas de fork possible
            return
        with self._last_prompts_lock:
            self._fork_bases[key] = entry
            self._fork_bases.move_to_end(key)
            while len(self._fork_bases) > self.fork_bases_max:
                self._fork_bases.popitem(last=False)

    def _trace(self, hook: str, turn: Turn | None, *args) -> None:
        """Crochet de l'enregistreur de traces (`traces`) : sans effet sur le tour, toute erreur journalisée et avalée."""
        if self.traces is None or turn is None:
            return
        try:
            getattr(self.traces, hook)(turn, *args)
        except Exception:  # noqa: BLE001 — une trace manquée ne casse jamais un tour
            logger.warning("traces : crochet %s en échec", hook, exc_info=True)

    def _chain_state(self, thread_id: str) -> dict:
        st = self._chain.get(thread_id)
        if st is None:
            if len(self._chain) > 5000:  # borne mémoire : on oublie les plus anciens fils
                for k in list(self._chain)[:2500]:
                    self._chain.pop(k, None)
            st = self._chain[thread_id] = {"calls": 0, "wrote": False, "green": False}
        return st

    def note_outcome(self, thread_id: str, name: str, arguments: dict, result: ToolResult) -> None:
        """Résultat d'un outil (dans la boucle, ou exécuté après une carte par l'exécuteur d'approbations) pour la règle de
        fin `conclude_on_green` : une écriture réussie ouvre la chaîne « écrit », une commande de tests reconnue qui
        rend 0 après elle la dit « verte », une écriture de plus la rouvre. Sans le réglage : aucune trace."""
        if not self.conclude_on_green or result.is_error:
            return
        st = self._chain_state(thread_id)
        if name in WRITE_TOOLS:
            st["wrote"], st["green"] = True, False
        elif name == "terminal" and st["wrote"] and result.exit_code == 0:
            from nokker.tools.code import is_test_command

            if is_test_command(str((arguments or {}).get("command") or "")):
                st["green"] = True

    def _conclude(self, turn: Turn, history: list, recall: list, state: RouteState, thread_id: str,
                  stream: TurnStream | None, rule: str, reason: str, reply: str = "") -> str:
        """Fin de boucle sans réponse du modèle (limite d'appels, stagnation) : un dernier appel SANS outils rédige la
        réponse avec ce qui a été trouvé (vu en réel le 20.10.2026 : la bonne page trouvée au 8e appel, puis « boucle
        d'outils non terminée » escaladée ; et le tour suivant répondait à cette question restée orpheline). Le palier
        est redemandé à la politique : un doute (REDO_DEEP) posé au dernier appel envoie la conclusion en Deep. Toujours
        une réponse dans l'historique : jamais de question laissée ouverte pour le tour suivant."""
        route = self._choose_route(state)
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0, rule=rule,
                                          reason=reason)])
        reply = self._wrap_up(turn, history, recall, route, thread_id, stream,
                              note=mt.pick(mt.GREEN_WRAP_UP) if rule == "loop:tests_green" else mt.pick(mt.WRAP_UP)) \
            if not turn.escalated else reply
        why = _FORCED_WHY.get(rule, rule)
        if not reply and turn.failure is not None and turn.failure.get("code") == "quota_exceeded":
            reply = turn.failure["person_message"]  # quota atteint à la conclusion : la phrase de quota, sans escalade
        elif not reply:
            turn.error = "max_calls"  # échec : ni conclusion ni escalade motivée
            if turn.failure is None:  # un amont en échec à la conclusion a déjà dit pourquoi (provider_error)
                turn.failure = {"code": "max_calls", "message": reason}
            reply = self._cannot_continue(turn, "je n'ai pas réussi à finir avec mes outils")
            turn.escalated = True
            if self.reviewer is not None and self._wrote(turn):
                from nokker.qa.review import NOT_REVIEWED_NOTE

                reply += NOT_REVIEWED_NOTE  # l'humain qui reprend sait que le diff n'a pas été relu
        else:
            # Conclusion forcée : relue une fois, sans renvoi (lot fix-batch-1 ; banc D1, H03 finissait non relue).
            reply = self._final_review(turn, reply, why)
        turn.messages.append(Message(role="assistant", content=reply))
        return reply

    def _quota_failure(self, turn: Turn, quota: dict[str, str | None]) -> str:
        """Pose l'échec « quota d'inférence atteint » du tour (`turn.failure` : code `quota_exceeded`, `retry_at` ISO UTC
        s'il est connu) et rend la phrase de la personne. Verdict `router:quota` au ledger (refus, pas une panne)."""
        retry_at = quota.get("retry_at")
        text = quota_text(retry_at)
        turn.failure = {"code": "quota_exceeded", "message": text, "person_message": text,
                        **({"retry_at": retry_at} if retry_at else {})}
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule="router:quota",
                                          reason="quota d'inférence atteint" + (f" (retry_at {retry_at})" if retry_at else ""))])
        return text

    def _wrote(self, turn: Turn) -> bool:
        from nokker.qa.review import turn_diff

        return bool(turn_diff(turn, int(getattr(self.reviewer, "max_diff_chars", 24000))))

    def _wrap_up(self, turn: Turn, history: list, recall: list, route: RouteDecision, thread_id: str,
                 stream: TurnStream | None = None, redo_allowed: bool = True, note: str | None = None) -> str:
        """Dernier appel sans outils quand la limite d'appels est atteinte : la réponse avec ce qui a été trouvé, ce qui
        manque dit clairement. La porte bascule s'applique comme dans la boucle : doute (REDO_DEEP) hors Deep → une
        relance en Deep ; refus → escalade avec le motif de la porte. Chaîne vide seulement si l'amont échoue ou ne rend
        rien (l'appelant escalade alors)."""
        assembled = self.context.assemble(turn, history, recall, [], route.tier)
        route, assembled = self._fit_context(turn, route, assembled,
                                             lambda t: self.context.assemble(turn, history, recall, [], t))
        assembled.messages.append({"role": "user", "content": note or mt.pick(mt.WRAP_UP)})
        assembled.tools = []
        try:
            comp, live = self._complete(self._provider_for(route.tier), assembled, route, thread_id, stream, turn)
        except TurnCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — l'appelant escalade
            logger.warning("appel de conclusion en échec", exc_info=True)
            quota = quota_reached(e)
            if quota is not None:
                self._quota_failure(turn, quota)
                return ""
            turn.failure = {"code": "provider_error", "message": f"appel de conclusion en échec : {_short_error(e)}",
                            "person_message": ptx.pick(ptx.ERROR_DOWN).format(why=person_failure_label(e)[0])}
            return ""
        self._record_call(turn, assembled, comp, route, f"conclusion (limite d'appels) : {route.reason}")
        candidate = comp.message.content or ""
        if not candidate.strip() or comp.message.tool_calls:
            return ""
        verdicts = self.gates.switch(candidate, turn, route.tier)
        self._log_verdicts(turn, verdicts)
        decision = worst(verdicts)
        if (decision is Decision.REDO_DEEP and route.tier is not Tier.DEEP and redo_allowed
                and Tier.DEEP in self.providers and session_opts.escalation_allowed(thread_id)):
            if live is not None:
                live.discard()  # la conclusion doutée ne reste pas à l'écran
            deep = self._deep_decision(thread_id, "porte bascule : doute sur la conclusion")
            return self._wrap_up(turn, history, recall, deep, thread_id, stream, redo_allowed=False, note=note)
        if decision in (Decision.ESCALATE, Decision.HITL, Decision.DENY):
            logger.info("escalade par une porte : fil=%s motif=%s", turn.thread_id,
                        "; ".join(f"{v.rule}: {v.reason}" for v in verdicts if v.decision is decision))
            turn.escalated = True
            return self.escalator.escalate(turn, person_why_all(verdicts, decision))
        return candidate

    def thread_lang(self, thread_id: str) -> str | None:
        """Dernière langue reconnue sur le fil (`Turn.lang_seen` du message humain le plus récent), ou None."""
        try:
            for t in reversed(self.store.history(thread_id, limit=LANG_LOOKBACK)):
                if getattr(t, "lang_seen", ""):
                    return norm_lang(t.lang_seen)
        except Exception:  # la langue est un confort : un historique illisible donne `fr`
            logger.warning("langue du fil illisible (%s)", thread_id[:12], exc_info=True)
        return None

    def resolve_lang(self, thread_id: str, sender: str | None, explicit: str | None = None,
                     text: str | None = None) -> str:
        """Langue des textes fixes d'un tour : `explicit` (ce que la personne vient d'écrire : requête, `language_code`),
        sinon la langue de son compte, sinon celle DÉTECTÉE dans `text` (son message, signal net seulement), sinon la
        dernière langue reconnue du fil, sinon celle de l'installation (`NOKKER_LANG`), sinon `fr`."""
        return self._resolve_lang(thread_id, sender, explicit, text)[0]

    def _resolve_lang(self, thread_id: str, sender: str | None, explicit: str | None,
                      text: str | None) -> tuple[str, bool]:
        """(langue, reconnue) : « reconnue » = la langue vient de la personne elle-même (requête ou message détecté), la
        seule mémorisée sur le fil (`Turn.lang_seen`)."""
        lang, recognized, _signal = self.resolve_lang_signal(thread_id, sender, explicit, text)
        return lang, recognized

    def resolve_lang_signal(self, thread_id: str, sender: str | None, explicit: str | None,
                            text: str | None) -> tuple[str, bool, bool]:
        """(langue, reconnue, signal) : même langue que `resolve_lang` ; `signal` est faux seulement quand elle retombe sur le
        DÉFAUT du code (`fr`, ni requête, ni compte, ni message détecté, ni langue du fil, ni `NOKKER_LANG`)."""
        explicit = norm_lang(explicit) or request_lang()
        if explicit:
            return resolve_lang(explicit=explicit), True, True
        account = None
        if self.account_lang is not None and sender:
            try:
                account = self.account_lang(sender)
            except Exception:  # compte illisible : on retombe sur le message, puis le fil
                logger.warning("langue du compte illisible", exc_info=True)
        if norm_lang(account):
            return resolve_lang(account=account), False, True
        detected = detect_lang(text)
        if detected:
            return resolve_lang(detected=detected), True, True
        thread = norm_lang(self.thread_lang(thread_id))
        installation = installation_lang()
        return resolve_lang(thread=thread, installation=installation), False, bool(thread or installation)

    def run_turn(self, inp: TurnInput, stream: TurnStream | None = None) -> Turn:
        """Un tour, dans la langue de la personne : `TurnInput.lang` (sinon résolue ici, `resolve_lang`) est posée pour la
        durée du tour comme langue des textes fixes (libellés de règles, pannes), puis restaurée ; le contexte envoyé au
        modèle ne la voit jamais.

        Voir `_run_turn_serial` pour le reste."""
        if stream is None and self.stream_upstream:
            stream = _MUTE_STREAM
        explicit = norm_lang(inp.lang)
        text = None if inp.control else inp.user_text
        if explicit:
            lang, recognized = explicit, inp.lang_recognized
            signal = inp.lang_signal or recognized
            if not signal:  # langue posée à la soumission (objectif, agent, canal) : réel signal ou simple repli ?
                again, _r, sig = self.resolve_lang_signal(inp.thread_id, inp.sender, None, text)
                signal = sig and again == explicit
        else:
            # Un tour de contrôle (btw, objectif, boucle) porte un texte écrit par le client : ni détecté ni mémorisé.
            lang, recognized, signal = self.resolve_lang_signal(inp.thread_id, inp.sender, None, text)
            recognized = recognized and not inp.control
        if inp.lang != lang or inp.lang_recognized != recognized or inp.lang_signal != signal:
            inp = replace(inp, lang=lang, lang_recognized=recognized, lang_signal=signal)
        with lang_scope(lang):
            return self._run_turn_serial(inp, stream)

    def _run_turn_serial(self, inp: TurnInput, stream: TurnStream | None = None) -> Turn:
        """Un tour. `stream` (optionnel, P1 19.10.2026) reçoit le texte des appels modèles en direct, caviardé
        avec retenue (`core/stream.py`) — seulement depuis les providers qui savent streamer ; il ne change
        RIEN au tour lui-même : `Turn.reply` reste la réponse qui fait foi.

        Un tour à la fois par fil, dans l'ordre d'arrivée (`turn_locks`) : un tour demandé pendant qu'un autre tourne
        attend sa fin, prend l'index suivant et voit son contexte."""
        from nokker.tools.scope import thread_scope

        with self.turn_locks.get(inp.thread_id), thread_scope(inp.thread_id), ExitStack() as leases:  # outils offerts selon le fil
            lease_token = _byok_leases.set(leases)  # emprunts BYOK du tour : rendus à la sortie, avant `leases.close()`
            leases.callback(_byok_leases.reset, lease_token)
            if inp.control in FOLLOW_CONTROLS:
                n = self._follow_chain.get(inp.thread_id, 0) + 1
                self._follow_chain[inp.thread_id] = n
                if len(self._follow_chain) > 5000:  # borne mémoire : on oublie les plus anciens fils
                    for k in list(self._follow_chain)[:2500]:
                        self._follow_chain.pop(k, None)
                if self.follow_chain_max and n > self.follow_chain_max:
                    logger.warning("tour de suite refusé : fil %s, %d suites consécutives sans message de la personne (plafond %d, %s)",
                                   inp.thread_id[:12], n, self.follow_chain_max, inp.control)
                    verdict = Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule="loop:follow_chain",
                                      reason=f"{n} tours de suite consécutifs sans message de la personne (plafond {self.follow_chain_max})")
                    return self._limited_turn(inp, self.invariants.check_input(inp.user_text), verdict,
                                              reply=ptx.pick(ptx.FOLLOW_CHAIN, inp.lang or None).format(n=n - 1))
            else:
                self._follow_chain.pop(inp.thread_id, None)  # un message de la personne remet la chaîne à zéro
                if inp.control != APPROVAL_CONTINUE:  # la suite d'une carte, elle, prolonge la chaîne (appels, tests verts)
                    self._chain.pop(inp.thread_id, None)
            if inp.control != APPROVAL_CONTINUE:
                self.approval_repeat.reset(inp.thread_id)  # idem pour la chaîne de cartes identiques
            capture = None
            if self.secret_capture is not None:
                try:
                    capture = self.secret_capture.intercept(inp.thread_id, inp.sender, inp.user_text)
                except Exception:  # noqa: BLE001 — jamais de valeur au journal ; le scrubber d'entrée reste en place
                    logger.warning("capture des secrets du chat en échec")
                    capture = None
                if capture is not None:
                    import dataclasses as _dc

                    known = getattr(self.invariants, "known", None)
                    if capture.value and known is not None:
                        known.add("sites-chat-capture", capture.value)
                    inp = _dc.replace(inp, user_text=capture.text)
            # 00. BYOK cloud : point d'accès du client pour ce tour. Formule sans inférence Nokker (`byok`) et aucun point
            # d'accès utilisable : refusé ici, avant tout appel de modèle et tout décompte (foyer, budget, quota).
            byok = self._byok_route()
            forced = None  # route forcée du fil (Lokkal) : son modèle est celui qui sert le tour
            if self.thread_route is not None:  # choix explicite de la personne pour ce fil : il prime
                try:
                    forced = self.thread_route(inp.thread_id)
                    if forced is not None and not getattr(forced, "from_thread", False):
                        forced = replace(forced, from_thread=True)
                    byok = forced or byok
                except Exception:  # noqa: BLE001 — état de fil illisible : le tour garde le routage habituel
                    logger.warning("modèle du fil illisible", exc_info=True)
            if byok is not None and byok.provider is None:
                verdict = Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule="byok:not_configured",
                                  reason=f"formule sans inférence Nokker : {byok.blocked}", subject="reply")
                return self._limited_turn(inp, self.invariants.check_input(inp.user_text), verdict,
                                          reply=ptx.pick(ptx.BYOK_NOT_CONFIGURED, inp.lang or None),
                                          failure={"code": "byok_not_configured", "message": BYOK_NOT_CONFIGURED_SHORT})
            # 0. Projet d'équipe : le budget est relu avant le tour ; refusé, le tour s'arrête là (rien n'est compté
            # au plafond du compte). Attribué, chaque appel du tour porte le projet au ledger (`spend_tag`).
            # 0 bis. Foyer (X23) : quota commun et plafond du membre, comptés en tours dans le ledger ; un tour d'un
            # membre porte l'étiquette du foyer, en plus de celle d'un projet d'équipe s'il y en a une.
            house = self.household.check(inp.thread_id, inp.sender) if self.household is not None else None
            if house is not None and house.verdict is not None:
                return self._limited_turn(inp, self.invariants.check_input(inp.user_text), house.verdict,
                                          reply=house.reply)
            ticket = self.project_budget.check(inp.thread_id, inp.sender) if self.project_budget is not None else None
            if ticket is not None and ticket.verdict is not None:
                return self._limited_turn(inp, self.invariants.check_input(inp.user_text), ticket.verdict,
                                          reply=ticket.reply)
            # 0 ter. Plafond de dépense de la session (API hôte) : relu au ledger, refusé ici sans appel modèle.
            capped = self.session_budget.check(inp.thread_id) if self.session_budget is not None else None
            if capped is not None:
                return self._limited_turn(inp, self.invariants.check_input(inp.user_text), capped.verdict,
                                          reply=capped.reply, failure=capped.failure)
            # Modèle servant le tour, lu UNE FOIS ici (préfixe par modèle) : un remplacement des fournisseurs pendant le tour ne le change pas.
            if forced is not None:  # modèle forcé du fil : le préfixe est celui de ce modèle (id exact), pas celui du défaut
                m = getattr(getattr(forced, "provider", None), "model", None)
                serving = m if isinstance(m, str) and m else None
            else:
                serving = None if byok is not None else serving_model(self.providers, Tier.SHIP, Tier.DEEP)
            with spend_tag(merge_tags(ticket.tag if ticket is not None else None,
                                      house.tag if house is not None else None)), \
                    byok_turn_scope(byok), turn_model_scope(serving):
                return self._run_turn(inp, stream, ticket, house, capture=capture)

    def _byok_route(self) -> Any:
        """Point d'accès du client pour ce tour (BYOK cloud), ou None : le Router et les paliers de la config. Emprunté
        (`route_scope`) jusqu'à la fin du tour : le service ne ferme pas le client d'un tour en vol."""
        if self.byok is None:
            return None
        try:
            leases, scope = _byok_leases.get(), getattr(self.byok, "route_scope", None)
            if scope is not None and leases is not None:
                return leases.enter_context(scope())
            return self.byok.route_for_turn()
        except Exception:  # noqa: BLE001 — réglages illisibles : le tour garde les paliers (jamais la clé au journal)
            logger.warning("byok : point d'accès du tour illisible", exc_info=True)
            return None

    def _provider_for(self, tier: Tier) -> Provider:
        """Fournisseur d'un appel : celui du client pour un tour BYOK (quel que soit le palier), sinon le palier."""
        route = byok_active()
        return route.provider if route is not None else self.providers[tier]

    def _run_turn(self, inp: TurnInput, stream: TurnStream | None = None, budget_ticket: Any = None,
                  household_ticket: Any = None, capture: Any = None) -> Turn:
        # 1. Entrée : caviardage + drapeaux de domaine AVANT tout enregistrement.
        if inp.control == APPROVAL_CONTINUE and hasattr(self.invariants, "check_kernel_input"):
            # Bloc écrit par le noyau : même caviardage que le résultat d'outil lu directement (motifs forts).
            checked = self.invariants.check_kernel_input(inp.user_text)
        else:
            checked = self.invariants.check_input(inp.user_text)
        if inp.control == APPROVAL_CONTINUE:
            # Suite d'une carte décidée : le bloc `[Approbation]` est écrit par le noyau (décision déjà prise par la
            # personne sur la carte) ; aucun drapeau de domaine sur sa propre relance (montant d'un pay_request…).
            checked.domain_flags = set()
            checked.origin = "approval"
        elif inp.control == "phone_fetch":  # P11 : le geste de la personne, la page jointe par le noyau
            checked.origin = "phone_fetch"
        elif inp.control == "phone_continue":  # P7b : suite d'une tâche du téléphone, écrite par le noyau
            checked.origin = "phone_continue"
        elif inp.control == "desktop_continue":  # B2 : suite d'un pas du bureau, écrite par le noyau
            checked.origin = "desktop_continue"
        # 1 bis. Plafond du compte (testeurs seulement) : au-delà, réponse claire, sans rappel ni appel modèle. Membre
        # d'un foyer : le quota est celui du foyer (0 bis), seule la garde de coût du compte reste (`shared`).
        shared = household_ticket is not None
        # Tour BYOK : hors du quota du Router, compté à part sous la garde de débit (`limits:byok_turns_per_day`).
        # Un modèle de fil servi par le compte relié (`own_key` faux) garde le quota du Router : pas une clé de la personne.
        byok_kw = {"byok": True} if byok_active() is not None and byok_active().own_key else {}
        over = self.limits.check_turn(inp.sender, shared=shared, **byok_kw) if self.limits is not None else None
        if over is not None:
            return self._limited_turn(inp, checked, over)
        # 1 ter. Serveurs MCP INDISPENSABLES de la session (`mcp_required`) : l'un ne répond pas = le tour échoue ICI, avant le
        # rappel, le contexte et tout appel modèle (jamais un tour qui continue « de tête » sans ses outils).
        required = session_opts.mcp_required(inp.thread_id)
        if required:
            missing = self._required_mcp_missing(required)
            if missing:
                return self._mcp_required_failed(inp, checked, missing)
        # 2 (lancé tôt). Rappel du tour : part maintenant, en parallèle de la préparation du contexte ci-dessous,
        # attendu au point d'usage (`_await_recall`). Le texte est déjà caviardé.
        recall_started = time.monotonic()
        recall_future = None
        if checked.text.strip() and checked.text.strip().lower() != "/new" \
                and not _session_mode.registry().kernel_memory_off(inp.thread_id):  # `memory: off|host` : aucun rappel pour ce fil
            # `recall_for` (optionnel) : un port qui sait quel fil appelle (mémoire de projet des sessions locales, X1).
            recall_for = getattr(self.memory, "recall_for", None)
            recall_future = submit_in_context(_RECALL_POOL, recall_for, inp.thread_id, checked.text, self.recall_k) \
                if recall_for is not None else submit_in_context(_RECALL_POOL, self.memory.recall, checked.text,
                                                                 self.recall_k)
        turn = Turn(thread_id=inp.thread_id, index=self.store.next_index(inp.thread_id), input=checked,
                    channel=inp.channel, sender=inp.sender, lang=inp.lang or "", lang_signal=inp.lang_signal,
                    lang_seen=(inp.lang or "") if inp.lang_recognized else "", surface=inp.surface)
        if self.questions is not None and not inp.control and checked.text.strip():
            self._close_question(inp.thread_id, checked.text)
        # Images jointes (X7) : références seules, déjà validées et rangées au magasin par la frontière (API) ; le nom
        # de fichier, seul texte venu du client, passe par le caviardage comme le reste.
        turn.images = [ImageRef(sha256=i.sha256, media_type=i.media_type, size=i.size,
                                name=self.invariants.scrub_output(i.name) if i.name else "") for i in inp.images]
        # Contexte reçu (bulle transférée, message d'une autre session), passé par le scrubber de sortie :
        # il vient d'un autre fil, jamais un secret n'entre par là.
        injected = self._observe("context_for", inp.thread_id) or []
        with self._notes_lock:
            notes = self._owner_notes.pop(inp.thread_id, [])
        turn.injected = notes + [self.invariants.scrub_output(str(b)) for b in injected]
        turn.attached = inp.attached  # déjà caviardé et borné par la frontière (API)
        turn.texts = [dict(t) for t in inp.texts]  # pièces de l'appareil, déjà caviardées par la frontière (API)
        if self.provenance is not None:
            self.provenance.begin_turn(turn)
        self._observe("turn_started", turn)
        # Never clear (docs/NEVER-CLEAR.md) : crochets OPTIONNELS du Context Engine v1 — frontières d'entrée (`/new`,
        # silence), fenêtre calculée au lieu de tout le journal. Un engine v0 ne les porte pas : comportement inchangé.
        before = getattr(self.context, "before_turn", None)
        if before is not None:
            try:
                before(inp.thread_id, checked.text)
            except Exception:  # la vue du contexte ne casse jamais un tour
                logger.exception("context.before_turn a levé")
        if getattr(self.context, "new_episode", None) is not None and checked.text.strip().lower() == "/new":
            turn.reply = ("Nouvel épisode ouvert — rien n'est perdu : la mémoire de la session reste disponible "
                          "(recall_context).")
            turn.messages.append(Message(role="assistant", content=turn.reply))
            self.store.append(turn)
            self._after_turn(turn)
            self._observe("turn_done", turn)
            return turn
        if capture is not None and getattr(capture, "direct_reply", None):
            # Mot de passe ou code capté (cas armé, ou réponse à la proposition d'enregistrement) : réponse du noyau,
            # sans appel du modèle ; le fil garde le placeholder (docs/CONNECTED-SITES.md §4.6).
            turn.reply = capture.direct_reply
            turn.messages.append(Message(role="assistant", content=turn.reply))
            self.store.append(turn)
            self._after_turn(turn)
            self._observe("turn_done", turn)
            return turn
        if self.goals is not None:
            try:
                direct = self.goals.before_turn(self, turn, inp)
            except Exception:  # noqa: BLE001 — les objectifs en panne ne cassent jamais un tour
                logger.warning("objectifs : before_turn en échec", exc_info=True)
                direct = None
            if direct is not None:  # commande `/goal`, ou `goal_check` noté par le correcteur : sans appel modèle
                turn.reply = self.invariants.scrub_output(direct)
                turn.messages.append(Message(role="assistant", content=turn.reply))
                self.store.append(turn)
                self._after_turn(turn)
                self._observe("turn_done", turn)
                return turn
        window = getattr(self.context, "history_window", None)
        history = window(inp.thread_id) if window is not None else self.store.history(inp.thread_id)
        ctx = ToolContext(thread_id=inp.thread_id, turn_index=turn.index, channel=inp.channel, sender=inp.sender,
                          kernel=self, turn=turn)

        # 2. Rappel du tour (bloc 5) : calculé une fois, par le noyau, sans attendre que le modèle y pense (A2).
        recall = self._await_recall(turn, recall_future, recall_started)
        loaded = self._initial_tools(checked.text, history)
        state = RouteState(turn=turn, call_index=0)
        last_errors: list[str] = []
        reply = ""
        # Mémo des résultats d'outils READ déjà obtenus DANS CE TOUR (clé = nom + arguments canoniques).
        # Un effet READ rejoué à l'identique renvoie le résultat déjà admis (réservoir/caviardage déjà
        # appliqués), sans ré-exécuter la source ni compter un nouveau verdict d'action (§_tool_step) —
        # mesuré en réel (P1-S1, 17.10.2026) : `memory_get` rappelé 2x sur la MÊME note, `infra_disk_usage`
        # rappelé plusieurs fois une fois le résultat déjà connu. Jamais pour un effet d'écriture (WRITE,
        # DESTRUCTIVE, SEND_*, SPEND, SECRET) : un doublon là est une DÉCISION du modèle, pas du bruit.
        tool_result_cache: dict[tuple[str, str], ToolResult] = {}
        limits = tool_loop_guard.resolve(session_opts.tool_loop_guard_registry().get(turn.thread_id), self.tool_loop_guard)
        loop_guard = ToolLoopGuard(limits) if limits is not None else None  # état du tour : boucles d'outils de lecture
        failed_call_ids: set[str] = set()  # appels du tour en échec ou refusés (gardes de fin de tour : note_claims, action_claims)
        truncations = 0
        text_toolcall_retried = False  # une seule relance par TOUR (compteur du tour, pas de l'appel)
        think_cut = False  # une coupure du tour a eu lieu réflexion active : jamais d'affichage du texte coupé
        cheap_relaunch = False  # le prochain appel repart sans réflexion, plafond bas (relance après coupure en réflexion)
        truncated_reported = False
        reviews = 0  # renvois de relecture faits ce tour (X8)
        outcome = None  # (objectif, rubrique) d'une tâche notée dans le tour (docs/OUTCOMES-GOAL.md)
        if self.goals is not None:
            try:
                outcome = self.goals.rubric_for_turn(turn, inp)
            except Exception:  # noqa: BLE001
                logger.warning("objectifs : rubrique du tour illisible", exc_info=True)
        outcome_passes = 0  # passes de reprise faites ce tour sur la note du correcteur
        seen_calls: set[str] = set()  # signatures d'appels (stall_signature) déjà vues ce tour
        pending_tools: set[str] = set()  # outils dont une carte attend déjà la décision dans ce tour
        stalls = 0
        # Plafond d'appels de CE tour : `max_calls`, réduit par le plafond cumulé de la chaîne (`chain_max_calls`) ; une
        # chaîne dont les tests sont déjà verts (`conclude_on_green`, commande exécutée sur une carte) conclut sans appel.
        chain = self._chain_state(inp.thread_id) if (self.chain_max_calls or self.conclude_on_green) else None
        cap = self.max_calls
        if chain is not None and self.chain_max_calls:
            cap = min(cap, max(0, self.chain_max_calls - chain["calls"]))
        green_end = chain is not None and self.conclude_on_green and chain["green"] and inp.control == APPROVAL_CONTINUE
        if green_end:
            cap = 0
        notice_at = (max(1, int(cap * self.budget_notice_at))
                     if self.budget_notice_at and cap > 4 else None)

        resumed = False  # le tour a été en pause depuis le dernier appel modèle (mention de reprise)
        dropped: list[Steer] = []
        self.controls.begin(inp.thread_id)
        try:
            self._cancelled.discard(inp.thread_id)  # une annulation d'avant ce tour ne le vise pas
            self._keep_partial.discard(inp.thread_id)
            for call_index in range(cap):
                state.call_index = call_index
                if chain is not None:
                    chain["calls"] += 1
                if call_index == notice_at:
                    turn.messages.append(Message(role="user", content=mt.pick(mt.BUDGET).format(
                        left=cap - call_index, total=cap)))
                # Point de contrôle avant l'appel modèle : pause, puis annulation (aussi pendant la pause), puis steer.
                resumed = self._pause_point(turn, "model") or resumed
                if self._take_cancel(inp.thread_id):
                    reply = ptx.pick(ptx.CANCEL, inp.lang or None)
                    turn.error = "cancelled"
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                stop = self.stop_check(turn) if self.stop_check is not None else None
                if stop:
                    reply = ptx.pick(ptx.STOPPED, inp.lang or None).format(stop=stop)
                    turn.error = "stopped"
                    turn.failure = {"code": "stopped", "message": str(stop)}
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                      rule="loop:stopped", reason=stop)])
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                self._take_steers(turn, resumed)
                resumed = False
                # 3. Palier pour CET appel.
                route = self._choose_route(state)
                state.current_tier = route.tier
                assembled = self.context.assemble(turn, history, recall, loaded, route.tier)
                route, assembled = self._fit_context(turn, route, assembled,
                                                     lambda t: self.context.assemble(turn, history, recall, loaded, t))
                state.current_tier = route.tier
                relaunch_cap: int | None = None
                if cheap_relaunch:  # relance après une coupure en réflexion : pour CET appel seulement
                    cheap_relaunch = False
                    route = replace(route, reasoning=False)
                    relaunch_cap = self.truncation_retry_max_tokens
                self._report_unavailable(turn)
                # 4. Appel modèle, clé de session = fil (affinité de cache, F5).
                # P1-S1 (diag latence, 17.10.2026) : un amont Ship qui n'a pas de repli (catalogue Router,
                # un seul amont servable pour qwen/qwen3.8-27b) peut échouer malgré les réessais internes
                # du provider (budget total dépassé, voir providers/sokkan.py) — un palier qui ne répond
                # jamais ne doit JAMAIS laisser le tour sans réponse : on dégrade proprement (escalade,
                # trace au ledger) au lieu de laisser l'exception remonter en tour vide (`turn.error` sans
                # `turn.reply`, bug trouvé en mesurant ce chantier : l'ancien chemin `except Exception`
                # en fin de fonction ne fixait jamais `reply`).
                comp = None
                live: ScrubbedDeltas | None = None
                failure: Exception | None = None
                for attempt_route in self._routes_with_fallback(route):
                    if attempt_route is not route:
                        # Palier demandé indisponible (vu en réel 19.10 : Deep → HTTP 502 du Router) : on retombe
                        # sur Ship pour CET appel au lieu d'escalader vers l'humain avec une page d'erreur.
                        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                          rule="router:fallback",
                                                          reason=f"palier {route.tier.value} indisponible "
                                                                 f"({_short_error(failure)}) → "
                                                                 f"{attempt_route.tier.value}")])
                        self._report_route_fallback(turn, route, attempt_route, failure)
                        route = attempt_route
                        state.current_tier = route.tier
                        assembled = self.context.assemble(turn, history, recall, loaded, route.tier)
                    try:
                        comp, live = self._complete(self._provider_for(route.tier), assembled, route, inp.thread_id,
                                                    stream, turn, max_tokens_cap=relaunch_cap)
                        break
                    except TurnCancelled:
                        raise  # l'utilisateur a annulé : ni repli de palier ni escalade
                    except Exception as e:  # noqa: BLE001 — un amont indisponible n'est pas un bug du noyau
                        failure = e
                        if quota_reached(e) is not None:
                            break  # quota d'inférence atteint : pas une panne, aucun repli de palier (le même quota s'applique)
                if comp is None and byok_active() is not None:
                    # BYOK : ni repli vers le Router ni escalade ; l'erreur du point d'accès du client, lisible.
                    name = byok_active().name
                    why = _short_error(failure)
                    why_loc = person_failure_label(failure)[0]
                    turn.failure = {"code": "byok_unavailable", "message": f"modèle « {name} » injoignable : {why}",
                                    "person_message": ptx.pick(ptx.ERROR_DOWN).format(why=why_loc)}
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0,
                                                      rule="byok:unavailable", reason=turn.failure["message"])])
                    if byok_active().from_thread:
                        reply = thread_model_down_reply(name, failure)
                    else:
                        reply = byok_down_reply(name, failure)
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                quota = quota_reached(failure) if comp is None else None
                if quota is not None:
                    # Quota d'inférence atteint : le tour échoue UNE fois, proprement, avec la phrase de quota. Ni
                    # escalade (personne à prévenir), ni « réessaie », ni mémoire de palier en panne.
                    reply = self._quota_failure(turn, quota)
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                if comp is None:
                    turn.fallback, turn.notice = None, ""  # aucune réponse : pas de « mode allégé » à annoncer
                    reason = f"palier {route.tier.value} indisponible : {_short_error(failure)}"
                    why_loc = person_failure_label(failure)[0]
                    turn.failure = {"code": "tier_unavailable", "message": reason,
                                    "person_message": ptx.pick(ptx.ERROR_DOWN).format(why=why_loc)}
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=0,
                                                      rule="router:unavailable", reason=reason)])
                    logger.warning("palier indisponible : %s", reason)  # cause brute au journal, jamais à la personne
                    reply = (self._cannot_continue(turn, ptx.pick(ptx.REASON_DOWN).format(why=why_loc)) + " "
                             + ptx.pick(ptx.RETRY))
                    turn.escalated = True
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                if comp.attempts > 1:
                    # Visibilité (pas un filet de sûreté) : l'amont a mis plus d'un essai à répondre —
                    # journalisé au ledger pour qu'un opérateur voie un amont qui se dégrade AVANT qu'il
                    # ne finisse par échouer complètement (cf. ci-dessus).
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                      rule="router:retried",
                                                      reason=f"{comp.attempts} tentative(s) palier "
                                                             f"{route.tier.value}")])
                self._record_call(turn, assembled, comp, route, route.reason)
                msg = comp.message

                if msg.tool_calls:
                    if live is not None:
                        live.discard()  # texte d'un appel qui finit en outils : pas la réponse
                    turn.messages.append(Message(role="assistant", content=msg.content, tool_calls=msg.tool_calls,
                                                 thinking_blocks=list(getattr(msg, "thinking_blocks", None) or [])))
                    terminal_text = None
                    stall_note: str | None = None
                    for tc in msg.tool_calls:
                        if terminal_text is not None and tc.name in pending_tools:  # tour clos sur une carte déjà en attente
                            turn.messages.append(Message(role="tool", content=mt.pick(mt.PENDING_SKIPPED), tool_call_id=tc.id, name=tc.name))
                            continue
                        # Point de contrôle avant chaque outil : une pause arrête ici, l'outil part à la reprise.
                        resumed = self._pause_point(turn, "tool", tc.name) or resumed
                        if inp.thread_id in self._cancelled:  # annulé : les appels restants ne partent pas
                            turn.messages.append(Message(role="tool", content=mt.pick(mt.CALL_CANCELLED),
                                                         tool_call_id=tc.id, name=tc.name))
                            continue
                        self._observe("tool_call", turn, tc)
                        t0 = time.monotonic()
                        result = self._tool_step(tc, turn, ctx, loaded, tool_result_cache, loop_guard)
                        self._observe("tool_result", turn, tc, result, int((time.monotonic() - t0) * 1000))
                        if result.images:
                            self._attach_tool_images(turn, result)
                        turn.messages.append(Message(role="tool", content=result.content + result.loop_note,
                                                     tool_call_id=tc.id, name=tc.name))
                        if result.is_error or result.refused:
                            failed_call_ids.add(tc.id)
                        if chain is not None:
                            self.note_outcome(inp.thread_id, tc.name, tc.arguments, result)
                        if tc.name in (ESCALATE_TOOL, CLARIFY_TOOL):
                            # Un humain lit la RÉPONSE du tour, jamais l'argument d'un outil : `clarify`
                            # termine le tour sur sa question, au même principe que `escalate_to_human`
                            # (cf commit 2f8d35c) — sinon le modèle peut très bien répondre, à l'appel
                            # suivant, par un texte de remplissage qui ne redonne jamais la question
                            # (vu en réel : « En attente de ta réponse — j'ai posé la question ci-dessus
                            # via `clarify`. », sans la question elle-même).
                            terminal_text = result.content
                            if tc.name == CLARIFY_TOOL and self.questions is not None:
                                self._ask_question(turn, tc)
                        if result.reason == APPROVAL_REPEAT_RULE:
                            # Même action validée `approval_repeat_max` fois de suite : pas de carte de plus, le tour
                            # conclut pour la personne seule (le modèle ne reçoit que APPROVAL_REPEAT_SKIPPED).
                            if terminal_text is None:
                                terminal_text = ptx.pick(ptx.APPROVAL_REPEAT, turn.lang or None).format(
                                    name=safe_name(tc.name), n=self.approval_repeat_max)
                            pending_tools.add(tc.name)
                        elif (result.content or "").startswith(PENDING_MARKS):
                            if tc.name in pending_tools and terminal_text is None:
                                # 2e carte du même outil dans ce tour : rien de plus à proposer avant la décision (vu en réel :
                                # 48 `desktop_act` en attente, un toutes les 7 s). Le tour se termine ; la personne décide.
                                lg = norm_lang(turn.lang) or current_lang()  # langue du tour, sinon du contexte, sinon fr
                                terminal_text = ptx.pick(ptx.PENDING_END, lg).format(what=human_tool(tc.name, lg))
                            pending_tools.add(tc.name)
                        if result.is_error:
                            state.tool_failures += 1
                            sig = error_signature(result)
                            state.repeated_error = state.repeated_error or sig in last_errors
                            last_errors.append(sig)
                        if self.stall_limit:
                            ssig = stall_signature(tc, result)
                            if ssig in seen_calls:
                                stalls += 1
                                if stalls == 1 or stalls == max(2, self.stall_limit // 2):
                                    stall_note = mt.pick(mt.STALL).format(name=tc.name)
                            seen_calls.add(ssig)
                    if terminal_text is not None:
                        late = self._settle_late_cards(turn)
                        if late:  # carte tranchée entre-temps : pas de « en attente » périmé, le tour continue avec la décision
                            for block in late:
                                turn.messages.append(Message(role="user", content=block))
                            continue
                        reply = terminal_text
                        turn.messages.append(Message(role="assistant", content=reply))
                        break
                    if chain is not None and self.conclude_on_green and chain["green"] and not turn.escalated:
                        # Tests verts après une écriture : la tâche est faite, le noyau conclut (banc de parité P01 :
                        # 128 appels après des tests verts).
                        reply = self._conclude(turn, history, recall, state, inp.thread_id, stream, "loop:tests_green",
                                               "tests verts après une écriture, conclusion sans outils")
                        break
                    if self.stall_limit and stalls >= self.stall_limit and not turn.escalated:
                        # Le tour ne progresse plus : conclure maintenant plutôt que de brûler le reste du budget.
                        reply = self._conclude(turn, history, recall, state, inp.thread_id, stream, "loop:stalled",
                                               f"{stalls} appels d'outils répétés à l'identique, conclusion sans outils")
                        break
                    if stall_note is not None:
                        turn.messages.append(Message(role="user", content=stall_note))
                    continue

                cut_mark = ""
                if getattr(comp, "finish_reason", None) == "length":
                    # Réflexion ACTIVE à cet appel : demandée (`route.reasoning`, ce que `_complete` envoie au provider, donc
                    # le palier `reasoning_on_body`/`reasoning_off_body`) OU rendue par l'amont (`comp.reasoning`). Alors le
                    # `content` coupé peut être la fin du raisonnement : ce n'est pas une réponse.
                    thinking = bool(route.reasoning) or bool(str(getattr(comp, "reasoning", "") or "").strip())
                    think_cut = think_cut or thinking  # collant : après une coupure en réflexion, aucun texte coupé du tour n'est gardé
                    if truncations < self.truncation_retries:
                        truncations += 1
                        if live is not None:
                            live.discard()
                        if think_cut:  # le texte coupé reste hors de l'historique ; la relance est sans réflexion
                            turn.messages.append(Message(role="user", content=mt.pick(mt.LENGTH_CUT_THINK)))
                            cheap_relaunch = True
                        else:  # vraie réponse coupée : elle reste dans l'historique, le modèle continue
                            turn.messages.append(Message(role="assistant", content=msg.content or ""))
                            turn.messages.append(Message(role="user", content=mt.pick(mt.LENGTH_CUT)))
                        continue
                    # Relances épuisées.
                    if not truncated_reported:
                        truncated_reported = True
                        self._observe_opt("turn_truncated", turn, {"reasoning": think_cut, "retries": truncations})
                    if think_cut:
                        # Échec propre : jamais le texte coupé (fin de raisonnement possible), la phrase de la personne.
                        if live is not None:
                            live.discard()
                        text = ptx.pick(ptx.LENGTH_FAILED, turn.lang or None)
                        turn.failure = {"code": "length_truncated", "message": "réponse coupée par la limite de longueur "
                                        f"après {truncations} relances, réflexion active", "person_message": text}
                        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0,
                                                          rule="loop:truncated",
                                                          reason=f"coupée par la limite de longueur ({truncations} "
                                                                 "relances), réflexion active : texte non affiché")])
                        reply = text
                        turn.messages.append(Message(role="assistant", content=reply))
                        break
                    cut_mark = mt.pick(mt.LENGTH_MARK)
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                      rule="loop:truncated",
                                                      reason=f"coupée par la limite de longueur ({truncations} "
                                                             "relances), affichée avec une marque")])

                if (not text_toolcall_retried and assembled.tools
                        and getattr(comp, "finish_reason", None) != "length"):
                    kind = text_toolcall_kind(msg.content)
                    if kind is not None:
                        # Appel d'outil écrit en texte : jamais interprété ni exécuté. Le même appel repart UNE fois, sans rien
                        # ajouter à l'historique (le texte fautif n'y entre pas) ; la relance compte dans `max_calls`.
                        text_toolcall_retried = True
                        if live is not None:
                            live.discard()
                        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                          rule="loop:text_toolcall",
                                                          reason="appel d'outil écrit en texte : relance")])
                        self._observe_opt("turn_text_toolcall_retry", turn, {"kind": kind})
                        continue

                # 7. Porte bascule avant d'envoyer.
                candidate = msg.content or ""
                if cut_mark:  # marque rangée avec la réponse (historique et réponse affichée)
                    candidate = f"{candidate.rstrip()}\n\n{cut_mark}".strip()
                late = self._settle_late_cards(turn)
                if late:
                    # Une carte restée « en attente » dans ce tour a été tranchée avant la réponse : la réponse candidate
                    # (qui dit « en attente ») n'entre PAS dans l'historique ; le modèle reprend avec la décision.
                    if live is not None:
                        live.discard()
                    for block in late:
                        turn.messages.append(Message(role="user", content=block))
                    continue
                verdicts = self.gates.switch(candidate, turn, route.tier)
                self._log_verdicts(turn, verdicts)
                decision = worst(verdicts)
                if (decision is Decision.REDO_DEEP and route.tier is not Tier.DEEP
                        and session_opts.escalation_allowed(turn.thread_id)):  # `escalate: false` : pas de relance en Deep
                    # Relance : la réponse candidate n'entre PAS dans l'historique (préfixe et cache intacts).
                    if live is not None:
                        live.discard()
                    state.switch_verdicts = verdicts
                    continue
                if decision in (Decision.ESCALATE, Decision.HITL, Decision.DENY) and not turn.escalated:
                    logger.info("escalade par une porte : fil=%s motif=%s", turn.thread_id,
                                "; ".join(f"{v.rule}: {v.reason}" for v in verdicts if v.decision is decision))
                    candidate = self.escalator.escalate(turn, person_why_all(verdicts, decision))
                    turn.escalated = True
                # X8 : le juge relit le diff du tour avant « terminé » ; à corriger → l'auteur reprend avec les
                # remarques (la réponse candidate reste dans l'historique : c'est la sienne, il la complète).
                note = self._review(turn, candidate, reviews, call_index)
                if note is not None:
                    reviews += 1
                    if live is not None:
                        live.discard()
                    turn.messages.append(Message(role="assistant", content=candidate))
                    turn.messages.append(Message(role="user", content=note))
                    continue
                if (self.reviewer is not None and call_index + 2 > cap
                        and reviews < int(getattr(self.reviewer, "rounds", 1))):
                    # Dernier appel du tour : plus de place pour un renvoi, la relecture se fait quand même (sans renvoi).
                    candidate = self._final_review(turn, candidate, "réponse au dernier appel du tour")
                if outcome is not None and not turn.escalated and candidate.strip():
                    # Rubrique de la tâche : le correcteur note dans sa propre fenêtre ; sous le seuil, une passe de
                    # plus avec son retour (la candidate reste dans l'historique : l'agent la complète).
                    can_retry = call_index + 2 <= cap and not (
                        self.stop_check is not None and self.stop_check(turn))
                    try:
                        onote, candidate = self.goals.in_turn(self, turn, outcome[0], outcome[1], candidate,
                                                              outcome_passes, can_retry)
                    except Exception:  # noqa: BLE001 — un correcteur en panne ne casse jamais un tour
                        logger.warning("objectifs : note du tour en échec", exc_info=True)
                        onote = None
                    if onote is not None:
                        outcome_passes += 1
                        if live is not None:
                            live.discard()
                        turn.messages.append(Message(role="assistant", content=candidate))
                        turn.messages.append(Message(role="user", content=onote))
                        continue
                    outcome = None
                reply = candidate
                turn.messages.append(Message(role="assistant", content=reply))
                break
            else:
                # Limite d'appels atteinte : conclusion sans outils (`_conclude`).
                state.call_index = self.max_calls
                if green_end:
                    reply = self._conclude(turn, history, recall, state, inp.thread_id, stream, "loop:tests_green",
                                           "tests verts après une écriture, conclusion sans outils", reply)
                else:
                    reply = self._conclude(turn, history, recall, state, inp.thread_id, stream, "loop:max_calls",
                                           (f"{self.max_calls} appels, conclusion sans outils" if cap == self.max_calls
                                            else f"{self.chain_max_calls} appels cumulés sur la chaîne de tours, "
                                                 "conclusion sans outils"), reply)
        except TurnCancelled as cancelled:
            # Annulé pendant un appel modèle en flux (D6) : la réponse HTTP de l'amont est déjà fermée. Le texte partiel
            # de cet appel n'est PAS gardé (ni dans `messages`, ni dans la réponse) : il n'a pas passé la porte bascule,
            # sa retenue de caviardage n'est pas sortie, et des arguments d'outil à moitié écrits ne font pas un appel.
            # Le client a reçu un reset (`ScrubbedDeltas.discard`), puis la réponse finale « Annulé à ta demande. ».
            # Exception, à la demande de l'hôte (`cancel(keep_partial=True)`) : le texte DÉJÀ STREAMÉ (caviardé, sans la
            # retenue de fin) est gardé, suivi d'une ligne qui le dit interrompu ; la réponse finale est ce texte.
            keep = inp.thread_id in self._keep_partial
            self._keep_partial.discard(inp.thread_id)
            self._take_cancel(inp.thread_id)
            reply = ptx.pick(ptx.CANCEL, inp.lang or None)
            partial = str(getattr(cancelled, "partial", "") or "").strip()
            if keep and partial:
                reply = f"{partial}\n\n{ptx.pick(ptx.CANCEL_PARTIAL, inp.lang or None)}"
            turn.error = "cancelled"
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="loop:cancelled_stream",
                                              reason="annulé pendant un appel modèle en flux, réponse amont fermée")])
            turn.messages.append(Message(role="assistant", content=reply))
        except Exception as e:  # noqa: BLE001 — le tour est enregistré même en échec
            turn.error = f"{type(e).__name__}: {e}"
            turn.failure = {"code": "internal_error", "message": turn.error}
        finally:
            dropped = self.controls.end(inp.thread_id)
        for s in dropped:  # arrivés trop tard (réponse finale déjà en cours) : le client les renvoie en message normal
            self._observe_opt("turn_steer_dropped", turn, {"steer_id": s.id, "text": s.text})

        # 8. Sortie : scrubber sur tout ce qui part.
        # 8 pre. « C'est noté » sans note écrite : texte fixe, dans la langue du tour (`note_claims`).
        fixed = self._end_of_turn_guards(turn, inp, reply, failed_call_ids)
        if fixed != reply:  # l'historique relu au tour suivant ne garde pas la version fausse
            for m in reversed(turn.messages):
                if m.role == "assistant" and m.content == reply:
                    m.content = fixed
                    break
            reply = fixed
        turn.reply = self.invariants.scrub_output(reply)
        # 8 bis. Quota dépassé en mode soft : l'avertissement suit la réponse (une fois par fenêtre). Hors de
        # `messages` : le modèle ne le relit pas au tour suivant. Le chunk final du flux porte `turn.reply`.
        try:
            notice = self.limits.notice(inp.sender, shared=shared) \
                if self.limits is not None and not byok_kw else None
            if household_ticket is not None and notice is None:
                notice = self.household.notice(household_ticket)  # foyer en mode soft : quota passé, une fois
        except Exception:  # noqa: BLE001 — un avertissement manqué ne casse jamais le tour
            logger.warning("avertissement de quota en échec", exc_info=True)
            notice = None
        if notice:
            turn.reply = f"{turn.reply}\n\n{notice}"
        # 8 ter. Budget du projet : avertissement quand CE tour fait passer 80 % du plafond (une fois).
        if budget_ticket is not None:
            try:
                project_notice = self.project_budget.notice(budget_ticket, turn.calls)
            except Exception:  # noqa: BLE001 — un avertissement manqué ne casse jamais le tour
                logger.warning("avertissement du budget de projet en échec", exc_info=True)
                project_notice = None
            if project_notice:
                turn.reply = f"{turn.reply}\n\n{project_notice}"
        # 8 quater. Capture spontanée d'un mot de passe : proposition d'enregistrement, hors contexte du modèle.
        if capture is not None and getattr(capture, "notice", None):
            turn.reply = f"{turn.reply}\n\n{capture.notice}" if turn.reply else capture.notice
        # 9. Fin de tour : append-only. L'extraction de faits → quarantaine arrive avec A-3 (memory.propose).
        self._seal_thinking(turn)
        self.store.append(turn)
        self._trace("finish", turn)  # traces d'entraînement du tour (docs/TRACES.md), après le tour qui fait foi
        self._after_turn(turn)
        self._observe("turn_done", turn)
        if self.goals is not None:  # objectif du fil : dépense, note, arrêt ou tour suivant (docs/OUTCOMES-GOAL.md)
            try:
                self.goals.after_turn(self, turn, inp)
            except Exception:  # noqa: BLE001 — jamais bloquant
                logger.warning("objectifs : after_turn en échec", exc_info=True)
        # 10. Ressources tenues pendant le tour (onglets du navigateur…) rendues par les sources qui le déclarent.
        tools_done = getattr(self.tools, "turn_done", None)
        if callable(tools_done):
            try:
                tools_done(turn.thread_id)
            except Exception:  # noqa: BLE001
                logger.warning("turn_done des outils en échec", exc_info=True)
        # 11. Hooks `turn_end` (Nokker Lokkal, lot X3, nokker/tools/hooks.py) : seulement si le registre est entouré des
        # hooks ; le tour fini (réponse caviardée) leur est passé, rien ne revient dans le tour.
        ended = getattr(self.tools, "turn_ended", None)
        if callable(ended):
            try:
                ended(turn)
            except Exception:  # noqa: BLE001 — un hook en panne ne casse jamais un tour
                logger.warning("hooks turn_end en échec", exc_info=True)
        # 12. Apprentissage (drapeau `learning_nudges`) : tour réussi et riche en outils → une ligne fixe au tour suivant.
        if self.learning is not None:
            try:
                line = self.learning.after_turn(turn)
            except Exception:  # jamais bloquant
                logger.warning("invitation d'apprentissage en échec", exc_info=True)
                line = None
            if line:
                self.add_owner_note(turn.thread_id, line)
        return turn

    def _limited_turn(self, inp: TurnInput, checked, verdict: Verdict, *, reply: str | None = None,
                      failure: dict[str, Any] | None = None) -> Turn:
        """Tour refusé par un plafond (compte, ou budget du projet : `reply` donné) : gardé dans l'historique avec son
        verdict, sans appel modèle. `failure` : l'échec à annoncer (`turn.done.error`), ex. BYOK non configuré."""
        turn = Turn(thread_id=inp.thread_id, index=self.store.next_index(inp.thread_id), input=checked,
                    channel=inp.channel, sender=inp.sender, lang=inp.lang or "", lang_signal=inp.lang_signal,
                    lang_seen=(inp.lang or "") if inp.lang_recognized else "", surface=inp.surface)
        if failure is not None:
            turn.failure = failure
        self._observe("turn_started", turn)
        self._log_verdicts(turn, [verdict])
        if reply is None:
            reply = self.limits.reply(verdict) if self.limits is not None else ""
        turn.reply = reply
        turn.messages.append(Message(role="assistant", content=turn.reply))
        self.store.append(turn)
        self._after_turn(turn)
        self._observe("turn_done", turn)
        return turn

    def _ask_question(self, turn: Turn, tc: ToolCall) -> None:
        """`clarify` enrichi (drapeau `host_clarify_options`) : le brouillon posé par l'outil sur le tour est inscrit au
        tableau des questions et annoncé aux clients. Jamais bloquant : en panne, la question reste la réponse en texte."""
        d = turn.question
        if not isinstance(d, dict) or "question_id" in d:
            return  # outil non exécuté (refusé par une porte), ou question déjà inscrite pour ce tour
        try:
            q, _old = self.questions.ask(turn.thread_id, turn.index, tc.id, d, sender=turn.sender, channel=turn.channel)
        except Exception:  # noqa: BLE001
            logger.warning("question à choix non inscrite", exc_info=True)
            turn.question = None
            return
        turn.question = q.view()
        self._observe_opt("question_asked", turn, q.view())

    def _close_question(self, thread_id: str, text: str) -> None:
        """Un message de la personne répond à la question en attente du fil (réponse en texte, hors route)."""
        try:
            self.questions.close_for_turn(thread_id, text)  # annoncée par le tableau (`on_closed`)
        except Exception:  # noqa: BLE001
            logger.warning("question à choix non close", exc_info=True)

    @staticmethod
    def _seal_thinking(turn: Turn) -> None:
        """Range la réflexion de la dernière réponse (sans appel d'outil) sur le message final du tour : la chaîne des blocs
        rejoués (providers/anthropic.py) n'a ainsi aucun trou entre deux tours d'outils. Sans effet sans réflexion."""
        last = turn.messages[-1] if turn.messages else None
        if turn.final_thinking and last is not None and last.role == "assistant" and not last.tool_calls \
                and not last.thinking_blocks:
            last.thinking_blocks = list(turn.final_thinking)

    def _after_turn(self, turn: Turn) -> None:
        """Comptage, délestage ou clôture d'épisode (Context Engine v1) — jamais bloquant pour le tour."""
        after = getattr(self.context, "after_turn", None)
        if after is None:
            return
        try:
            after(turn)
        except Exception:
            logger.exception("context.after_turn a levé")

    def _initial_tools(self, text: str, history: list) -> list[ToolSpec]:
        """Schémas non cœur chargés au premier appel du tour (`tool_loading`, voir tools/select.py)."""
        non_core = self.tools.find("", limit=10_000)
        if self.tool_loading == "all":
            return list(non_core)
        used = {c.name for past in history[-self.tool_reuse_turns:] for m in past.messages for c in m.tool_calls}
        return relevant_tools(text, used, non_core)

    # 5-6. Porte action, exécution, réservoir.
    def _attach_tool_images(self, turn: Turn, result: ToolResult) -> None:
        """Images rendues par un outil (r44 : read_file d'une image) : rangées au magasin et jointes au tour comme des
        pièces jointes — partie image sur un palier avec vision, note sinon (`images/render.py`). Sans magasin : le
        modèle garde la ligne de description du résultat."""
        if self.images is None:
            return
        for img in result.images:
            try:
                ref = self.images.put(turn.thread_id, img)
                turn.images.append(replace(ref, origin=f"tool:{result.name}"))
            except Exception:  # noqa: BLE001 — une image non rangée ne casse pas le tour
                logger.warning("image d'outil non rangée (%s)", result.name, exc_info=True)

    def _note_tool_removed(self, thread_id: str) -> bool:
        """`memory_note` retiré du catalogue de ce fil par l'hôte (`deny_tools`, `memory: off|host`) : la garde « noté » se tait."""
        if _session_mode.registry().kernel_memory_off(thread_id):
            return True
        reg, seen = self.tools, 0
        while reg is not None and not callable(getattr(reg, "hidden_for", None)) and seen < 8:
            reg, seen = getattr(reg, "inner", None), seen + 1
        hidden = getattr(reg, "hidden_for", None)
        try:
            return callable(hidden) and note_claims.TOOL in hidden(thread_id)
        except Exception:  # noqa: BLE001 — illisible : la garde garde son comportement d'avant
            return False

    def _end_of_turn_guards(self, turn: Turn, inp: TurnInput, reply: str, failed_ids: set[str]) -> str:
        """Gardes de la réponse finale (étape 8 pre) : « noté » sans note, « fait » sans action (drapeau
        `action_claims_guard`), nom technique d'outil dans le texte. Jamais bloquant : une garde en erreur laisse la réponse telle quelle."""
        lang = inp.lang or None
        cont = bool(turn.input.origin)  # suite d'une carte décidée, d'un geste du téléphone ou du bureau : l'action a tourné hors tour
        try:
            reply = note_claims.guard(turn.messages, reply, lang, self.tools.spec, failed_ids, cont,
                                      user_text=None if cont else inp.user_text,
                                      tool_removed=self._note_tool_removed(turn.thread_id))
            if self._action_claims_on(turn):
                reply = action_claims.guard(turn.messages, reply, lang, self.tools.spec, failed_ids, cont)
            reply = tool_name_scrub.scrub(reply, self._tool_names(turn), inp.user_text or "", lang)
        except Exception:
            logger.warning("gardes de fin de tour en échec", exc_info=True)
        return reply

    def _policy_attr(self, name: str, default: Any) -> Any:
        """Attribut de la politique de palier, à travers ses enveloppes (`LimitedPolicy`, `ForcedTierPolicy` : `inner`)."""
        pol, seen = self.policy, 0
        while pol is not None and not hasattr(pol, name) and seen < 6:
            pol, seen = getattr(pol, "inner", None), seen + 1
        return getattr(pol, name, default) if pol is not None else default

    def _deep_reasoning(self, thread_id: str | None) -> bool:
        """Réflexion demandée sur le palier deep : celle de la politique (`route.deep_reasoning`, vrai par défaut) ; la session qui a posé
        `deep_reasoning: false` ne l'impose pas : le raisonnement suit alors celui de Ship (`ship_reasoning`)."""
        if session_opts.deep_reasoning_forced(thread_id):
            return bool(self._policy_attr("deep_reasoning", True))
        return bool(self._policy_attr("ship_reasoning", False))

    def _deep_decision(self, thread_id: str | None, reason: str) -> RouteDecision:
        return RouteDecision(tier=Tier.DEEP, reasoning=self._deep_reasoning(thread_id), reason=reason)

    def _choose_route(self, state: RouteState) -> RouteDecision:
        """Palier de l'appel d'après la politique ; une montée vers deep ne force la réflexion que si la session ne l'a pas retirée."""
        route = self.policy.choose(state)
        if route.tier is Tier.DEEP and route.reasoning and not session_opts.deep_reasoning_forced(state.turn.thread_id):
            return replace(route, reasoning=self._deep_reasoning(state.turn.thread_id))
        return route

    def _action_claims_on(self, turn: Turn) -> bool:
        """Garde « c'est fait » active pour ce tour : le réglage du palier qui a répondu (dernier appel), sinon le réglage global."""
        tier = turn.calls[-1].tier if turn.calls else None
        if tier is not None and self.tier_action_claims.get(getattr(tier, "value", str(tier))) is not None:
            return bool(self.tier_action_claims[getattr(tier, "value", str(tier))])
        return self.action_claims_guard

    def _tool_names(self, turn: Turn) -> tuple[set[str], bool]:
        """(noms d'outils du catalogue du tour, session de code) : outils appelés, cœur et index du registre."""
        names = {tc.name for m in turn.messages for tc in m.tool_calls}
        try:
            names |= {s.name for s in self.tools.core()} | {n for n, _d in self.tools.index()}
        except Exception:
            logger.warning("noms d'outils du tour illisibles", exc_info=True)
        return names, bool(names & tool_name_scrub.CODE_TOOLS)

    def _tool_step(self, tc: ToolCall, turn: Turn, ctx: ToolContext, loaded: list[ToolSpec],
                   seen: dict[tuple[str, str], ToolResult] | None = None,
                   guard: ToolLoopGuard | None = None) -> ToolResult:
        if tc.name in MEMORY_TOOLS:
            done = sum(1 for m in turn.messages for c in m.tool_calls if c.name in MEMORY_TOOLS and c.id != tc.id)
            if done >= self.memory_calls_per_turn:
                return ToolResult(call_id=tc.id, name=tc.name, content=(
                    mt.pick(mt.MEMORY_BUDGET).format(n=self.memory_calls_per_turn)))
        if tc.name == FIND_TOOL:
            found = self.tools.find(str(tc.arguments.get("query", "")))
            known = {s.name for s in loaded}
            loaded.extend(s for s in found if s.name not in known)
            listing = [{"name": s.name, "description": s.description} for s in found]
            return ToolResult(call_id=tc.id, name=tc.name, content=json.dumps(listing, ensure_ascii=False))

        # Valeur masquée sur l'appareil (P9b) : un marqueur ne sort jamais du fil — refus AVANT toute porte et toute
        # exécution, sans carte (escalade comprise : son motif part chez un humain, hors du fil).
        masked = masked_value_refusal(tc.name, self.tools.spec(tc.name), tc.arguments)
        if masked is not None:
            return ToolResult(call_id=tc.id, name=tc.name, content=masked, reason=MASKED_VALUE, refused=True)

        if tc.name == ESCALATE_TOOL:
            text = self.escalator.escalate(turn, scrub_internal_ids(str(tc.arguments.get("reason", ""))))
            turn.escalated = True
            return ToolResult(call_id=tc.id, name=tc.name, content=text)

        spec = self.tools.spec(tc.name)
        if spec is not None and not spec.core and all(s.name != spec.name for s in loaded):
            loaded.append(spec)  # appelé sans schéma : exécuté (portes comprises) ET chargé pour la suite du tour
            # Sauf si un champ `required` manque ou est vide : le modèle a inventé les arguments sans voir le schéma. Erreur qui cite
            # les champs attendus, aucune exécution, aucune porte, aucun verdict ; le schéma est chargé (ligne ci-dessus), donc un
            # 2e appel invalide rend l'erreur de la source, pas celle-ci (`core/deferred_args.py`).
            wrong = deferred_args_error(spec.name, spec.parameters, tc.arguments)
            if wrong is not None:
                return ToolResult(call_id=tc.id, name=tc.name, content=wrong, is_error=True)
        if spec is not None:  # carte et exécution portent les mêmes arguments (empreinte `args_sha256`)
            tc.arguments = spec.normalized(turn.thread_id, tc.arguments)
        effect = call_effect(spec, tc.arguments)
        # Appel identique (même nom, mêmes arguments) déjà fait DANS CE TOUR : pour un effet READ
        # seulement (jamais WRITE/DESTRUCTIVE/SEND_*/SPEND/SECRET — un doublon là est une décision du
        # modèle, pas du bruit), renvoie le résultat déjà obtenu (déjà caviardé/admis) sans ré-exécuter
        # la source ni compter un nouveau verdict d'action — mesuré en réel (P1-S1, 17.10.2026) :
        # `memory_get` et `infra_disk_usage` rappelés plusieurs fois avec les mêmes arguments une fois
        # le résultat déjà connu.
        cache_key = (tc.name, json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)) if seen is not None \
            else None
        if getattr(spec, "fresh", False):
            cache_key = None  # journal d'un processus, liste des processus : relus à chaque appel
        # Garde-fou contre les boucles de lecture (`core/tool_loop_guard.py`) : les outils à effet et les lectures `fresh`
        # (journal d'un processus) n'en relèvent jamais.
        loop_guard = guard if effect is Effect.READ and not getattr(spec, "fresh", False) else None
        if loop_guard is not None and loop_guard.suspended(tc.name):
            return ToolResult(call_id=tc.id, name=tc.name,
                              content=mt.pick(mt.TOOL_LOOP_SUSPENDED).format(tool=safe_name(tc.name)))
        if effect is Effect.READ and cache_key is not None and cache_key in seen:
            cached = seen[cache_key]
            again = ToolResult(call_id=tc.id, name=tc.name,
                               content=cached.content + mt.pick(mt.ALREADY_CALLED),
                               is_error=cached.is_error, reservoir_id=cached.reservoir_id,
                               secret_handles=cached.secret_handles)
            return self._loop_guard_note(loop_guard, turn, tc, again, cached.content)

        verdicts = self.gates.action(tc, spec, turn)
        if self.provenance is not None:  # en tête : sa règle est celle que nomme la carte ou le refus
            verdicts = self.provenance.check(tc, spec, turn) + verdicts
        self._log_verdicts(turn, verdicts)
        decision = worst(verdicts)
        if decision is Decision.ALLOW:
            result = self.tools.call(tc.name, tc.arguments, ctx)
            result.call_id = tc.id
            # Caviardage AVANT le contexte et le store : la garantie ne dépend ni du modèle ni de la source d'outils.
            redacted = self.invariants.redact_result(result, spec)
            if self.provenance is not None:
                self.provenance.record(turn, tc, spec, redacted)
            # Producteurs de bulles : le résultat CAVIARDÉ mais complet — `admit_result` peut le ranger au
            # réservoir et n'en garder qu'un extrait (vu sur le canari 18.10 : web_search sans bulle).
            suffix = self._observe("tool_executed", turn, tc, spec, redacted)
            admit_in = getattr(self.context, "admit_result_in", None)
            admitted = (admit_in(turn.thread_id, turn.index, redacted) if admit_in is not None
                        else self.context.admit_result(redacted))
            if suffix:
                admitted.content = admitted.content + str(suffix)
            if effect is Effect.READ and cache_key is not None:
                seen[cache_key] = admitted
                return self._loop_guard_note(loop_guard, turn, tc, admitted, admitted.content)
            if loop_guard is not None:  # lecture `fresh` : exclue plus haut, jamais ici ; lecture sans cache : comptée aussi
                return self._loop_guard_note(loop_guard, turn, tc, admitted, admitted.content)
            if seen is not None and effect is not Effect.READ:
                # Un appel à effet a pu changer ce que les lectures renvoient (fichier patché puis relu, événement
                # créé puis listé) : les lectures déjà faites ce tour ne valent plus.
                seen.clear()
            return admitted
        rule = next((v for v in verdicts if v.decision is decision), None)
        why = f"{rule.rule}: {rule.reason}" if rule else decision.value
        if decision is Decision.HITL:
            if self.approvals is not None and self.approval_repeat.blocked(
                    turn.thread_id, tc.name, tc.arguments, self.approval_repeat_max):
                self._log_verdicts(turn, [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=0,
                                                  rule=APPROVAL_REPEAT_RULE,
                                                  reason=f"{self.approval_repeat_max} cartes identiques validées de suite")])
                return ToolResult(call_id=tc.id, name=tc.name, content=mt.pick(mt.APPROVAL_REPEAT_SKIPPED).format(n=self.approval_repeat_max),
                                  reason=APPROVAL_REPEAT_RULE)
            turn.pending_approvals.append(tc)
            dup = self._pending_duplicate(turn, tc)
            if dup is not None:  # même action déjà en attente d'une décision : pas de 2e carte (une écriture, une exécution)
                turn.approval_ids.append(dup.id)
                return ToolResult(call_id=tc.id, name=tc.name, content=self._pending_text(why, dup.id))
            if self.approvals is not None:
                # P1 (18.10.2026) : carte PROUVÉE (docs/APPROVALS.md), pas seulement une mention dans
                # `pending_approvals`. `advice` : avis du juge sur une carte forte (X8, `advisor`), None sinon
                # (accompagne, ne bloque rien).
                auto = self._auto_review_eligible(rule, tc, effect)
                if auto:
                    approval = self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule), notify=False)
                    reviewed = self._auto_review(turn, tc, spec, ctx, approval, effect)
                    if reviewed is not None:
                        return reviewed
                else:
                    approval = self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule))
                turn.approval_ids.append(approval.id)
                decided = self._await_decision(turn, tc, spec, approval)
                if decided is not None:
                    return decided
                return ToolResult(call_id=tc.id, name=tc.name, content=self._pending_text(why, approval.id))
            return ToolResult(call_id=tc.id, name=tc.name, content=self._with_lang(mt.pick(mt.PENDING_BARE).format(why=why)))
        if decision is Decision.ESCALATE:
            logger.info("escalade par une porte : fil=%s motif=%s", turn.thread_id, why)
            text = self.escalator.escalate(turn, person_why(rule, decision))
            turn.escalated = True
            return ToolResult(call_id=tc.id, name=tc.name, content=mt.pick(mt.ESCALATED).format(why=why, text=text))
        return ToolResult(call_id=tc.id, name=tc.name, content=refused_text(refusal_why(rule, decision)) + " "
                          + _refusal_advice(rule, decision), is_error=False, refused=True,
                          reason=(rule.code or rule.rule) if rule else None)

    def _await_decision(self, turn: Turn, tc: ToolCall, spec: Any, approval: Any) -> ToolResult | None:
        """Carte qui vient d'être posée : le tour attend sa décision au plus `approval_wait_s` secondes. Décidée à temps, le
        résultat de l'outil (ou le refus) devient le résultat de cet appel et le tour continue ; None : toujours en attente
        (délai, annulation, réglage éteint), le chemin habituel (« en attente », suite de la décision) s'applique."""
        if self.approval_wait_s <= 0 or self.approval_inline is None:
            return None
        try:
            out = self.approval_inline(approval, self.approval_wait_s, lambda: turn.thread_id in self._cancelled)
        except Exception:  # une attente qui plante laisse la carte au chemin habituel
            logger.warning("attente de la décision de la carte en échec", exc_info=True)
            return None
        if out is None:
            return None
        if tc in turn.pending_approvals:  # décidée : plus rien n'attend l'humain pour cet appel
            turn.pending_approvals.remove(tc)
        if out.kind == "rejected":
            who = out.approval.decided_by or mt.pick(mt.CARD_UNKNOWN_DECIDER)
            reason = self._card_reason(approval.id)
            if reason:  # motif donné par la personne : donnée de l'hôte, encadrée comme un commentaire
                content = mt.pick(mt.CARD_DECLINED_REASON).format(who=who, approval_id=approval.id, reason=reason)
            else:
                content = mt.pick(mt.CARD_DECLINED).format(who=who, approval_id=approval.id)
            return ToolResult(call_id=tc.id, name=tc.name, refused=True, reason="declined", content=content,
                              decision_reason=reason)
        result = out.result if out.result is not None else ToolResult(call_id=tc.id, name=tc.name, content=out.content,
                                                                      is_error=True)
        result.call_id = tc.id
        suffix = self._observe("tool_executed", turn, tc, spec, result)
        admit_in = getattr(self.context, "admit_result_in", None)
        admitted = (admit_in(turn.thread_id, turn.index, result) if admit_in is not None
                    else self.context.admit_result(result))
        if suffix:
            admitted.content = admitted.content + str(suffix)
        return admitted

    def _card_reason(self, approval_id: str) -> str | None:
        """Motif donné avec le refus de la carte, nettoyé une dernière fois (caviardage du noyau compris), ou None."""
        from nokker.approvals.reasons import clean_reason

        get = getattr(self.approvals, "decision_reason", None)
        try:
            return clean_reason(get(approval_id), self.invariants.scrub_output) if callable(get) else None
        except Exception:  # un motif illisible ne casse pas le refus
            logger.warning("motif du refus de la carte %s illisible", approval_id, exc_info=True)
            return None

    def _settle_late_cards(self, turn: Turn) -> list[str]:
        """Filet avant la réponse finale : une carte restée « en attente » dans CE tour peut avoir été tranchée entre-temps.
        Rend les blocs `[Approbation]` des cartes qu'on vient de prendre (outil exécuté ou refus consigné, une seule fois) :
        le tour continue avec eux au lieu de publier un « en attente » périmé. Vide : rien n'a changé."""
        if self.approval_wait_s <= 0 or self.approval_inline is None or self.approvals is None:
            return []
        blocks: list[str] = []
        seen: set[str] = set()
        for m in list(turn.messages):
            if m.role != "tool" or not (m.content or "").startswith(PENDING_MARK):
                continue
            found = APPROVAL_ID_IN_RESULT.search(m.content or "")
            if found is None or found.group(1) in seen:
                continue
            seen.add(found.group(1))
            try:
                card = self.approvals.get(found.group(1))
                if card is None or card.status not in ("approved", "rejected"):
                    continue
                out = self.approval_inline(card, 0.0, None)
            except Exception:  # le filet ne casse jamais le tour
                logger.warning("relecture de la carte %s avant la réponse en échec", found.group(1), exc_info=True)
                continue
            if out is not None:
                blocks.append(out.block)
        return blocks

    def _loop_guard_note(self, guard: ToolLoopGuard | None, turn: Turn, tc: ToolCall, result: ToolResult,
                         raw: str) -> ToolResult:
        """Compte une lecture dans le garde-fou du tour (`raw` = le résultat sans note) : à `note_after` appels stagnants du
        même outil, une note est ajoutée au résultat ; à `suspend_after` l'outil est suspendu pour la suite du tour. Journal
        DEBUG et événement hôte `tool_loop_guard {tool, n, action}` : jamais d'argument ni de résultat."""
        if guard is None:
            return result
        action, n = guard.after(tc.name, tc.arguments, raw)
        if action is None:
            return result
        logger.debug("garde-fou outils : fil=%s outil=%s n=%d action=%s", turn.thread_id, safe_name(tc.name), n, action)
        if action == "suspend" or n == guard.limits.note_after:
            self._observe_opt("tool_loop_guard", turn, {"tool": safe_name(tc.name), "n": n, "action": action})
            # Persisté au ledger (verdict `loop:tool_loop_guard`, comptable par fil/tour/horodatage ; aucune migration) :
            # outil, n, action — jamais d'argument ni de résultat.
            try:
                self._log_verdicts(turn, [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=0,
                                                  rule="loop:tool_loop_guard", subject=tc.id,
                                                  reason=f"tool={safe_name(tc.name)} n={n} action={action}")])
            except Exception:  # noqa: BLE001 — la trace ne casse jamais le tour
                logger.debug("garde-fou outils : verdict non écrit")
        # Copie : le résultat d'origine reste dans le mémo du tour sans la note ; la note part dans le message `tool` seulement.
        return replace(result, loop_note=mt.pick(mt.TOOL_LOOP_NOTE).format(tool=safe_name(tc.name), n=n))

    def _pending_text(self, why: str, approval_id: str) -> str:
        return self._with_lang(mt.pick(mt.PENDING).format(why=why, approval_id=approval_id))

    def _with_lang(self, text: str) -> str:
        """Texte d'attente d'une carte + rappel de langue (drapeau `reply_language_tail`) : le message d'attente du modèle
        suit ce résultat, loin de la ligne de langue de l'en-tête. Ajouté EN FIN : le début reconnu (`PENDING_MARKS`) et le
        préfixe de cache ne bougent pas."""
        if getattr(self.context, "reply_language_tail", False):
            return f"{text} {mt.pick(mt.PENDING_LANG)}"
        return text

    def _pending_duplicate(self, turn: Turn, tc: ToolCall) -> Any:
        """Carte déjà en attente pour CETTE action (outil + empreinte des arguments) sur ce fil, ou None."""
        find = getattr(self.approvals, "pending_duplicate", None)
        if not callable(find):
            return None
        try:
            return find(turn.thread_id, tc.name, args_sha256(tc.arguments))
        except Exception:  # sans dédoublonnage, le comportement est celui d'avant
            logger.exception("recherche d'une carte identique en attente impossible (fil %s)", turn.thread_id)
            return None

    def _routes_with_fallback(self, route: RouteDecision) -> list[RouteDecision]:
        """Le palier choisi, puis Ship en repli s'il est différent et configuré (jamais l'inverse : Ship est le
        plancher, son indisponibilité escalade). Tour BYOK : un seul point d'accès, aucun repli."""
        if route.tier is Tier.SHIP or Tier.SHIP not in self.providers or byok_active() is not None:
            return [route]
        return [route, RouteDecision(tier=Tier.SHIP, reasoning=route.reasoning,
                                     reason=f"repli : {route.tier.value} indisponible")]

    def _wire_messages(self, provider: Provider, assembled, thread_id: str, turn: Turn | None) -> list[dict]:
        """Messages tels qu'ils partent (X7) : parties `nokker_image` du Context Engine changées en `image_url` (palier
        avec vision) ou en note texte (sans vision). Des images du tour courant non lues : verdict `images:not_read` au
        ledger et événement `attachment.skipped` à l'utilisateur, une fois par tour ; celles des tours précédents,
        sans vision, deviennent leur ligne de délestage, sans rien signaler."""
        vision = bool(getattr(provider, "vision", False))
        probe = getattr(provider, "vision_probe", None)
        if not vision and probe is not None and has_images(assembled.messages):
            # Vision `auto` (§7.6) : UNE sonde, ici seulement (un tour qui porte une image), jamais en fond.
            vision, fresh = probe.check(provider)
            if fresh and turn is not None:
                self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                  rule="images:vision_probe",
                                                  reason=f"sonde de vision {getattr(provider, 'model', '?')} : "
                                                         f"{'oui' if vision else 'non'} ({probe.last_reason})")])
        # Images non lues signalées pour CE tour seulement ; celles des tours d'avant sont délestées en une ligne.
        messages, skipped = materialize(assembled.messages, vision=vision, blobs=self.images, thread_id=thread_id,
                                        current_turn=turn.index if turn is not None else None)
        if skipped and turn is not None and not any(v.rule == "images:not_read" for v in turn.verdicts):
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="images:not_read",
                                              reason=f"{skipped} image(s) non envoyée(s) : modèle sans vision "
                                                     f"({getattr(provider, 'model', '?')})")])
            if self.observer is not None and hasattr(self.observer, "attachment_skipped"):
                self._observe("attachment_skipped", turn, skipped, "no_vision")
        # Filet du fil (lot fix-batch-1) : coffre + motifs forts sur TOUT ce qui part, y compris les blocs qui n'ont pas
        # de porte à eux (plan, mémoire de projet, profil, arguments d'outils rejoués). Un amont qui refuse un secret
        # (Router : `400 secret_detected`) casserait le tour entier ; un texte sans secret part à l'identique.
        redact_context = getattr(self.invariants, "redact_context", None)
        if callable(redact_context):
            messages = redact_context(messages)
        if not getattr(provider, "replays_thinking", False):
            messages = strip_thinking(messages)  # réflexion d'un fournisseur Anthropic : jamais vers un autre
        return messages

    def _loop_max_tokens(self, provider: Any) -> int | None:
        """Plafond de sortie que la boucle impose : son défaut (`max_tokens`), sauf si le palier en pose un
        (`tiers.<nom>.max_tokens`, `max_tokens_pinned`) : alors la valeur du palier est celle de l'appel."""
        return None if getattr(provider, "max_tokens_pinned", False) else self.max_tokens

    def _fit_context(self, turn: Turn, route: RouteDecision, assembled, reassemble: Callable[[Tier], Any]):
        """Un palier qui déclare sa fenêtre (`max_context_tokens`, palier `nina` 32 k) ne reçoit jamais un appel qui n'y
        tient pas : bascule sur Ship (verdict `router:context`), le prompt est réassemblé pour Ship."""
        provider = self.providers.get(route.tier)
        if provider is None or route.tier is Tier.SHIP or Tier.SHIP not in self.providers or byok_active() is not None:
            return route, assembled
        from nokker.route.nina import fits

        ok, need, limit = fits(provider, assembled.messages, assembled.tools, self._loop_max_tokens(provider))
        if ok:
            return route, assembled
        reason = f"{route.reason} ; contexte ~{need} jetons > {limit} ({route.tier.value}) → ship"
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0, rule="router:context",
                                          reason=reason)])
        ship = RouteDecision(tier=Tier.SHIP, reasoning=False, reason=reason)
        return ship, reassemble(Tier.SHIP)

    def _shadow(self, turn: Turn | None, route: RouteDecision, messages, tools, comp) -> None:
        """Ombre de Nina (route/nina.py) : premier appel du tour seulement, jamais quand Nina répond déjà ; sans effet."""
        if self.nina_shadow is None or turn is None or turn.calls or route.tier is Tier.NINA:
            return
        try:
            self.nina_shadow.maybe_run(turn, messages, tools, comp, route.tier.value, getattr(comp, "model", ""))
        except Exception:  # noqa: BLE001 — l'ombre ne casse jamais un tour
            logger.warning("nina ombre : en échec", exc_info=True)

    def _complete(self, provider: Provider, assembled, route: RouteDecision, thread_id: str,
                  stream: TurnStream | None, turn: Turn | None = None, max_tokens_cap: int | None = None):
        """Un appel modèle (en streaming si le client l'a demandé et que le provider sait le faire). Un flux
        coupé APRÈS le premier morceau (`StreamInterrupted`, vu en réel 19.10) : reset du client puis UN rejeu
        sans streaming. Toute autre erreur remonte (repli de palier ou escalade chez l'appelant)."""
        messages = self._wire_messages(provider, assembled, thread_id, turn)
        live = None
        tools_live: LiveToolInputs | None = None
        kwargs = {}
        if stream is not None and accepts_on_delta(provider):
            # Retenue : base + début de secret connu en fin de texte (`secret_tail`), au lieu de 2 × le plus long secret.
            live = ScrubbedDeltas(self.invariants.scrub_output, stream, DEFAULT_HOLDBACK,
                                  known=getattr(self.invariants, "known", None))
            kwargs["on_delta"] = self._cancellable(live.feed, thread_id)
            tools_live = self._live_tool_inputs(provider, assembled, turn)
            if tools_live is not None:
                kwargs["on_tool_delta"] = self._cancellable(tools_live.feed, thread_id)
            think_sink = getattr(stream, "reasoning", None) if self.reasoning_stream else None
            if callable(think_sink) and accepts_on_delta(provider, "on_reasoning_delta"):
                # Réflexion en direct : même caviardage avec retenue que la réponse, vers un champ séparé du flux.
                think_live = ScrubbedDeltas(self.invariants.scrub_output,
                                            _ReasoningSink(think_sink, len(turn.calls) if turn is not None else 0),
                                            DEFAULT_HOLDBACK, known=getattr(self.invariants, "known", None))
                kwargs["on_reasoning_delta"] = self._cancellable(think_live.feed, thread_id)
        cap = self._loop_max_tokens(provider)
        budget: dict[str, Any] = {"max_tokens": cap} if cap else {}
        if max_tokens_cap:  # relance après une coupure en réflexion : plafond bas, même pour un palier à `max_tokens` fixé
            own = getattr(provider, "max_tokens", None) if getattr(provider, "max_tokens_pinned", False) else cap
            budget["max_tokens"] = min(int(own), max_tokens_cap) if isinstance(own, int) and own else max_tokens_cap
        if accepts_on_delta(provider, "stable_prefix"):
            # Provider qui pose lui-même ses points de cache (format anthropic) : le préfixe stable du Context Engine
            # lui est désigné. Aucun autre provider ne déclare ce mot-clé : leurs appels ne changent pas.
            stable = getattr(self.context, "stable_prefix", None)
            try:
                budget["stable_prefix"] = stable() if callable(stable) else None
            except Exception:  # noqa: BLE001 — sans préfixe désigné, le provider ne marque que les outils
                budget["stable_prefix"] = None
        self._trace("sent", turn, strip_thinking(messages), assembled.tools)  # jamais de réflexion dans les traces
        self._keep_fork_base(turn, assembled)
        # Fork (`context/fork.py`) : l'enfant garde l'affinité de cache du fil parent ; sinon, le fil.
        session = getattr(self.context, "session_key", None)
        session = session if isinstance(session, str) and session else thread_id
        try:
            comp = provider.complete(messages, assembled.tools, reasoning=route.reasoning,
                                     session_key=session, **budget, **kwargs)
        except StreamInterrupted as e:
            logger.warning("flux amont interrompu (%s) : rejeu sans streaming", e)
            if live is not None:
                live.discard()
            if tools_live is not None:
                tools_live.discard()
            comp = provider.complete(messages, assembled.tools, reasoning=route.reasoning,
                                     session_key=session, **budget)
            self._expose_reasoning(turn, comp, stream)
            return comp, None
        except Exception as e:
            if isinstance(e, TurnCancelled) and live is not None:
                e.partial = live.emitted  # déjà streamé et caviardé : gardé si l'hôte l'a demandé (`keep_partial`)
            if live is not None:
                live.discard()
            if tools_live is not None:
                tools_live.discard()
            raise
        if tools_live is not None:
            tools_live.finish()  # arguments complets : la retenue du caviardage sort (le texte caviardé en entier)
        self._expose_reasoning(turn, comp, stream)
        self._shadow(turn, route, messages, assembled.tools, comp)
        self._calibrate_tokens(route, messages, assembled.tools, comp)
        return comp, live

    def _calibrate_tokens(self, route: RouteDecision, messages, tools, comp) -> None:
        """Calibrage de l'estimateur de jetons (`context/tokens.py`, drapeau `calibrate_tokens`, éteint par défaut) :
        rapport `prompt_tokens` réels / estimés de CETTE requête, par palier. Sans effet hors drapeau."""
        budgets = getattr(getattr(self.context, "manager", None), "budgets", None)
        if budgets is None or not getattr(budgets, "calibrate_tokens", False):
            return
        try:
            from nokker.context.tokens import CALIBRATOR, estimate_tokens

            est = estimate_tokens(json.dumps(messages, ensure_ascii=False, default=str)) \
                + estimate_tokens(json.dumps(tools or [], ensure_ascii=False, default=str), "json")
            CALIBRATOR.observe(route.tier.value, est, int(getattr(comp.usage, "prompt", 0) or 0))
        except Exception:  # noqa: BLE001 — une mesure ne casse jamais un tour
            logger.debug("calibrage des jetons ignoré", exc_info=True)

    def _expose_reasoning(self, turn: Turn | None, comp: Any, stream: TurnStream | None) -> None:
        """Réflexion de l'appel qui vient de finir (drapeau `host_reasoning_stream`) : caviardée EN ENTIER, bornée, puis
        remise au client par un champ séparé — puits `reasoning(texte, True, rang de l'appel)` du flux (le texte complet
        remplace les morceaux partis en direct, dont la retenue de caviardage n'est jamais sortie), observateur
        `turn_reasoning`, et `turn.reasoning` pour la réponse non streamée. Elle ne touche ni `comp.message` ni `turn.messages` : rien
        n'en revient au modèle, au store ou à la mémoire. Jamais bloquant pour le tour."""
        if not self.reasoning_stream or turn is None:
            return
        try:
            raw = getattr(comp, "reasoning", "")
            if not isinstance(raw, str) or not raw.strip():
                return
            text = self.invariants.scrub_output(raw.strip())[:REASONING_MAX_CHARS]
            index = len(turn.calls)  # rang de l'appel dans le tour (il n'est pas encore inscrit dans `turn.calls`)
            item = {"call_index": index, "text": text}
            turn.reasoning.append(item)
            sink = getattr(stream, "reasoning", None) if stream is not None else None
            if callable(sink):
                sink(text, True, index)
            self._observe_opt("turn_reasoning", turn, dict(item))
        except Exception:  # noqa: BLE001 — un affichage manqué ne casse jamais un tour
            logger.warning("réflexion non exposée", exc_info=True)

    def _cancellable(self, feed: Callable[..., None], thread_id: str) -> Callable[..., None]:
        """Relais d'un morceau en flux qui vérifie l'annulation AVANT de le remettre (D6) : annulé, il lève
        `TurnCancelled`, ce qui coupe le flux de l'amont au prochain morceau (texte ou arguments d'outil)."""
        cancelled = self._cancelled

        def fed(*args: Any) -> None:
            if thread_id in cancelled:
                raise TurnCancelled(thread_id)
            feed(*args)

        return fed

    def _live_tool_inputs(self, provider: Provider, assembled, turn: Turn | None) -> LiveToolInputs | None:
        """Écriture en direct (X22, docs/BUBBLES.md §6) : seulement pour un tour streamé, un provider qui remet les
        arguments d'outils au fil de l'eau, un appel qui propose des outils, et un observateur qui publie
        `tool.input.delta`. Le texte passe par le caviardage avec retenue, comme la réponse."""
        if (turn is None or self.observer is None or not assembled.tools
                or not hasattr(self.observer, "tool_input_delta")
                or not accepts_on_delta(provider, "on_tool_delta")):
            return None

        def emit(data: dict) -> None:
            self._observe("tool_input_delta", turn, data)

        return LiveToolInputs(self.invariants.scrub_output, emit, holdback_for(self.invariants))

    def _log_verdicts(self, turn: Turn, verdicts: list[Verdict]) -> None:
        for v in verdicts:
            turn.verdicts.append(v)
            self.ledger.record_verdict(turn.thread_id, turn.index, v)


__all__ = ["CANCEL_REPLY", "ESCALATE_TOOL", "FIND_TOOL", "Gate", "Kernel", "TurnCancelled", "worst"]
