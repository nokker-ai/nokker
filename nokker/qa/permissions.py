"""Permissions locales de Nokker Lokkal (docs/LOKKAL.md, phase L1) : modes et règles par outil et par chemin, devant
les portes du noyau — sur le modèle des modes de permission des agents de code courants.

Modes :
  - `default`      : lire et chercher librement ; toute écriture et toute commande sont demandées (carte) ;
  - `accept_edits` : en plus, modifier les fichiers du dossier (write_file, patch) sans demander ; les commandes qui
                     écrivent restent demandées ;
  - `auto`         : lire, écrire et lancer des commandes sans demander ; seul le DESTRUCTIF est demandé ;
  - `plan`         : lecture seule ; tout appel à effet est refusé.

Règles, forme usuelle des agents de code : `outil` ou `outil(motif)`, motif glob sur la commande (`terminal(npm test*)`) ou sur le
chemin (`write_file(src/**)`) ; `deny` passe avant `ask`, qui passe avant `allow`. Une règle `allow` peut couvrir
une commande destructive (l'utilisateur la nomme lui-même) ; une commande « hardline » reste refusée par la source.

Ce qu'un DÉPÔT peut dire : un `.nokker/settings.json` vient avec le code, donc d'un tiers possible. Il ne peut que
RESTREINDRE (`deny`, `ask`, un mode plus strict) ; les `allow` et les modes plus larges ne viennent que de
l'utilisateur (ses réglages, ou la ligne de commande).

Une décision des permissions passe AVANT le juge : un appel couvert (`allow`, ou permis par le mode) n'est plus soumis
au juge Nina à chaque écriture — seuls les invariants qui refusent toujours (envoi de masse) le peuvent encore.
"""
from __future__ import annotations

import enum
import fnmatch
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolSpec, call_effect
from nokker.core.turn import Decision, Effect, Gate, ToolCall, Turn, Verdict

# accept_edits : écritures permises sans carte. checkpoint_create n'écrit que dans le dépôt fantôme des points de retour.
FILE_EDIT_TOOLS = frozenset({"write_file", "patch", "checkpoint_create"})
# accept_edits : la mémoire de projet (X1) n'écrit que dans le dossier d'état de Nokker, chaque note est visible
# (événement `memory.noted`) et un oubli se range à part (rien n'est détruit) — même régime qu'un point de retour.
MEMORY_EDIT_TOOLS = frozenset({"memory_note", "memory"})
# Verdict d'un appel à effet d'un connecteur relayé (X20) : une carte par appel, sans règle « session » ni « always ».
RELAY_CARD_RULE = "permissions:relayed_connector"
_RULE = re.compile(r"^\s*([A-Za-z0-9_.:-]+)\s*(?:\((.*)\))?\s*$")
_STRICTNESS = {"plan": 3, "default": 2, "accept_edits": 1, "auto": 0}


class Mode(str, enum.Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    AUTO = "auto"
    PLAN = "plan"


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None = None

    @classmethod
    def parse(cls, text: str) -> Rule:
        m = _RULE.match(text)
        if not m:
            raise ValueError(f"règle illisible : {text!r} (attendu : outil ou outil(motif))")
        return cls(tool=m.group(1), pattern=(m.group(2) or None))

    def matches(self, call: ToolCall) -> bool:
        # Une règle `terminal(…)` couvre aussi la même commande lancée en arrière-plan (lot X6) : mêmes règles.
        if call.name != self.tool and not (self.tool == "terminal" and call.name == "process_start"):
            return False
        if self.pattern is None:
            return True
        return bool(_subjects(call)) and all(fnmatch.fnmatchcase(s, self.pattern) for s in _subjects(call))


def _subjects(call: ToolCall) -> list[str]:
    """Ce que le motif d'une règle compare : la commande, ou le(s) chemin(s) touchés."""
    a = call.arguments
    if call.name in ("terminal", "process_start"):
        return [str(a.get("command", "")).strip()]
    if call.name.startswith("ssh_"):  # outils ops (lot X11) : `hôte:commande`, `hôte:chemin`
        from nokker.tools.ssh import ssh_subject

        subject = ssh_subject(call.name, a)
        return [subject] if subject else []
    if call.name == "patch" and str(a.get("mode")) == "patch":
        return [p.strip() for p in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", str(a.get("patch", "")),
                                              re.MULTILINE)]
    path = str(a.get("path") or "").strip()
    # normpath : « ./src/a.py » → « src/a.py », « ./../x » → « ../x » (qu'aucune règle « src/** » ne couvre)
    return [posixpath.normpath(path)] if path else []


