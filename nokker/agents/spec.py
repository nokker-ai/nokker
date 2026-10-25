"""Validation des champs d'un agent (docs/BUBBLES.md §9.5) : la même pour `POST /v1/agents`, `PATCH`, et l'activation
d'un `agent_draft`. Tout ce qui sort d'ici est normalisé (dates ISO 8601 UTC, règles relues, noms vérifiés).

Ce que le noyau refuse (400) :
  - un déclencheur inconnu, un cron sans échéance, un fuseau inconnu, une date `once` illisible ;
  - `event` hors `alert:<nom>`, `webhook:<id>`, `mail:<filtre>` ; `mail:<filtre>` n'est accepté que si l'appelant dit
    que la relève du courrier est branchée (`mail=True` : moteur + connecteurs, `nokker/agents/mailwatch.py`), et son
    filtre doit rester dans le sous-ensemble sûr (`parse_mail_filter`) ; sinon refusé avec `NO_MAIL_TRIGGER` (il ne
    tournerait jamais) ;
  - un secret dont le NOM ressemble à une valeur (préfixe de clé connu, longue chaîne sans séparateur) ;
  - une règle `auto_approve` illisible ; une règle d'écriture sur un agent déclenché par un événement ;
  - un placement hors `auto`, `web`, `mc_…` ; sur Lokkal, tout placement autre que cette machine (il se choisit dans
    le deck de nokker.ai, X15) ; chez nokker.ai, une machine d'un autre compte, `web` hors offre (CloudPlacement) ;
  - des `needs` qui ne sont pas des NOMS (dépôts, hôtes, connecteurs, MCP) ou un `gpu` non booléen ;
  - un budget sans plafond, ou avec un plafond négatif.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from nokker.agents.cron import Cron, CronError, zone
from nokker.agents.types import TRIGGERS, AgentError

MAX_INSTRUCTIONS = 20_000
MAX_SECRETS = 32
MAX_RULES = 40
EVENT_KINDS = ("alert", "webhook", "mail")
# `mail:<filtre>` : son producteur est la relève du courrier (`nokker/agents/mailwatch.py`), branchée sur le moteur
# seulement quand l'instance a des connecteurs. Sans elle (pas de moteur, pas de connecteurs), refusé : il ne
# tournerait jamais.
NO_MAIL_TRIGGER = ("trigger.event mail:<filtre> : pas disponible (aucune relève du courrier ne déclenche un agent, il "
                   "ne tournerait jamais) — à la place : un déclencheur schedule dont la mission lit la boîte "
                   "(mail_search), ou webhook:<id> appelé par le service de courrier")
_WEBHOOK_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# Préfixes de clés connus : un NOM qui commence ainsi est très probablement une valeur collée au mauvais endroit.
_VALUE_PREFIXES = ("sk-", "sk_", "pk_", "rk_", "ghp_", "gho_", "ghs_", "github_pat_", "glpat-", "xox", "akia", "eyj",
                   "whsec_", "ya29.", "aiza")


def _looks_like_value(name: str) -> bool:
    low = name.lower()
    if low.startswith(_VALUE_PREFIXES):
        return True
    # longue chaîne sans séparateur, chiffres et lettres mêlés : une clé, pas un nom
    return len(name) >= 24 and not re.search(r"[_.-]", name) and bool(re.search(r"\d", name)) \
        and bool(re.search(r"[A-Za-z]", name))


def parse_when(value: Any) -> float:
    """Date ISO 8601 (avec fuseau ; `Z` accepté) ou epoch → epoch."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise AgentError("trigger.at : date ISO 8601 attendue (ex. 2026-11-02T07:00:00Z)")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise AgentError(f"trigger.at illisible : {value!r}") from None
    if dt.tzinfo is None:
        raise AgentError("trigger.at : préciser le fuseau (Z ou +01:00)")
    return dt.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def norm_trigger(raw: Any, *, mail: bool = False) -> dict[str, Any]:
    """`mail` : la relève du courrier est branchée ici (sinon `mail:<filtre>` est refusé, `NO_MAIL_TRIGGER`)."""
    if raw is None:
        return {"kind": "manual"}
    if not isinstance(raw, dict):
        raise AgentError("trigger doit être un objet {kind, …}")
    kind = str(raw.get("kind") or "")
    if kind not in TRIGGERS:
        raise AgentError(f"trigger.kind inconnu : {kind!r} (manual, schedule, once, event)")
    if kind == "manual":
        return {"kind": "manual"}
    if kind == "schedule":
        tz = str(raw.get("tz") or "UTC")
        try:
            cron = Cron.parse(str(raw.get("cron") or ""))
            z = zone(tz)
        except CronError as e:
            raise AgentError(str(e)) from None
        if cron.next_after(datetime.now(timezone.utc), z) is None:
            raise AgentError(f"cron sans échéance dans les 5 ans : {cron.expr}")
        return {"kind": "schedule", "cron": cron.expr, "tz": tz}
    if kind == "once":
        return {"kind": "once", "at": iso(parse_when(raw.get("at")))}
    event = str(raw.get("event") or "").strip()
    head, sep, tail = event.partition(":")
    if head == "mail" and sep:
        if not mail:
            raise AgentError(NO_MAIL_TRIGGER, code="no_mail_trigger")
        from nokker.agents.mailwatch import parse_mail_filter

        event = "mail:" + parse_mail_filter(tail)[0]
    if not sep or head not in EVENT_KINDS or not tail.strip():
        raise AgentError("trigger.event : alert:<nom>, webhook:<id>" + (" ou mail:<filtre>" if mail else ""))
    if head == "webhook" and not _WEBHOOK_ID.match(tail):
        raise AgentError("trigger.event webhook:<id> : id de 1 à 64 caractères [A-Za-z0-9_.-]")
    if len(event) > 200:
        raise AgentError("trigger.event : 200 caractères au plus")
    return {"kind": "event", "event": event}


