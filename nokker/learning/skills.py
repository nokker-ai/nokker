"""Skills appris (docs/LEARNING.md §2) : l'agent PROPOSE un skill (`skill_create`) ou une nouvelle version
(`skill_update`) ; rien n'est écrit sans la carte d'approbation de l'humain, qui voit le skill complet (les arguments
de l'appel SONT le skill) ; chaque version est gardée (`skill_history`, différence entre versions) et l'humain peut
revenir à n'importe laquelle, ou désactiver le skill (`skill_revert`, version 0), sans rien effacer.

Différences avec l'apprentissage « libre » des agents courants :
  - **carte toujours** : `LearningGates` impose une carte à `skill_create|update|revert` venus du modèle, quel que
    soit le mode (auto, accept_edits), une règle `allow` ou un GO écrit ; seuls un `deny` et le mode plan refusent ;
  - **garde avant la carte** : le SKILL.md proposé passe par le scanner des skills (`tools.skills_guard`, même brique
    que `skill_install`) AVANT la carte (un skill `dangerous` est refusé sans déranger l'humain) puis de nouveau à
    l'exécution ; scanner absent = refus (échec fermé) ;
  - **racine de l'utilisateur seulement** : un skill appris s'écrit sous `<home>/skills/learned/<nom>/SKILL.md`
    (Lokkal), jamais dans un dépôt ; chez nokker.ai il vit en base, par compte, sous RLS ;
  - **provenance** : fil, tour, date, carte (id), acteur (`agent` | `owner`) sur chaque version et au journal.

Un skill appris ne prend jamais le nom d'un skill fourni, installé ou du dépôt (`taken`) : il ne masque rien."""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, ToolCall, ToolResult, Turn, Verdict
from nokker.learning.store import NAME_RE, JournalEntry, LearningConflict, SkillVersion

log = logging.getLogger("nokker.learning.skills")

LEARNED_FAMILY = "learned"  # famille (dossier) des skills appris sous la racine de l'utilisateur
MAX_DESCRIPTION = 200
MAX_BODY = 16_000
MAX_REASON = 300
CARD_TOOLS = frozenset({"skill_create", "skill_update", "skill_revert"})
CARD_RULE = "learning:card"
REFUSED_RULE = "learning:refused"
DIRECT_CHANNEL = "local-direct"  # = nokker.core.loop.DIRECT_CHANNEL (appel du propriétaire hors tour)

# Textes vus par le modèle (gel : la session d'entraînement les valide avant activation, docs/LEARNING.md §7).
_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("skill_create",
     ("Propose un nouveau skill appris : une méthode réutilisable tirée d'une tâche réussie (nom, description d'une "
      "ligne, mode d'emploi). Scanné, puis écrit seulement si l'utilisateur l'approuve sur carte ; versionné."),
     Effect.WRITE, {"type": "object", "required": ["name", "description", "body"], "properties": {
         "name": {"type": "string", "description": "minuscules et tirets, 2 à 64 caractères (ex. deploy-docs)"},
         "description": {"type": "string",
                         "description": f"une ligne : quand s'en servir (≤ {MAX_DESCRIPTION} car.)"},
         "body": {"type": "string",
                  "description": f"mode d'emploi en Markdown : étapes, commandes, pièges (≤ {MAX_BODY} car.), "
                                 "sans secret"}}}),
    ("skill_update",
     ("Propose une nouvelle version d'un skill appris (mode d'emploi, description) ; l'ancienne reste dans "
      "l'historique. Écrite seulement si l'utilisateur l'approuve sur carte."),
     Effect.WRITE, {"type": "object", "required": ["name", "body", "reason"], "properties": {
         "name": {"type": "string", "description": "nom du skill appris"},
         "description": {"type": "string", "description": "nouvelle description (facultative : sinon inchangée)"},
         "body": {"type": "string", "description": f"nouveau mode d'emploi complet (≤ {MAX_BODY} car.)"},
         "reason": {"type": "string", "description": f"ce qui change et pourquoi (≤ {MAX_REASON} car.)"}}}),
    ("skill_history",
     ("Historique des skills appris : sans name, la liste ; avec name, les versions et leur provenance (fil, tour, "
      "date) ; avec version, le texte de cette version ; avec diff_from, la différence entre deux versions."),
     Effect.READ, {"type": "object", "properties": {
         "name": {"type": "string"},
         "version": {"type": "integer", "description": "version à lire (défaut : la dernière)"},
         "diff_from": {"type": "integer", "description": "version de départ de la différence"}}}),
    ("skill_revert",
     ("Revient à une version antérieure d'un skill appris (une nouvelle version est créée, rien n'est effacé) ; "
      "version 0 désactive le skill. Écrit seulement si l'utilisateur l'approuve sur carte."),
     Effect.WRITE, {"type": "object", "required": ["name", "version"], "properties": {
         "name": {"type": "string"},
         "version": {"type": "integer", "description": "version à rétablir ; 0 = désactiver"}}}),
)
LEARNING_TOOLS = tuple(n for n, *_ in _SPECS)