@dataclass
class LocalPermissions:
    mode: Mode = Mode.DEFAULT
    allow: list[Rule] = field(default_factory=list)
    ask: list[Rule] = field(default_factory=list)
    deny: list[Rule] = field(default_factory=list)
    # Règles `allow` d'une décision « session » (§7.4.3) : pour CE fil seulement, en mémoire.
    thread_allow: dict[str, list[Rule]] = field(default_factory=dict)
    # `.nokker/settings.json` du dépôt de chaque session locale (lu à l'ouverture) : mode plus strict, `ask`, `deny`
    # pour CE fil. Un dépôt restreint, il n'élargit jamais (ses `allow` sont ignorés).
    thread_project: dict[str, tuple[Mode | None, list[Rule], list[Rule]]] = field(default_factory=dict)

    def restrict_for_thread(self, thread_id: str, project_settings: str | Path) -> None:
        try:
            project = _read(project_settings)
        except (OSError, ValueError):
            project = {}  # illisible : rien de plus strict que l'utilisateur, l'ouverture de session n'échoue pas
        mode = Mode(project["mode"]) if project.get("mode") in _STRICTNESS else None
        self.thread_project[thread_id] = (mode, [Rule.parse(r) for r in project.get("ask", [])],
                                          [Rule.parse(r) for r in project.get("deny", [])])

    def restrict_with(self, thread_id: str, rules: dict[str, Any]) -> None:
        """Règles d'un projet d'équipe (lot X17, nokker/team/) EN PLUS de celles du dépôt, même régime : mode plus
        strict, `ask`, `deny` ; un `allow` ou un mode plus large ne changent rien. À appeler APRÈS
        `restrict_for_thread` (qui repart de zéro pour le fil). Une règle illisible est ignorée (nokker.ai les a déjà
        validées ; l'ouverture de session n'échoue pas)."""
        cur_mode, cur_ask, cur_deny = self.thread_project.get(thread_id, (None, [], []))
        mode = Mode(rules["mode"]) if rules.get("mode") in _STRICTNESS else None
        if mode is None or (cur_mode is not None and _STRICTNESS[cur_mode.value] >= _STRICTNESS[mode.value]):
            mode = cur_mode

        def parsed(key: str) -> list[Rule]:
            out = []
            for r in rules.get(key) or []:
                try:
                    out.append(Rule.parse(str(r)))
                except ValueError:
                    continue
            return out

        self.thread_project[thread_id] = (mode, [*cur_ask, *parsed("ask")], [*cur_deny, *parsed("deny")])

    def allow_for_thread(self, thread_id: str, rule: Rule) -> None:
        rules = self.thread_allow.setdefault(thread_id, [])
        if rule not in rules:
            rules.append(rule)

    def mode_for(self, thread_id: str | None = None) -> Mode:
        """Le mode en vigueur sur ce fil : celui de l'utilisateur, ou celui du dépôt s'il est plus strict."""
        proj_mode = self.thread_project.get(thread_id or "", (None, [], []))[0]
        return proj_mode if proj_mode and _STRICTNESS[proj_mode.value] > _STRICTNESS[self.mode.value] else self.mode

    def decide(self, call: ToolCall, effect: Effect, thread_id: str | None = None) -> tuple[Decision | None, str]:
        """(décision, motif). None = rien à dire : les portes du noyau décident seules (ex. destructif en mode auto)."""
        _, proj_ask, proj_deny = self.thread_project.get(thread_id or "", (None, [], []))
        mode = self.mode_for(thread_id)
        for r in [*self.deny, *proj_deny]:
            if r.matches(call):
                return Decision.DENY, f"règle deny {r.tool}({r.pattern or ''})"
        if effect is not Effect.READ and mode is Mode.PLAN:
            return Decision.DENY, "mode plan : lecture seule"
        for r in [*self.ask, *proj_ask]:
            if r.matches(call):
                return Decision.HITL, f"règle ask {r.tool}({r.pattern or ''})"
        for r in [*self.allow, *self.thread_allow.get(thread_id or "", [])]:
            if r.matches(call):
                return Decision.ALLOW, f"règle allow {r.tool}({r.pattern or ''})"
        if effect is Effect.READ:
            return Decision.ALLOW, f"mode {mode.value} : lecture"
        if effect is Effect.WRITE:
            if mode is Mode.AUTO or (mode is Mode.ACCEPT_EDITS and call.name in FILE_EDIT_TOOLS | MEMORY_EDIT_TOOLS):
                return Decision.ALLOW, f"mode {mode.value} : écriture dans le dossier"
            return Decision.HITL, f"mode {mode.value} : écriture demandée"
        return None, ""  # DESTRUCTIVE, SEND_*, SPEND, SECRET : les invariants décident (carte forte, refus…)

    # ── chargement ──
    @classmethod
    def load(cls, *, mode: str | None = None, user_settings: str | Path | None = None,
             project_settings: str | Path | None = None, allow: list[str] | None = None,
             fallback: Mode = Mode.DEFAULT) -> LocalPermissions:
        """Réglages de l'utilisateur (tout permis), puis du dépôt (restreindre seulement), puis ligne de commande.
        `fallback` : le mode quand ni la ligne de commande ni l'utilisateur n'en donnent."""
        user = _read(user_settings)
        project = _read(project_settings)
        chosen = Mode(mode or user.get("mode") or fallback.value)
        proj_mode = project.get("mode")
        if proj_mode and _STRICTNESS[Mode(proj_mode).value] > _STRICTNESS[chosen.value]:
            chosen = Mode(proj_mode)  # le dépôt peut rendre plus strict, jamais plus large
        return cls(mode=chosen,
                   allow=[Rule.parse(r) for r in [*user.get("allow", []), *(allow or [])]],
                   ask=[Rule.parse(r) for r in [*user.get("ask", []), *project.get("ask", [])]],
                   deny=[Rule.parse(r) for r in [*user.get("deny", []), *project.get("deny", [])]])