def norm_secrets(raw: Any) -> list[str]:
    from nokker.secrets.base import SecretsError, check_name

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AgentError("secrets doit être une liste de NOMS du coffre")
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        try:
            check_name(name)
        except SecretsError:
            raise AgentError(f"secrets : nom invalide {name[:12]!r}… ([A-Za-z0-9][A-Za-z0-9_.-]{{0,127}})") from None
        if _looks_like_value(name):
            raise AgentError("secrets : on attend le NOM d'un secret du coffre, pas sa valeur")
        if name not in out:
            out.append(name)
    if len(out) > MAX_SECRETS:
        raise AgentError(f"secrets : {MAX_SECRETS} noms au plus")
    return out


def norm_rules(raw: Any) -> list[str]:
    from nokker.qa.permissions import Rule

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AgentError("auto_approve doit être une liste de règles outil(motif)")
    out: list[str] = []
    for item in raw:
        try:
            r = Rule.parse(str(item))
        except ValueError as e:
            raise AgentError(f"auto_approve : {e}") from None
        text = f"{r.tool}({r.pattern})" if r.pattern else r.tool
        if text not in out:
            out.append(text)
    if len(out) > MAX_RULES:
        raise AgentError(f"auto_approve : {MAX_RULES} règles au plus")
    return out