class LearningError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def render_skill(name: str, description: str, body: str) -> str:
    """Le SKILL.md d'un skill appris : frontmatter (`name`, `description` en chaîne JSON, donc YAML valide sur une
    ligne, `learned: true`), puis le corps tel quel."""
    return (f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\nlearned: true\n---\n"
            f"{body.rstrip()}\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _guard() -> Any | None:
    from nokker.tools.skills import (
        _guard_module,  # même brique que skill_install, importée paresseusement
    )

    return _guard_module()


@dataclass
class LearnedSkills:
    """Service des skills appris. `store` : `nokker/learning/store.py`. `skills_dir(compte)` : dossier où écrire les
    SKILL.md actifs (Lokkal : `<home>/skills/learned`) ou None (nokker.ai : lus en base). `taken(compte, nom)` : vrai si
    un skill NON appris porte déjà ce nom. `emit(nom, données)` : bus d'événements (`skill.learned`)."""

    store: Any
    skills_dir: Callable[[str], Path | None] = field(default=lambda _a: None)
    taken: Callable[[str, str], bool] | None = None
    emit: Callable[[str, dict[str, Any]], Any] | None = None
    now_fn: Callable[[], str] = field(default=_now)
    scrub: Callable[[str], str] = field(default=lambda s: s)  # `invariants.scrub_output` (aucun secret écrit)

    # ── validation et garde ──
    def _fields(self, args: dict[str, Any], account: str, update: bool) -> tuple[str, str, str]:
        name = str(args.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise LearningError(400, "bad_name", "name : minuscules, chiffres et tirets, 2 à 64 caractères")
        body = str(args.get("body") or "")
        desc = args.get("description")
        if update:
            last = self.current(account, name)
            if last is None:
                raise LearningError(404, "unknown_skill", f"skill appris inconnu : {name} (skill_create d'abord)")
            if desc is None or not str(desc).strip():
                from nokker.tools.skills import parse_frontmatter

                desc = str(parse_frontmatter(last.content)[0].get("description") or "")
            reason = " ".join(str(args.get("reason") or "").split())
            if not reason or len(reason) > MAX_REASON:
                raise LearningError(400, "bad_reason", f"reason requis (≤ {MAX_REASON} car.)")
        else:
            if self.store.skill_versions(account, name):
                raise LearningError(409, "exists", f"le skill appris {name} existe : skill_update pour le modifier")
            if self.taken is not None and self.taken(account, name):
                raise LearningError(409, "name_taken", f"{name} est déjà le nom d'un skill fourni, installé ou du "
                                                       "dépôt : choisis un autre nom")
        desc = " ".join(str(desc or "").split())
        if not desc or len(desc) > MAX_DESCRIPTION:
            raise LearningError(400, "bad_description", f"description requise, une ligne (≤ {MAX_DESCRIPTION} car.)")
        if not body.strip() or len(body) > MAX_BODY:
            raise LearningError(400, "bad_body", f"body requis (≤ {MAX_BODY} car.)")
        if "\x00" in body:
            raise LearningError(400, "bad_body", "body : texte seulement")
        return name, desc, body

    def scan(self, name: str, content: str) -> tuple[str, str]:
        """(verdict, résumé) du scanner des skills sur ce SKILL.md, écrit dans un dossier temporaire. `dangerous` ou
        scanner absent : `LearningError` (rien n'est écrit, aucune carte n'est posée)."""
        guard = _guard()
        if guard is None:
            raise LearningError(503, "no_scanner", "le scanner de sécurité des skills n'est pas disponible ici : "
                                                   "rien n'est écrit")
        with tempfile.TemporaryDirectory(prefix="nokker-learned-") as tmp:
            d = Path(tmp) / name
            d.mkdir()
            (d / "SKILL.md").write_text(content, encoding="utf-8")
            try:
                result = guard.scan_skill(d, source="community")
            except Exception as e:
                raise LearningError(503, "scan_failed", f"scan impossible : {type(e).__name__}") from e
        verdict = str(getattr(result, "verdict", "dangerous"))
        summary = str(getattr(result, "summary", "") or verdict).replace("hermes", "nokker").replace("Hermes", "Nokker")
        if verdict not in ("safe", "caution"):
            raise LearningError(422, "scan_refused", f"skill refusé par le scanner de sécurité : {summary}")
        return verdict, summary

    def check(self, tool: str, args: dict[str, Any], account: str) -> str:
        """Ce que la carte dira : validation et scan AVANT la carte. Lève `LearningError` si l'appel est refusé."""
        if tool == "skill_revert":
            name, version = self._revert_args(args)
            self._revert_target(account, name, version)
            return (f"retour du skill appris {name} à la version {version}" if version else
                    f"désactivation du skill appris {name} (historique gardé)")
        name, desc, body = self._fields(args, account, update=tool == "skill_update")
        verdict, summary = self.scan(name, render_skill(name, desc, body))
        what = "nouvelle version du skill appris" if tool == "skill_update" else "nouveau skill appris"
        warn = f" — scanner : à vérifier ({summary})" if verdict == "caution" else ""
        return f"{what} {name} : la carte montre le skill complet (arguments){warn}"

    # ── lecture ──
    def current(self, account: str, name: str) -> SkillVersion | None:
        versions = self.store.skill_versions(account, name)
        return versions[-1] if versions else None

    def active(self, account: str) -> list[SkillVersion]:
        """Dernière version de chaque skill appris actif (non désactivé)."""
        out = []
        for name in self.store.skill_names(account):
            v = self.current(account, name)
            if v is not None and v.active:
                out.append(v)
        return out

    def history(self, account: str, name: str | None = None, version: int | None = None,
                diff_from: int | None = None) -> dict[str, Any]:
        if not name:
            items = []
            for n in self.store.skill_names(account):
                v = self.current(account, n)
                if v is not None:
                    items.append({"name": n, "version": v.version, "active": v.active, "at": v.at})
            return {"skills": items}
        versions = self.store.skill_versions(account, name)
        if not versions:
            raise LearningError(404, "unknown_skill", f"skill appris inconnu : {name}")
        out: dict[str, Any] = {"name": name, "current": versions[-1].version, "active": versions[-1].active,
                               "versions": [v.to_json(content=False) for v in versions]}
        by = {v.version: v for v in versions}
        if version is not None or diff_from is not None:
            to = by.get(int(version)) if version is not None else versions[-1]
            if to is None:
                raise LearningError(404, "unknown_version", f"{name} : version {version} inconnue")
            out["version"] = to.to_json()
            if diff_from is not None:
                frm = by.get(int(diff_from))
                if frm is None:
                    raise LearningError(404, "unknown_version", f"{name} : version {diff_from} inconnue")
                out["diff"] = "".join(difflib.unified_diff(
                    frm.content.splitlines(keepends=True), to.content.splitlines(keepends=True),
                    fromfile=f"{name}@v{frm.version}", tofile=f"{name}@v{to.version}"))
        return out

    # ── écritures ──
    def _write(self, account: str, v: SkillVersion) -> SkillVersion:
        try:
            self.store.add_skill_version(account, v)
        except LearningConflict as e:
            raise LearningError(409, "conflict", str(e)) from e
        self._materialize(account, v)
        kind = {"create": "skill.created", "update": "skill.updated", "revert": "skill.reverted",
                "disable": "skill.disabled"}[v.op]
        detail: dict[str, Any] = {"sha256": v.sha256, "active": v.active}
        if v.reverted_to is not None:
            detail["reverted_to"] = v.reverted_to
        if v.approval_id:
            detail["approval_id"] = v.approval_id
        self.store.journal_append(account, JournalEntry(kind=kind, subject=v.name, at=v.at, actor=v.actor,
                                                        version=v.version, thread_id=v.thread_id,
                                                        turn_index=v.turn_index, detail=detail))
        if self.emit is not None:
            try:
                self.emit("skill.learned", {"account": account, "name": v.name, "version": v.version, "op": v.op,
                                            "active": v.active, "thread_id": v.thread_id,
                                            "turn_index": v.turn_index, "actor": v.actor,
                                            "_audience": [account]})  # le compte seul, dans son locataire
            except Exception:
                log.debug("emit skill.learned a levé", exc_info=True)
        return v

    def _materialize(self, account: str, v: SkillVersion) -> None:
        """Lokkal : le SKILL.md actif sous la racine de l'utilisateur (le catalogue des skills le lit et le rescanne à
        chaque lecture) ; version désactivée : le dossier du skill est retiré (l'historique reste en base d'état)."""
        root = self.skills_dir(account)
        if root is None:
            return
        root.mkdir(parents=True, exist_ok=True)
        target = root / v.name
        real_root, real = os.path.realpath(root), os.path.realpath(target)
        if not real.startswith(real_root + os.sep) or target.is_symlink():
            raise LearningError(409, "bad_path", "dossier du skill hors de la racine de l'utilisateur")
        if not v.active:
            if target.is_dir():
                shutil.rmtree(real)
            return
        target.mkdir(exist_ok=True)
        tmp = target / "SKILL.md.tmp"
        tmp.write_text(v.content, encoding="utf-8")
        tmp.replace(target / "SKILL.md")

    def _prov(self, ctx: ToolContext | None, actor: str) -> dict[str, Any]:
        return {"thread_id": (ctx.thread_id if ctx is not None else "") or "",
                "turn_index": ctx.turn_index if ctx is not None and ctx.channel != DIRECT_CHANNEL else None,
                "approval_id": getattr(ctx, "approval_id", None), "actor": actor, "at": self.now_fn()}

    def create(self, args: dict[str, Any], account: str, ctx: ToolContext | None, update: bool = False,
               actor: str = "agent") -> SkillVersion:
        name, desc, body = self._fields(args, account, update=update)
        content = render_skill(name, self.scrub(desc), self.scrub(body))
        self.scan(name, content)  # de nouveau à l'exécution : la carte a pu attendre, le scanner a pu changer
        last = self.current(account, name)
        if update and last is not None and last.active and last.content == content:
            raise LearningError(409, "unchanged", f"{name} : contenu identique à la version {last.version}")
        return self._write(account, SkillVersion(name=name, version=(last.version + 1) if last else 1,
                                                 content=content, sha256=_sha(content),
                                                 op="update" if update else "create", **self._prov(ctx, actor)))

    @staticmethod
    def _revert_args(args: dict[str, Any]) -> tuple[str, int]:
        name = str(args.get("name") or "").strip()
        try:
            version = int(args.get("version"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise LearningError(400, "bad_version", "version : entier (0 = désactiver)") from None
        if not NAME_RE.match(name) or version < 0:
            raise LearningError(400, "bad_version", "name et version (≥ 0) requis")
        return name, version

    def _revert_target(self, account: str, name: str, version: int) -> tuple[SkillVersion, SkillVersion | None]:
        versions = self.store.skill_versions(account, name)
        if not versions:
            raise LearningError(404, "unknown_skill", f"skill appris inconnu : {name}")
        last = versions[-1]
        if version == 0:
            if not last.active:
                raise LearningError(409, "unchanged", f"{name} est déjà désactivé")
            return last, None
        target = next((v for v in versions if v.version == version), None)
        if target is None or not target.active:
            raise LearningError(404, "unknown_version", f"{name} : version {version} inconnue ou désactivée")
        if last.active and last.content == target.content:
            raise LearningError(409, "unchanged", f"{name} : la version {version} est déjà le contenu actif")
        return last, target

    def revert(self, args: dict[str, Any], account: str, ctx: ToolContext | None, actor: str = "agent") -> SkillVersion:
        name, version = self._revert_args(args)
        last, target = self._revert_target(account, name, version)
        if target is not None:
            self.scan(name, target.content)  # une ancienne version repasse la garde d'aujourd'hui
            v = SkillVersion(name=name, version=last.version + 1, content=target.content, sha256=target.sha256,
                             op="revert", reverted_to=version, **self._prov(ctx, actor))
        else:
            v = SkillVersion(name=name, version=last.version + 1, content="", sha256="", op="disable", active=False,
                             reverted_to=0, **self._prov(ctx, actor))
        return self._write(account, v)

    def skill_json(self, v: SkillVersion) -> dict[str, Any]:
        from nokker.tools.skills import parse_frontmatter

        fm, body = parse_frontmatter(v.content)
        return {"name": v.name, "category": LEARNED_FAMILY, "description": str(fm.get("description") or ""),
                "source": "learned", "version": v.version, "content": body.lstrip("\n")}


@dataclass
class LearningToolSource:
    """`ToolSource` des skills appris. `account_of(ctx)` : le compte de l'appel (Lokkal : toujours le propriétaire ;
    nokker.ai : le compte `us_…` du tour), None = refusé. `catalog` : ajoute `skill_list` et `skill_view` sur les
    skills appris (nokker.ai sans packs ; dans Lokkal, le catalogue des skills les lit déjà sur disque)."""

    service: LearnedSkills
    account_of: Callable[[ToolContext], str | None]
    catalog: bool = False

    def specs(self) -> list[ToolSpec]:
        specs = [ToolSpec(name=n, description=d, parameters=p, effect=e, source="builtin:learning")
                 for n, d, e, p in _SPECS]
        if self.catalog:
            specs += [
                ToolSpec(name="skill_list", description="Liste les skills appris (nom, description).",
                         parameters={"type": "object", "properties": {}}, effect=Effect.READ,
                         source="builtin:learning"),
                ToolSpec(name="skill_view", description="Lit le mode d'emploi d'un skill appris.",
                         parameters={"type": "object", "required": ["name"],
                                     "properties": {"name": {"type": "string"}}},
                         effect=Effect.READ, source="builtin:learning")]
        return specs

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        account = self.account_of(ctx)
        if not account:
            return out("refusé : les skills appris se rangent dans un compte", True)
        a = arguments or {}
        svc = self.service
        actor = "owner" if ctx.channel == DIRECT_CHANNEL else "agent"
        try:
            if name in ("skill_create", "skill_update"):
                v = svc.create(a, account, ctx, update=name == "skill_update", actor=actor)
                return out({"learned": v.name, "version": v.version, "op": v.op,
                            "note": "visible dans skill_list (famille learned) ; skill_history pour l'historique"})
            if name == "skill_revert":
                v = svc.revert(a, account, ctx, actor=actor)
                return out({"skill": v.name, "version": v.version, "op": v.op, "active": v.active,
                            "reverted_to": v.reverted_to})
            if name == "skill_history":
                return out(svc.history(account, a.get("name"), a.get("version"), a.get("diff_from")))
            if name == "skill_list" and self.catalog:
                return out({"skills": [{k: s[k] for k in ("name", "category", "description", "source")}
                                       for s in map(svc.skill_json, svc.active(account))]})
            if name == "skill_view" and self.catalog:
                v = svc.current(account, str(a.get("name") or "").strip())
                if v is None or not v.active:
                    return out(f"compétence introuvable : {a.get('name')!r} (skill_list pour voir les noms)", True)
                return out(svc.skill_json(v))
        except LearningError as e:
            return out(f"refusé : {e.message}", True)
        except (TypeError, ValueError) as e:
            return out(f"arguments invalides : {e}", True)
        return out(f"outil inconnu : {name}", True)


@dataclass
class LearningGates:
    """Portes du noyau, plus la carte obligatoire des écritures d'apprentissage venues du modèle : `skill_create`,
    `skill_update`, `skill_revert` → validation et scan (refus sans carte si invalide ou dangereux), puis carte
    STANDARD que ni le mode, ni une règle `allow`, ni un GO écrit ne lèvent. Un refus des portes internes (`deny`,
    mode plan, invariants) passe avant. L'appel direct du propriétaire (hors tour) suit les portes internes."""

    inner: Any
    service: LearnedSkills
    account_of: Callable[[ToolContext], str | None]

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]:
        verdicts = self.inner.action(call, spec, turn)
        if call.name not in CARD_TOOLS or getattr(turn, "channel", None) == DIRECT_CHANNEL \
                or getattr(spec, "source", "") != "builtin:learning":
            return verdicts
        if any(v.decision is Decision.DENY for v in verdicts):
            return verdicts
        ctx = ToolContext(thread_id=turn.thread_id, turn_index=turn.index, channel=turn.channel, sender=turn.sender)
        account = self.account_of(ctx)
        try:
            if not account:
                raise LearningError(403, "no_account", "les skills appris se rangent dans un compte")
            reason = self.service.check(call.name, call.arguments or {}, account)
        except LearningError as e:
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=REFUSED_RULE, reason=e.message,
                            subject=call.id)]
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=CARD_RULE,
                        reason=f"apprentissage : {reason}", subject=call.id, strength="standard")]

    def switch(self, reply: str, turn: Turn, tier: Any) -> list[Verdict]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:  # permissions, floors, invariants… : ceux des portes enveloppées
        if name in ("inner", "service", "account_of"):
            raise AttributeError(name)
        return getattr(self.inner, name)


__all__ = ["CARD_RULE", "CARD_TOOLS", "LEARNED_FAMILY", "LEARNING_TOOLS", "REFUSED_RULE", "LearnedSkills",
           "LearningError", "LearningGates", "LearningToolSource", "render_skill"]