def _read(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    return dict(json.loads(p.read_text(encoding="utf-8")).get("permissions") or {})


@dataclass
class PermissionGates:
    """Portes du noyau précédées des permissions locales. `inner` : GatesV0 ou GatesV1 (juge)."""

    inner: Any
    permissions: LocalPermissions
    # Planchers (lot X11, `SshToolSource.floor`) : `(appel, spec, tour) -> Verdict | None`, consultés AVANT les
    # règles et le mode. Un DENY refuse ; un HITL est une carte qu'aucune règle `allow` (session, « always »), aucun
    # mode ni GO textuel ne lève — seuls les `deny` et le mode plan refusent encore (plus strict que la carte).
    floors: list[Any] = field(default_factory=list)
    # Fil → session de code locale ? Les portes à permissions locales servent `nokker code` (tout le processus est une
    # session de code : défaut None = vrai pour tout fil) et Lokkal (le registre des sessions de code, posé au
    # démarrage). Transmis aux portes du noyau : le drapeau de domaine y devient consultatif (qa/gates.py).
    code_session: Any = None

    def __post_init__(self) -> None:
        if hasattr(self.inner, "code_session") and getattr(self.inner, "code_session", None) is None:
            self.inner.code_session = self.code_session if self.code_session is not None else (lambda _tid: True)

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]:
        if call.name in ("terminal", "process_start", "ssh_run"):
            from nokker.tools.code import hardline_reason

            why = hardline_reason(str(call.arguments.get("command", "")))
            if why:  # jamais exécutable, même approuvé : pas de carte, refus direct
                return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="permissions:hardline",
                                reason=why, subject=call.id)]
        effect = call_effect(spec, call.arguments)
        for floor in self.floors:
            imposed = floor(call, spec, turn)
            if imposed is None:
                continue
            if imposed.decision is Decision.HITL:
                decision, why = self.permissions.decide(call, effect, getattr(turn, "thread_id", None))
                if decision is Decision.DENY:
                    return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1,
                                    rule=f"permissions:{self.permissions.mode.value}", reason=why, subject=call.id)]
            return [imposed]
        decision, why = self.permissions.decide(call, effect, getattr(turn, "thread_id", None))
        if getattr(spec, "locality", None) == "cloud" and effect is not Effect.READ and decision is not Decision.DENY:
            # Connecteur relayé (lot X20) : la ressource est au compte nokker.ai, qui exige la preuve d'une carte pour
            # tout appel à effet. Ni le mode (auto, accept_edits), ni une règle allow, ni un GO écrit n'en dispensent.
            # Envoi externe et destructif (X20b) : carte FORTE, qui part au téléphone (X10) et dont nokker.ai vérifie
            # lui-même la signature ; une écriture interne garde une carte standard.
            v1 = self.inner.invariants.check_action(call, spec, turn)
            if v1.decision is Decision.DENY:  # envoi de masse : refusé, pas de carte
                return [v1]
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RELAY_CARD_RULE,
                            reason="outil d'un compte nokker.ai (connecteur relayé) : chaque appel à effet passe par "
                                   "une carte", subject=call.id,
                            strength="strong" if effect in (Effect.DESTRUCTIVE, Effect.SEND_EXTERNAL)
                            else "standard")]
        if decision is None:
            return self.inner.action(call, spec, turn)
        if decision is Decision.ALLOW:
            v1 = self.inner.invariants.check_action(call, spec, turn)
            if v1.decision is Decision.DENY:  # ce qui est toujours refusé (envoi de masse) le reste
                return [v1]
        strength = "strong" if effect is Effect.DESTRUCTIVE and decision is Decision.HITL else None
        return [Verdict(gate=Gate.ACTION, decision=decision, layer=1, rule=f"permissions:{self.permissions.mode.value}",
                        reason=why, subject=call.id, strength=strength)]

    def switch(self, reply: str, turn: Turn, tier: Any) -> list[Verdict]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:  # deliverable, invariants… : ceux des portes du noyau
        return getattr(self.inner, name)


__all__ = ["FILE_EDIT_TOOLS", "MEMORY_EDIT_TOOLS", "RELAY_CARD_RULE", "LocalPermissions", "Mode", "PermissionGates",
           "Rule"]
