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
import logging
import os
import posixpath
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.core import model_texts as mt
from nokker.core.ports import ToolSpec, call_effect
from nokker.core.turn import RELAY_CHANNEL, Decision, Effect, Gate, ToolCall, Turn, Verdict

# accept_edits : écritures permises sans carte. checkpoint_create n'écrit que dans le dépôt fantôme des points de retour.
FILE_EDIT_TOOLS = frozenset({"write_file", "patch", "checkpoint_create"})
# accept_edits : la mémoire de projet (X1) n'écrit que dans le dossier d'état de Nokker, chaque note est visible
# (événement `memory.noted`) et un oubli se range à part (rien n'est détruit) — même régime qu'un point de retour.
MEMORY_EDIT_TOOLS = frozenset({"memory_note", "memory"})
# Verdict d'un appel à effet d'un connecteur relayé (X20) : une carte par appel, sans règle « session » ni « always ».
RELAY_CARD_RULE = "permissions:relayed_connector"
# Tour arrivé du WEB (canal `relay`) : destructif, envoi externe, dépense = carte forte signée au téléphone (X10).
RELAYED_STRONG_RULE = "permissions:relayed_strong"
# Destructif hors du bac à sable d'une session de code : le GO textuel du propriétaire n'y suffit pas.
GO_SANDBOX_RULE = "permissions:go_sandbox_only"
# Seuls outils dont l'exécution tient dans le bac à sable de la session (dossier monté seul, points de retour).
SANDBOX_GO_TOOLS = frozenset({"terminal", "process_start"})
LOCAL_STRONG_RULE = "permissions:local_connector_strong"  # boîte mail posée dans Lokkal : envoi, corbeille
# Bureau (docs/DESKTOP.md §3.1) : une écriture dans une appli est toujours demandée, même en mode `auto` ; seule une
# règle `allow` (« pour cette session », `desktop_act(<appli>:write)`) la couvre. Envoi et destruction : carte.
DESKTOP_TOOLS = frozenset({"desktop_act", "desktop_open"})
_RULE = re.compile(r"^\s*([A-Za-z0-9_.:-]+)\s*(?:\((.*)\))?\s*$")
_STRICTNESS = {"plan": 3, "default": 2, "accept_edits": 1, "auto": 0}
MODES = ("default", "accept_edits", "auto", "plan")
# Codes stables des refus (`Verdict.code`, puis `reason` du `tool.result`) : pour les clients, jamais vus par le modèle.
# Mode plan : rien ne s'exécute, donc aucune étape du plan de travail (`todo`) ne peut être terminée. Texte vu du modèle.
PLAN_TODO_WHY = mt.PERM_PLAN_TODO["fr"]  # figé ; langue du tour : `plan_todo_why()`
PLAN_MODE, DENY_RULE, HARDLINE, NO_CODE_SESSION = "plan_mode", "deny_rule", "hardline", "no_code_session"
# Codes stables des CARTES (`Verdict.code` d'un HITL, puis `reason_code` de la carte) : nommés d'après la CAUSE, posés
# au même endroit que le texte du motif, jamais déduits de ce texte. Pour les clients seulement (jamais vus du modèle).
ASK_MATCHED = "ask_rule"  # une règle `ask` (utilisateur, dépôt ou équipe) couvre l'appel
DEFAULT_WRITE = "default_write"  # mode default : toute écriture / commande est demandée
ACCEPT_EDITS_WRITE = "accept_edits_write"  # mode accept_edits : écriture hors fichiers du dossier (commande…)
DESKTOP_WRITE = "desktop_write"  # écriture sur le bureau : toujours demandée sans pas validé à l'écran
RELAYED_CONNECTOR = "relayed_connector"  # connecteur relayé nokker.ai : une carte par appel à effet
LOCAL_CONNECTOR_STRONG = "local_connector_strong"  # boîte mail de la machine : envoi, corbeille
RELAYED_STRONG = "relayed_strong"  # tour venu du web : destructif, envoi externe, dépense
GO_SANDBOX_ONLY = "go_sandbox_only"  # destructif hors du bac à sable : le GO écrit ne suffit pas
_WRITE_ASKED_CODE = {"default": DEFAULT_WRITE, "accept_edits": ACCEPT_EDITS_WRITE}


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
    if call.name in DESKTOP_TOOLS:  # bureau : `appli:classe` (classe = write | send_external | destructive)
        from nokker.desktop.policy import desktop_subject

        subject = desktop_subject(call.name, a)
        return [subject] if subject else []
    if call.name == "patch" and str(a.get("mode")) == "patch":
        return [p.strip() for p in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", str(a.get("patch", "")),
                                              re.MULTILINE)]
    path = str(a.get("path") or "").strip()
    # normpath : « ./src/a.py » → « src/a.py », « ./../x » → « ../x » (qu'aucune règle « src/** » ne couvre)
    return [posixpath.normpath(path)] if path else []