def norm_budget(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AgentError("budget doit être un objet {max_calls, max_cost, currency} ou null")
    out: dict[str, Any] = {"max_calls": None, "max_cost": None, "currency": "EUR"}
    if raw.get("max_calls") is not None:
        try:
            n = int(raw["max_calls"])
        except (TypeError, ValueError):
            raise AgentError("budget.max_calls : entier attendu") from None
        if not 1 <= n <= 1000:
            raise AgentError("budget.max_calls : entre 1 et 1000")
        out["max_calls"] = n
    if raw.get("max_cost") is not None:
        try:
            c = float(raw["max_cost"])
        except (TypeError, ValueError):
            raise AgentError("budget.max_cost : nombre attendu") from None
        if not c > 0:
            raise AgentError("budget.max_cost : strictement positif")
        out["max_cost"] = round(c, 6)
    cur = str(raw.get("currency") or "EUR").upper()
    if not _CURRENCY.match(cur):
        raise AgentError("budget.currency : code ISO à 3 lettres (EUR, CHF, USD)")
    out["currency"] = cur
    if out["max_calls"] is None and out["max_cost"] is None:
        raise AgentError("budget : max_calls ou max_cost requis (ou budget null)")
    return out


_MACHINE_ID = re.compile(r"^mc_[0-9a-f]{16}$")
PLACEMENTS = ("auto", "web")  # plus un `mc_…` (X15)


def placement_target(raw: Any) -> str | None:
    """La valeur de `placement.machine` lue et vérifiée dans sa FORME (`auto`, `web`, `mc_` + 16 hex) ; None pour
    `null` ou une machine vide. AgentError sinon."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AgentError("placement doit être un objet {machine: auto | web | mc_…} ou null")
    machine = str(raw.get("machine") or "").strip()
    if not machine:
        return None
    if machine not in PLACEMENTS and not _MACHINE_ID.match(machine):
        raise AgentError(f"placement.machine : « auto », « web » ou l'id d'une machine (mc_…), pas {machine[:40]!r}")
    return machine


def norm_placement(raw: Any, local_machine: str | None, *, policy: Any = None,
                   owner: str | None = None) -> dict[str, Any] | None:
    """`policy` (nokker.ai, X15 : `nokker/cloud/crew.py::CloudPlacement`) décide pour `auto`, `web` et les machines
    du compte. Sans politique (Lokkal) : seule la machine qui porte le noyau (null, ou son propre `mc_…`) ; le reste
    se choisit dans le deck de nokker.ai (un déménagement, LOKKAL §6.7)."""
    if isinstance(raw, dict) and raw.get("moved_at"):
        return dict(raw)  # pierre tombale d'un agent déménagé (posée par le noyau, jamais par un client)
    if local_machine and isinstance(raw, dict) and str(raw.get("machine") or "").strip() == local_machine:
        return {"machine": local_machine}
    machine = placement_target(raw)
    if machine is None:
        return None
    if policy is not None:
        return policy.normalize(machine, owner)
    if machine == "auto":
        raise AgentError("placement « auto » : se choisit dans le deck de nokker.ai (machine liée) — laisser null "
                         "pour exécuter sur cette machine")
    raise AgentError(f"placement sur {machine} : se choisit dans le deck de nokker.ai (déménagement) — ici, seul null "
                     "(cette machine) s'exécute")


def norm_needs(raw: Any) -> dict[str, Any] | None:
    from nokker.cloud.placement import NeedsError, parse_needs

    try:
        return parse_needs(raw).to_json()
    except NeedsError as e:
        raise AgentError(str(e)) from None


def norm_tools(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AgentError("tools doit être une liste")
    return [str(t) for t in raw][:40]


def norm_fields(body: dict[str, Any], *, partial: bool, local_machine: str | None = None, policy: Any = None,
                owner: str | None = None, mail: bool = False) -> dict[str, Any]:
    """Champs reconnus de `body`, validés. `partial` (PATCH) : seulement ceux présents. `mail` : voir `norm_trigger`."""
    out: dict[str, Any] = {}

    def has(key: str) -> bool:
        return key in body if partial else True

    for key in ("name", "role", "model"):
        if has(key) and (not partial or body.get(key) is not None):
            out[key] = str(body.get(key) or "").strip()[:200]
    if not partial and (not out.get("name") or not out.get("role")):
        raise AgentError("name/role requis")
    if partial and ("name" in out and not out["name"] or "role" in out and not out["role"]):
        raise AgentError("name/role ne peuvent pas être vides")
    if has("tools"):
        out["tools"] = norm_tools(body.get("tools"))
    if has("instructions"):
        text = str(body.get("instructions") or "")
        if len(text) > MAX_INSTRUCTIONS:
            raise AgentError(f"instructions : {MAX_INSTRUCTIONS} caractères au plus")
        out["instructions"] = text
    if has("trigger"):
        out["trigger"] = norm_trigger(body.get("trigger"), mail=mail)
    if has("secrets"):
        out["secrets"] = norm_secrets(body.get("secrets"))
    if has("budget"):
        out["budget"] = norm_budget(body.get("budget"))
    if has("auto_approve"):
        out["auto_approve"] = norm_rules(body.get("auto_approve"))
    if has("placement"):
        if isinstance(body.get("placement"), dict) and body["placement"].get("moved_at"):
            raise AgentError("placement : un déménagement passe par POST /v1/agents/{id}/move")
        out["placement"] = norm_placement(body.get("placement"), local_machine, policy=policy, owner=owner)
    if has("needs"):
        out["needs"] = norm_needs(body.get("needs"))
    if has("project"):
        p = body.get("project")
        out["project"] = str(p).strip()[:128] or None if p is not None else None
    return out


__all__ = ["EVENT_KINDS", "NO_MAIL_TRIGGER", "PLACEMENTS", "iso", "norm_budget", "norm_fields", "norm_needs",
           "norm_placement", "norm_rules", "norm_secrets", "norm_trigger", "parse_when", "placement_target"]