def _completes_todo(arguments: Any) -> bool:
    todos = arguments.get("todos") if isinstance(arguments, dict) else None
    return isinstance(todos, list) and any(isinstance(t, dict) and str(t.get("status") or "").strip().lower() == "completed"
                                           for t in todos)


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
    # Mode choisi pour UNE session en cours de route (`PUT /v1/local/sessions/{fil}/mode`, docs/LOKKAL.md §7.4.10) :
    # remplace `mode` pour ce fil (et ses sous-agents), toujours restreint par le dépôt et les équipes. En mémoire.
    thread_mode: dict[str, Mode] = field(default_factory=dict)
    # Fichier où le mode choisi d'une session survit au redémarrage du noyau (`<état>/session-modes.json`, 0600, posé par
    # serve) : `set_thread_mode` l'y écrit, `restore_thread_mode` le relit à la reprise du fil. None (défaut) : en mémoire seulement.
    modes_path: Path | None = None
    _modes_lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    # Bureau en mode session (B2, docs/DESKTOP.md §12) : chaque action ouvre un PAS que la personne valide à l'écran
    # (« Vas-y », lié à l'id du pas et à args_sha256) ; ce pas remplace la carte d'écriture. Envoi et suppression gardent
    # leur carte (les invariants décident, pas cette règle). None = pas de pas : l'écriture du bureau reste une carte.
    desktop_steps: Any = None

    def chosen_mode(self, thread_id: str | None = None) -> Mode:
        """Le mode choisi pour ce fil (celui de la session, sinon celui de l'utilisateur), AVANT restriction."""
        tid = thread_id or ""
        while tid not in self.thread_mode and "/sa-" in tid:
            tid = tid.rsplit("/sa-", 1)[0]
        return self.thread_mode.get(tid, self.mode)

    def project_mode(self, thread_id: str | None = None) -> Mode | None:
        """Le mode imposé par le dépôt ou les équipes du fil (le plus strict), ou None."""
        return self._project(thread_id)[0]

    def set_thread_mode(self, thread_id: str, mode: str) -> Mode:
        """Change le mode d'une session. `ValueError` si le mode est inconnu ou plus permissif que celui que le dépôt ou
        une équipe impose au fil (on ne fait pas semblant d'accorder ce que la restriction reprendrait)."""
        if mode not in _STRICTNESS:
            raise ValueError(f"mode inconnu : {mode!r} (parmi {', '.join(MODES)})")
        proj = self.project_mode(thread_id)
        if proj is not None and _STRICTNESS[proj.value] > _STRICTNESS[mode]:
            raise ValueError(f"mode {mode} plus permissif que celui du projet ({proj.value})")
        self.thread_mode[thread_id] = Mode(mode)
        self._save_mode(thread_id, mode)
        return self.mode_for(thread_id)

    MAX_STORED_MODES = 5000

    def _save_mode(self, thread_id: str, mode: str) -> None:
        """Écrit le mode du fil dans `modes_path` (atomique, 0600). Un échec d'écriture ne casse pas le changement de mode : il
        reste en vigueur jusqu'au redémarrage, et l'échec est journalisé."""
        if self.modes_path is None:
            return
        with self._modes_lock:
            try:
                stored = self._load_modes()
                stored.pop(thread_id, None)
                stored[thread_id] = mode  # le plus récent en dernier
                while len(stored) > self.MAX_STORED_MODES:
                    stored.pop(next(iter(stored)))
                tmp = self.modes_path.with_name(self.modes_path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"v": 1, "modes": stored}, fh)
                os.replace(tmp, self.modes_path)
            except OSError:
                logging.getLogger("nokker.qa.permissions").warning("mode de session non conservé (écriture impossible)")

    def _load_modes(self) -> dict[str, str]:
        try:
            raw = json.loads(self.modes_path.read_text(encoding="utf-8")) if self.modes_path else {}
        except (OSError, ValueError):
            return {}
        modes = raw.get("modes") if isinstance(raw, dict) else None
        return {k: v for k, v in modes.items() if isinstance(k, str) and v in _STRICTNESS} if isinstance(modes, dict) else {}

    def restore_thread_mode(self, thread_id: str) -> Mode | None:
        """À la reprise d'un fil (après un redémarrage) : le mode que l'hôte lui avait réglé, relu de `modes_path`. Sans effet si
        le fil a déjà un mode en mémoire ou n'en a jamais eu. Pas de contrôle contre le dépôt ici : `mode_for` prend toujours
        le plus strict des deux."""
        if self.modes_path is None or thread_id in self.thread_mode:
            return self.thread_mode.get(thread_id)
        with self._modes_lock:
            stored = self._load_modes().get(thread_id)
        if stored is None:
            return None
        self.thread_mode[thread_id] = Mode(stored)
        return self.thread_mode[thread_id]

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

    def _project(self, thread_id: str | None) -> tuple[Mode | None, list[Rule], list[Rule]]:
        """Restrictions du dépôt / de l'équipe du fil ; un sous-agent `<fil>/sa-<id>` a celles de son parent (elles ne
        font que restreindre : déléguer ne les contourne pas). Les règles « pour cette session » (`thread_allow`) ne
        s'étendent PAS à l'enfant."""
        tid = thread_id or ""
        while tid not in self.thread_project and "/sa-" in tid:
            tid = tid.rsplit("/sa-", 1)[0]
        return self.thread_project.get(tid, (None, [], []))

    def mode_for(self, thread_id: str | None = None) -> Mode:
        """Le mode en vigueur sur ce fil : celui de l'utilisateur, ou celui du dépôt s'il est plus strict."""
        proj_mode = self._project(thread_id)[0]
        mine = self.chosen_mode(thread_id)
        return proj_mode if proj_mode and _STRICTNESS[proj_mode.value] > _STRICTNESS[mine.value] else mine

    def decide(self, call: ToolCall, effect: Effect, thread_id: str | None = None) -> tuple[Decision | None, str]:
        """(décision, motif). None = rien à dire : les portes du noyau décident seules (ex. destructif en mode auto)."""
        decision, why, _ = self.decide_coded(call, effect, thread_id)
        return decision, why

    def decide_coded(self, call: ToolCall, effect: Effect, thread_id: str | None = None
                     ) -> tuple[Decision | None, str, str | None]:
        """`decide` plus le code stable d'un refus (`deny_rule`, `plan_mode`) ou d'une carte (HITL), None pour un ALLOW."""
        _, proj_ask, proj_deny = self._project(thread_id)
        mode = self.mode_for(thread_id)
        for r in [*self.deny, *proj_deny]:
            if r.matches(call):
                return Decision.DENY, mt.pick(mt.PERM_DENY_RULE).format(rule=f"{r.tool}({r.pattern or ''})"), DENY_RULE
        if effect is not Effect.READ and mode is Mode.PLAN:
            return Decision.DENY, mt.pick(mt.PERM_PLAN_READONLY), PLAN_MODE
        if mode is Mode.PLAN and call.name == "todo" and _completes_todo(call.arguments):
            return Decision.DENY, mt.pick(mt.PERM_PLAN_TODO), PLAN_MODE
        decision, why, code = self._decide_rest(call, effect, thread_id, mode, proj_ask)
        return decision, why, code if decision is Decision.HITL else None

    def _decide_rest(self, call: ToolCall, effect: Effect, thread_id: str | None, mode: Mode,
                     proj_ask: list[Rule]) -> tuple[Decision | None, str, str | None]:
        for r in [*self.ask, *proj_ask]:
            if r.matches(call):
                return Decision.HITL, mt.pick(mt.PERM_ASK_RULE).format(rule=f"{r.tool}({r.pattern or ''})"), ASK_MATCHED
        for r in [*self.allow, *self.thread_allow.get(thread_id or "", [])]:
            if r.matches(call):
                return Decision.ALLOW, f"règle allow {r.tool}({r.pattern or ''})", None
        if effect is Effect.READ:
            return Decision.ALLOW, f"mode {mode.value} : lecture", None
        if effect is Effect.WRITE:
            if call.name in DESKTOP_TOOLS:
                if self.desktop_steps is not None and self.desktop_steps():
                    return Decision.ALLOW, "bureau en session : chaque pas est validé à l'écran par la personne", None
                return Decision.HITL, mt.pick(mt.PERM_DESKTOP_WRITE).format(mode=mode.value), DESKTOP_WRITE
            if mode is Mode.AUTO or (mode is Mode.ACCEPT_EDITS and call.name in FILE_EDIT_TOOLS | MEMORY_EDIT_TOOLS):
                return Decision.ALLOW, f"mode {mode.value} : écriture dans le dossier", None
            return Decision.HITL, mt.pick(mt.PERM_WRITE_ASKED).format(mode=mode.value), _WRITE_ASKED_CODE.get(mode.value)
        return None, "", None  # DESTRUCTIVE, SEND_*, SPEND, SECRET : les invariants décident (carte forte, refus…)

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


def _session_refused(thread_id: str) -> bool:
    from nokker.tools.code import session_refused  # import tardif : `tools.code` importe les portes

    return session_refused(thread_id)


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
    # Fil → le bac à sable de sa session de code est-il un vrai bac (docker, kubernetes) ? Posé par Lokkal : le GO
    # textuel du propriétaire ne lève alors un destructif que pour `SANDBOX_GO_TOOLS` sur un tel fil ; ailleurs, carte
    # forte. None (`nokker code`, bancs) : comportement inchangé.
    go_sandbox: Any = None
    # Outils qui n'existent que dans une session de code (Lokkal : `CODE_TOOLS`). Sur un fil sans session, refusés AVANT
    # toute carte : le 26.10, une carte forte signée au téléphone a échoué à l'exécution (« pas de session de code »).
    session_tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if hasattr(self.inner, "code_session") and getattr(self.inner, "code_session", None) is None:
            self.inner.code_session = self.code_session if self.code_session is not None else (lambda _tid: True)

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]:
        if call.name in self.session_tools and self.code_session is not None \
                and not self.code_session(getattr(turn, "thread_id", "") or ""):
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="permissions:no_code_session",
                            reason=mt.pick(mt.PERM_SESSION_REFUSED if _session_refused(getattr(turn, "thread_id", "") or "")
                                           else mt.PERM_NO_CODE_SESSION), subject=call.id, code=NO_CODE_SESSION)]
        if call.name in ("terminal", "process_start", "ssh_run"):
            from nokker.tools.code import hardline_reason

            why = hardline_reason(str(call.arguments.get("command", "")))
            if why:  # jamais exécutable, même approuvé : pas de carte, refus direct
                return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="permissions:hardline",
                                reason=why, subject=call.id, code=HARDLINE)]
        effect = call_effect(spec, call.arguments)
        # Le mode DU FIL (celui que `PUT …/sessions/{fil}/mode` a posé, dépôt compris), pas le mode de base : l'étiquette
        # de règle (`permissions:<mode>`) décide aussi de l'éligibilité au relecteur automatique et aux suggestions.
        rule_name = f"permissions:{self.permissions.mode_for(getattr(turn, 'thread_id', None)).value}"
        for floor in self.floors:
            imposed = floor(call, spec, turn)
            if imposed is None:
                continue
            if imposed.decision is Decision.HITL:
                decision, why, code = self.permissions.decide_coded(call, effect, getattr(turn, "thread_id", None))
                if decision is Decision.DENY:
                    return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1,
                                    rule=rule_name, reason=why, subject=call.id, code=code)]
            return [imposed]
        decision, why, code = self.permissions.decide_coded(call, effect, getattr(turn, "thread_id", None))
        if getattr(spec, "locality", None) == "cloud" and effect is not Effect.READ and decision is not Decision.DENY:
            # Connecteur relayé (lot X20) : la ressource est au compte nokker.ai, qui exige la preuve d'une carte pour
            # tout appel à effet. Ni le mode (auto, accept_edits), ni une règle allow, ni un GO écrit n'en dispensent.
            # Envoi externe et destructif (X20b) : carte FORTE, qui part au téléphone (X10) et dont nokker.ai vérifie
            # lui-même la signature ; une écriture interne garde une carte standard.
            v1 = self.inner.invariants.check_action(call, spec, turn)
            if v1.decision is Decision.DENY:  # envoi de masse : refusé, pas de carte
                return [v1]
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RELAY_CARD_RULE,
                            reason=mt.pick(mt.PERM_RELAY_CARD), subject=call.id,
                            strength="strong" if effect in (Effect.DESTRUCTIVE, Effect.SEND_EXTERNAL)
                            else "standard", code=RELAYED_CONNECTOR)]
        if getattr(spec, "locality", None) == "local" and str(getattr(spec, "source", "") or "").startswith(
                "connector:") and effect in (Effect.DESTRUCTIVE, Effect.SEND_EXTERNAL) and decision is not Decision.DENY:
            # Boîte posée dans Lokkal (nokker/local/connectors.py) : l'envoi et la corbeille restent une carte FORTE,
            # comme pour la boîte relayée (X20b) — ni le mode, ni une règle allow, ni un GO écrit n'en dispensent.
            v1 = self.inner.invariants.check_action(call, spec, turn)
            if v1.decision is Decision.DENY:  # envoi de masse : refusé, pas de carte
                return [v1]
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=LOCAL_STRONG_RULE,
                            reason=mt.pick(mt.PERM_LOCAL_STRONG), subject=call.id, strength="strong", code=LOCAL_CONNECTOR_STRONG)]
        if getattr(turn, "channel", None) == RELAY_CHANNEL and decision is not Decision.DENY \
                and effect in (Effect.DESTRUCTIVE, Effect.SEND_EXTERNAL, Effect.SPEND):
            # Tour venu du web (relais `<nom>.nokker.app`, X10) : ni le mode (auto, accept_edits), ni une règle allow,
            # ni un GO écrit ne dispensent de la carte FORTE, qui part au téléphone. Seuls deny et plan refusent encore.
            v1 = self.inner.invariants.check_action(call, spec, turn)
            if v1.decision is Decision.DENY:  # envoi de masse : refusé, pas de carte
                return [v1]
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RELAYED_STRONG_RULE,
                            reason=mt.pick(mt.PERM_RELAYED_STRONG),
                            subject=call.id, strength="strong", code=RELAYED_STRONG)]
        if decision is None and effect is Effect.DESTRUCTIVE and self.go_sandbox is not None \
                and not (call.name in SANDBOX_GO_TOOLS and self.go_sandbox(getattr(turn, "thread_id", None) or "")):
            # Le nom exact de la commande vaut accord dans le bac à sable de la session seulement (réversible par les
            # points de retour) : ssh_run, connecteurs, outils MCP, fil hors session → carte forte, GO ou pas.
            v1 = self.inner.invariants.check_action(call, spec, turn)
            if v1.decision is Decision.DENY:
                return [v1]
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=GO_SANDBOX_RULE,
                            reason=mt.pick(mt.PERM_GO_SANDBOX), subject=call.id, strength="strong", code=GO_SANDBOX_ONLY)]
        if decision is None:
            return self.inner.action(call, spec, turn)
        if decision is Decision.ALLOW:
            v1 = self.inner.invariants.check_action(call, spec, turn)
            if v1.decision is Decision.DENY:  # ce qui est toujours refusé (envoi de masse) le reste
                return [v1]
        strength = "strong" if effect is Effect.DESTRUCTIVE and decision is Decision.HITL else None
        return [Verdict(gate=Gate.ACTION, decision=decision, layer=1, rule=rule_name,
                        reason=why, subject=call.id, strength=strength,
                        code=code if decision in (Decision.DENY, Decision.HITL) else None)]

    def switch(self, reply: str, turn: Turn, tier: Any) -> list[Verdict]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:  # deliverable, invariants… : ceux des portes du noyau
        return getattr(self.inner, name)


__all__ = ["MODES", "PLAN_MODE", "PLAN_TODO_WHY", "FILE_EDIT_TOOLS", "GO_SANDBOX_RULE", "SANDBOX_GO_TOOLS", "MEMORY_EDIT_TOOLS", "RELAYED_STRONG_RULE", "RELAY_CARD_RULE", "LocalPermissions", "Mode", "PermissionGates",
           "Rule"]
