"""nokker/tools/packs.py — packs d'agents (lot X16, docs/en/packs.md).

Un pack est un dossier `nokker/packs/<nom>/` :

    pack.yaml                         manifeste : nom, description, outils intégrés utilisés, connecteurs attendus,
                                      serveurs MCP attendus par RÔLE (outils, effets, NOMS de secrets), besoins
    skills/<famille>/<skill>/SKILL.md des compétences au format que lit déjà `SkillToolSource`
    agents/<id>.yaml                  des modèles d'agents : les champs d'un `agent_draft` (BUBBLES.md §9.5)

Installer un pack (`pack_install`, effet DESTRUCTIVE = carte, comme `skill_install`) : le pack est relu et validé,
chaque compétence et chaque modèle passe par la garde des compétences (source « community » : seul un
verdict `safe` passe, garde en ÉCHEC FERMÉ comme `skill_install`), les compétences sont copiées dans la racine
utilisateur (où `SkillToolSource` les rescanne à chaque lecture), puis chaque modèle d'agent est PROPOSÉ en bulle
`agent_draft`. Aucun agent n'est créé ni armé ici : l'activation et l'armement restent des gestes de l'humain.

Prudence, vérifiée au chargement de chaque pack (et par les tests) :
  - un modèle déclare TOUS ses outils (liste blanche du passage) ; `auto_approve` n'en lève qu'un sous-ensemble ;
  - `auto_approve` ne lève que des lectures (et `mail_draft`, écriture qui reste dans le compte) : jamais un envoi
    externe, une écriture prod, une commande, une dépense ; rien du tout sur un déclencheur d'événement ;
  - un pack ne baisse jamais l'effet d'un outil : l'effet retenu est le plus prudent entre celui du noyau et celui que
    déclare le pack ; les effets déclarés des outils MCP RELÈVENT la table d'effets au démarrage (jamais l'inverse) ;
  - identifiants : des NOMS de secrets (`secret:NOM`), jamais une valeur ; aucun placement (X15 lit `needs`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

log = logging.getLogger("nokker.packs")

PACKS_ROOT = Path(__file__).resolve().parent.parent / "packs"
RECORDS_DIR = ".packs"  # sous la racine utilisateur des compétences ; dossier caché = jamais pris pour un skill
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_SKILL_DESC = 60  # docs/en/skills.md : la description de l'index tient en 60 caractères
_MANIFEST_KEYS = {"name", "version", "title", "description", "tools", "connectors", "mcp", "needs"}
_AGENT_KEYS = {"id", "name", "role", "model", "instructions", "trigger", "tools", "auto_approve", "secrets", "budget",
               "needs"}
_NEEDS_KEYS = {"gpu"}
_CONNECTORS = {"mail", "calendar"}

# Gravité des effets : un pack peut relever un effet, jamais le baisser.
RANK: dict[Effect, int] = {Effect.READ: 0, Effect.WRITE: 1, Effect.SECRET: 2, Effect.SPEND: 2,
                           Effect.SEND_EXTERNAL: 2, Effect.DESTRUCTIVE: 3, Effect.SEND_BULK: 3}
# Écritures qu'un modèle d'agent peut lever sans carte : elles restent dans le compte de l'utilisateur et se défont.
# L'armement d'un agent qui en lève une passe quand même par la carte forte (§9.5).
PRUDENT_WRITES = frozenset({"mail_draft"})

_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("pack_list", "Liste les packs d'agents (compétences + modèles d'agents) ; avec name, le détail d'un pack.",
     Effect.READ, {"type": "object", "properties": {
         "name": {"type": "string", "description": "pack à détailler (ex. mail)"}}}),
    ("pack_install", "Installe un pack : ses compétences (scannées par la garde) et la PROPOSITION de ses agents "
     "(cartes agent_draft, rien n'est armé). Carte d'approbation.", Effect.DESTRUCTIVE, {
         "type": "object", "required": ["name"], "properties": {
             "name": {"type": "string"},
             "bind": {"type": "object", "description": "facultatif : « rôle.outil » → nom réel de l'outil du "
                                                       "serveur MCP de l'utilisateur ; « secret:NOM » → autre NOM "
                                                       "du coffre"}}}),
)
PACK_TOOLS = tuple(name for name, *_ in _SPECS)


class PackError(ValueError):
    pass


# ─────────────────────────────── effets connus du noyau ───────────────────────────────


def builtin_effects() -> dict[str, Effect]:
    """Effet déclaré de chaque outil intégré qu'un pack peut nommer (table d'effets + sources du noyau)."""
    from nokker.tools import code, mcp_config, skills, ssh
    from nokker.tools.effects import DEFAULT_EFFECTS

    out: dict[str, Effect] = dict(DEFAULT_EFFECTS)
    for name, _d, effect, *_ in (*code._SPECS, *code._PROCESS_SPECS, *skills._SPECS, *ssh._specs(),
                                 *mcp_config._SPECS, *_SPECS):
        out[name] = effect
    out.update({"memory_note": Effect.WRITE, "memory": Effect.WRITE, "agent_draft": Effect.READ,
                "page_create": Effect.WRITE})
    return out


def stricter(a: Effect, b: Effect | None) -> Effect:
    return a if b is None or RANK[a] >= RANK[b] else b


# ─────────────────────────────── modèle ───────────────────────────────


@dataclass
class McpRole:
    """Un serveur MCP que l'utilisateur ajoute lui-même, décrit par ce qu'il doit savoir faire."""

    name: str
    purpose: str
    examples: list[str]
    secrets: list[str]
    tools: dict[str, tuple[str, Effect]]  # outil abstrait → (nom par défaut, effet)
    optional: bool = False


@dataclass
class AgentTemplate:
    id: str
    fields: dict[str, Any]
    needs: dict[str, Any]
    path: Path


@dataclass
class Pack:
    name: str
    version: int
    title: str
    description: str
    tools: list[str]
    connectors: list[str]
    mcp: dict[str, McpRole]
    needs: dict[str, Any]
    skills: list[tuple[str, str, str, Path]]  # (famille, nom, description, dossier)
    agents: list[AgentTemplate]
    path: Path

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description,
                "skills": [n for _f, n, _d, _p in self.skills], "agents": [a.id for a in self.agents],
                "connectors": self.connectors, "mcp": sorted(self.mcp), "needs": self.needs}

    def detail(self) -> dict[str, Any]:
        out = self.summary()
        out["skills"] = [{"name": n, "family": f, "description": d} for f, n, d, _p in self.skills]
        out["agents"] = [{"id": a.id, "name": a.fields["name"], "role": a.fields["role"],
                          "trigger": a.fields.get("trigger") or {"kind": "manual"},
                          "auto_approve": list(a.fields.get("auto_approve") or []),
                          "needs": a.needs} for a in self.agents]
        out["mcp"] = [{"role": r.name, "purpose": r.purpose, "examples": r.examples, "optional": r.optional,
                       "secrets": r.secrets,
                       "tools": {t: {"default": d, "effect": e.value} for t, (d, e) in r.tools.items()}}
                      for r in self.mcp.values()]
        out["tools"] = self.tools
        return out


def _str(raw: Any, what: str, limit: int, required: bool = True) -> str:
    text = " ".join(str(raw or "").split())
    if required and not text:
        raise PackError(f"{what} manquant")
    if len(text) > limit:
        raise PackError(f"{what} : {limit} caractères au plus")
    return text


def _secret_names(raw: Any, what: str) -> list[str]:
    from nokker.agents.spec import norm_secrets
    from nokker.agents.types import AgentError

    try:
        return norm_secrets(raw)
    except AgentError as e:
        raise PackError(f"{what} : {e}") from None


def _needs(raw: Any, what: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - _NEEDS_KEYS:
        raise PackError(f"{what}.needs : seulement {sorted(_NEEDS_KEYS)}")
    out: dict[str, Any] = {}
    if "gpu" in raw:
        if not isinstance(raw["gpu"], bool):
            raise PackError(f"{what}.needs.gpu : booléen")
        out["gpu"] = raw["gpu"]
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise PackError(f"{path.name} illisible : {e}") from None
    if not isinstance(data, dict):
        raise PackError(f"{path.name} : un objet YAML attendu")
    return data


def _load_role(name: str, raw: Any) -> McpRole:
    if not _ROLE.match(name) or not isinstance(raw, dict):
        raise PackError(f"mcp.{name} : rôle invalide")
    tools: dict[str, tuple[str, Effect]] = {}
    for tname, spec in (raw.get("tools") or {}).items():
        if not _ROLE.match(str(tname)) or not isinstance(spec, dict):
            raise PackError(f"mcp.{name}.tools.{tname} : outil invalide")
        default = str(spec.get("default") or "")
        if not _TOOL.match(default):
            raise PackError(f"mcp.{name}.tools.{tname}.default : nom d'outil invalide")
        try:
            effect = Effect(str(spec.get("effect") or ""))
        except ValueError:
            raise PackError(f"mcp.{name}.tools.{tname}.effect : {[e.value for e in Effect]}") from None
        tools[str(tname)] = (default, effect)
    if not tools:
        raise PackError(f"mcp.{name} : au moins un outil")
    return McpRole(name=name, purpose=_str(raw.get("purpose"), f"mcp.{name}.purpose", 240),
                   examples=[_str(x, f"mcp.{name}.examples", 120) for x in (raw.get("examples") or [])],
                   secrets=_secret_names(raw.get("secrets"), f"mcp.{name}.secrets"), tools=tools,
                   optional=bool(raw.get("optional", False)))


def _load_skills(pack_dir: Path) -> list[tuple[str, str, str, Path]]:
    from nokker.tools.skills import SkillToolSource, parse_frontmatter

    root = pack_dir / "skills"
    out = []
    for d in SkillToolSource._skill_dirs(root):
        rel = d.relative_to(root).parts
        if len(rel) != 2:
            raise PackError(f"compétence {d.relative_to(pack_dir)} : attendu skills/<famille>/<skill>/SKILL.md")
        fm, body = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
        name = str(fm.get("name") or "")
        if name != d.name:
            raise PackError(f"compétence {d.name} : le champ name doit être le nom du dossier")
        desc = _str(fm.get("description"), f"compétence {name}.description", MAX_SKILL_DESC)
        if len(body.strip()) < 200:
            raise PackError(f"compétence {name} : mode d'emploi vide ou trop court")
        out.append((rel[0], name, desc, d))
    if not out:
        raise PackError("pack sans compétence")
    names = [n for _f, n, _d, _p in out]
    if len(set(names)) != len(names):
        raise PackError("deux compétences du pack portent le même nom")
    return out


def load_pack(pack_dir: Path) -> Pack:
    """Pack relu et validé (modèles d'agents compris, avec la liaison par défaut). Lève `PackError`."""
    pack_dir = Path(pack_dir)
    data = _read_yaml(pack_dir / "pack.yaml")
    if set(data) - _MANIFEST_KEYS:
        raise PackError(f"pack.yaml : clés inconnues {sorted(set(data) - _MANIFEST_KEYS)}")
    name = str(data.get("name") or "")
    if not _NAME.match(name) or name != pack_dir.name:
        raise PackError(f"pack {pack_dir.name} : name doit être le nom du dossier (minuscules, chiffres, tirets)")
    known = builtin_effects()
    tools = [str(t) for t in (data.get("tools") or [])]
    unknown = [t for t in tools if t not in known]
    if unknown:
        raise PackError(f"pack {name} : outils intégrés inconnus {unknown}")
    connectors = [str(c) for c in (data.get("connectors") or [])]
    if set(connectors) - _CONNECTORS:
        raise PackError(f"pack {name} : connecteurs {sorted(_CONNECTORS)}")
    roles = {str(k): _load_role(str(k), v) for k, v in (data.get("mcp") or {}).items()}
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1:
        raise PackError(f"pack {name} : version entière ≥ 1")
    pack = Pack(name=name, version=version, title=_str(data.get("title"), "title", 60),
                description=_str(data.get("description"), "description", 240), tools=tools, connectors=connectors,
                mcp=roles, needs=_needs(data.get("needs"), "pack"), skills=_load_skills(pack_dir), agents=[],
                path=pack_dir)
    seen: set[str] = set()
    for f in sorted((pack_dir / "agents").glob("*.yaml")):
        raw = _read_yaml(f)
        if set(raw) - _AGENT_KEYS:
            raise PackError(f"agents/{f.name} : clés inconnues {sorted(set(raw) - _AGENT_KEYS)}")
        tid = str(raw.get("id") or "")
        if not _NAME.match(tid) or tid != f.stem or tid in seen:
            raise PackError(f"agents/{f.name} : id = nom du fichier, unique")
        seen.add(tid)
        tpl = AgentTemplate(id=tid, fields={k: v for k, v in raw.items() if k not in ("id", "needs")},
                            needs=_needs(raw.get("needs"), f"agents/{f.name}"), path=f)
        pack.agents.append(tpl)
        resolve_agent(pack, tpl)  # liaison par défaut : un modèle invalide refuse le pack entier
    if not pack.agents:
        raise PackError(f"pack {name} : au moins un modèle d'agent")
    return pack


# ─────────────────────────────── liaison et prudence ───────────────────────────────


def _ref(pack: Pack, tool: str, bind: dict[str, str]) -> tuple[str, Effect]:
    """(nom réel, effet retenu) d'un outil d'un modèle : intégré déclaré par le pack, ou `rôle.outil`."""
    known = builtin_effects()
    if "." in tool:
        role, _, abstract = tool.partition(".")
        r = pack.mcp.get(role)
        if r is None or abstract not in r.tools:
            raise PackError(f"outil {tool} : rôle MCP ou outil non déclaré par le pack")
        default, effect = r.tools[abstract]
        real = str(bind.get(tool) or default)
        if not _TOOL.match(real):
            raise PackError(f"liaison {tool} : nom d'outil invalide")
        return real, stricter(effect, known.get(real))
    if tool not in pack.tools:
        raise PackError(f"outil {tool} : non déclaré dans pack.yaml (tools)")
    return tool, known[tool]


def resolve_agent(pack: Pack, tpl: AgentTemplate, bind: dict[str, str] | None = None
                  ) -> tuple[dict[str, Any], dict[str, Effect]]:
    """Arguments `agent_draft` du modèle (outils et secrets liés) et l'effet retenu de chacun de ses outils.
    Valide avec les règles du noyau (§9.5) puis la prudence des packs. Lève `PackError`."""
    from nokker.agents.spec import norm_budget, norm_rules, norm_trigger
    from nokker.agents.types import AgentError
    from nokker.qa.permissions import Rule

    bind = {str(k): str(v) for k, v in (bind or {}).items()}
    f = tpl.fields
    where = f"agent {pack.name}/{tpl.id}"
    effects: dict[str, Effect] = {}
    tools: list[str] = []
    for t in f.get("tools") or []:
        real, eff = _ref(pack, str(t), bind)
        effects[real] = eff
        if real not in tools:
            tools.append(real)
    if not tools:
        raise PackError(f"{where} : tools (liste blanche du passage) requis")
    rules: list[str] = []
    for text in f.get("auto_approve") or []:
        try:
            r = Rule.parse(str(text))
        except ValueError as e:
            raise PackError(f"{where} : {e}") from None
        real, _eff = _ref(pack, r.tool, bind)
        rules.append(f"{real}({r.pattern})" if r.pattern else real)
    secrets = []
    for s in f.get("secrets") or []:
        # « secret:NOM » → autre nom du coffre ; valeur vide = l'utilisateur n'en a pas besoin (serveur sans jeton,
        # connecteur relayé) : un nom absent du coffre ferait sauter chaque passage (§9.5).
        name = bind.get(f"secret:{s}", str(s))
        if name:
            secrets.append(name)
    try:
        args: dict[str, Any] = {
            "name": _str(f.get("name"), f"{where}.name", 80), "role": _str(f.get("role"), f"{where}.role", 200),
            "model": str(f.get("model") or ""), "tools": tools,
            "instructions": str(f.get("instructions") or "").strip(),
            "trigger": norm_trigger(f.get("trigger")), "secrets": _secret_names(secrets, f"{where}.secrets"),
            "budget": norm_budget(f.get("budget")), "auto_approve": norm_rules(rules)}
    except AgentError as e:
        raise PackError(f"{where} : {e}") from None
    if len(args["instructions"]) < 80:
        raise PackError(f"{where} : instructions vides ou trop courtes")
    if args["budget"] is None:
        raise PackError(f"{where} : budget requis (max_calls et/ou max_cost)")
    problems = auto_approve_problems(args, effects)
    if problems:
        raise PackError(f"{where} : " + " ; ".join(problems))
    return args, effects


def auto_approve_problems(args: dict[str, Any], effects: dict[str, Effect]) -> list[str]:
    """Pourquoi les `auto_approve` d'un agent ne sont pas prudents (liste vide = prudents)."""
    from nokker.qa.permissions import Rule

    event = (args.get("trigger") or {}).get("kind") == "event"
    out = []
    for text in args.get("auto_approve") or []:
        tool = Rule.parse(text).tool
        if tool not in effects:
            out.append(f"{text} : outil hors de la liste blanche de l'agent")
            continue
        eff = effects[tool]
        if eff is Effect.READ:
            continue
        if event:
            out.append(f"{text} : déclenché par un événement, l'agent ne lève aucune écriture")
        elif tool not in PRUDENT_WRITES:
            out.append(f"{text} : effet {eff.value}, jamais levé sans carte par un modèle de pack")
    return out


# ─────────────────────────────── catalogue ───────────────────────────────


def pack_dirs(root: Path = PACKS_ROOT) -> list[Path]:
    return sorted(p for p in Path(root).iterdir() if p.is_dir() and (p / "pack.yaml").is_file()) \
        if Path(root).is_dir() else []


def load_all(root: Path = PACKS_ROOT) -> tuple[list[Pack], dict[str, str]]:
    """(packs valides, erreurs par dossier). Un pack invalide n'est jamais proposé."""
    packs, errors = [], {}
    for d in pack_dirs(root):
        try:
            packs.append(load_pack(d))
        except PackError as e:
            errors[d.name] = str(e)
            log.warning("pack %s refusé : %s", d.name, e)
    return packs, errors


def installed_effect_overrides(home: Path, current: Callable[[str], Effect]) -> dict[str, str]:
    """Effets des outils MCP déclarés par les packs installés, quand ils sont PLUS prudents que l'effet actuel.
    `current(nom)` : l'effet que le noyau donne aujourd'hui (table + surcharges de la config)."""
    out: dict[str, str] = {}
    records = Path(home) / "skills" / RECORDS_DIR
    for f in sorted(records.glob("*.json")) if records.is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for tool, value in (data.get("mcp_effects") or {}).items():
                eff = Effect(str(value))
                if _TOOL.match(str(tool)) and RANK[eff] > RANK[current(str(tool))]:
                    prev = Effect(out[tool]) if tool in out else None
                    out[str(tool)] = stricter(eff, prev).value
        except (OSError, ValueError, AttributeError) as e:
            log.warning("registre de pack %s illisible, ignoré : %s", f.name, e)
    return out


def user_home(state_dir: Path | None) -> Path:
    """`$NOKKER_HOME` de Lokkal : le dossier d'état, sinon la même règle que `SkillToolSource`."""
    return Path(state_dir or os.environ.get("NOKKER_HOME") or "~/.nokker").expanduser()


def with_user_extensions(config: Any, state_dir: Path) -> Any:
    """`NokkerConfig` du démarrage de Lokkal + ce que l'utilisateur a posé depuis une session : serveurs MCP ajoutés
    (`mcp-servers.yaml`, après ceux de la config, un nom déjà pris est ignoré) et effets des outils MCP déclarés par
    les packs installés (ne font que relever la table)."""
    import dataclasses

    from nokker.tools.effects import EffectsTable
    from nokker.tools.mcp_config import FILE_NAME, load_user_servers, merge_servers

    raised = installed_effect_overrides(state_dir, EffectsTable.load(config.effect_overrides).effect_for)
    return dataclasses.replace(
        config, effect_overrides={**config.effect_overrides, **raised},
        mcp_tool_servers=merge_servers(list(config.mcp_tool_servers), load_user_servers(Path(state_dir) / FILE_NAME)))


# ─────────────────────────────── outils du modèle ───────────────────────────────


Proposer = Callable[[dict[str, Any], ToolContext, dict[str, Any]], ToolResult]


@dataclass
class PackToolSource:
    """`ToolSource` : `pack_list` (READ) et `pack_install` (DESTRUCTIVE). `home` : `$NOKKER_HOME` (la racine
    utilisateur des compétences est `home/skills`, la même que `SkillToolSource`). `propose(args, ctx, extra)` pose
    une bulle `agent_draft` (`nokker.bubbles.tools.propose_agent_draft`) ; None = les cartes sont rendues au modèle,
    qui les propose avec `agent_draft`."""

    home: Path | None = None
    packs_root: Path = PACKS_ROOT
    propose: Proposer | None = None
    audit: Callable[[dict[str, Any]], None] | None = None
    core: bool = False
    _lock: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        import threading

        self.home = user_home(self.home)
        self._lock = threading.Lock()
        if self.audit is None:
            self.audit = lambda ev: log.info("%s", json.dumps(ev, ensure_ascii=False, default=str))

    @property
    def user_root(self) -> Path:
        return Path(self.home) / "skills"

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:packs")
                for n, d, e, p in _SPECS]

    def _emit(self, **event: Any) -> None:
        try:
            self.audit(event)  # type: ignore[misc]
        except Exception:
            log.exception("audit en échec")

    def _record(self, name: str) -> dict[str, Any] | None:
        try:
            return json.loads((self.user_root / RECORDS_DIR / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        a = arguments or {}
        try:
            if name == "pack_list":
                return out(self.list_packs(a.get("name")))
            if name == "pack_install":
                bind = a.get("bind") or {}
                if not isinstance(bind, dict):
                    return out("bind doit être un objet", True)
                payload, err = self.install(str(a.get("name") or ""), bind, ctx)
                return out(payload, err)
        except PackError as e:
            return out(f"refusé : {e}", True)
        except Exception as e:  # noqa: BLE001 — l'erreur revient au modèle, la boucle continue
            from nokker.tools.skills import _clean

            return out(_clean(f"{type(e).__name__}: {e}"), True)
        return out(f"outil inconnu : {name}", True)

    def _find(self, name: str) -> Pack:
        d = Path(self.packs_root) / name
        if not _NAME.match(name) or not (d / "pack.yaml").is_file():
            known = ", ".join(p.name for p in pack_dirs(self.packs_root)) or "aucun"
            raise PackError(f"pack inconnu : {name!r} (packs : {known})")
        return load_pack(d)

    def list_packs(self, name: str | None = None) -> dict[str, Any]:
        if name:
            pack = self._find(str(name))
            detail = pack.detail()
            rec = self._record(pack.name)
            detail["installed"] = bool(rec)
            return detail
        packs, errors = load_all(self.packs_root)
        items = []
        for p in packs:
            item = p.summary()
            item["installed"] = bool(self._record(p.name))
            items.append(item)
        result: dict[str, Any] = {"packs": items}
        if errors:
            result["refused"] = sorted(errors)
        return result

    def _scan(self, pack: Pack) -> list[str]:
        """Rapports refusés (vide = tout passe). Lève `PackError` sans scanner : échec fermé."""
        from nokker.tools.skills import _clean, _guard_module

        guard = _guard_module()
        if guard is None:
            raise PackError("installation impossible ici : le scanner de sécurité des compétences n'est pas "
                            "disponible (rien n'a été installé)")
        refused = []
        targets = [p for _f, _n, _d, p in pack.skills]
        for path in targets:
            result, _ = guard.scan_skill_cached(path, source="community", cache_dir=Path(self.home) / ".scan-cache")
            allowed, _reason = guard.should_allow_install(result)
            if allowed is not True:
                refused.append(_clean(guard.format_scan_report(result)))
        # Le manifeste et les modèles d'agents aussi : leurs instructions entrent dans le contexte de chaque passage.
        for path in [pack.path / "pack.yaml", *(t.path for t in pack.agents)]:
            result = guard.scan_skill(path, source="community")
            allowed, _reason = guard.should_allow_install(result)
            if allowed is not True:
                refused.append(_clean(guard.format_scan_report(result)))
        return refused

    def install(self, name: str, bind: dict[str, Any], ctx: ToolContext) -> tuple[Any, bool]:
        pack = self._find(name)
        bind = {str(k): str(v) for k, v in bind.items()}
        bad = [k for k in bind if not (k.startswith("secret:") or "." in k)]
        if bad:
            return f"bind : clés « rôle.outil » ou « secret:NOM » seulement ({bad})", True
        drafts = []
        mcp_effects: dict[str, str] = {}
        for tpl in pack.agents:
            args, _effects = resolve_agent(pack, tpl, bind)
            extra = {"pack": pack.name, "template": tpl.id, "needs": {**pack.needs, **tpl.needs}}
            drafts.append((tpl, args, extra))
        for role in pack.mcp.values():
            for abstract, (default, effect) in role.tools.items():
                real = str(bind.get(f"{role.name}.{abstract}") or default)
                mcp_effects[real] = stricter(effect, Effect(mcp_effects[real]) if real in mcp_effects else None).value
        refused = self._scan(pack)
        if refused:
            self._emit(event="pack_install_refused", pack=pack.name, reason="scan")
            return "installation REFUSÉE par le scanner (rien n'a été installé).\n" + "\n".join(refused), True
        with self._lock:
            installed = self._copy(pack)
            record = {"name": pack.name, "version": pack.version, "installed_at": time.time(),
                      "skills": installed, "mcp_effects": mcp_effects, "bind": bind}
            rec_dir = self.user_root / RECORDS_DIR
            rec_dir.mkdir(parents=True, exist_ok=True)
            (rec_dir / f"{pack.name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
        proposed, to_propose = [], []
        for tpl, args, extra in drafts:
            if self.propose is None:
                to_propose.append({"template": tpl.id, "agent_draft": args, "needs": extra["needs"]})
                continue
            try:
                res = self.propose(args, ctx, extra)
            except Exception as e:  # noqa: BLE001 — une carte non posée ne défait pas l'installation
                res = ToolResult(call_id="", name="agent_draft", content=f"{type(e).__name__}: {e}", is_error=True)
            if res.is_error:
                to_propose.append({"template": tpl.id, "agent_draft": args, "needs": extra["needs"],
                                   "error": res.content})
                continue
            proposed.append({"template": tpl.id, "bubble_id": json.loads(res.content).get("bubble_id")})
        self._emit(event="pack_installed", pack=pack.name, version=pack.version, skills=installed,
                   proposed=[p["template"] for p in proposed])
        payload: dict[str, Any] = {
            "installed": pack.name, "skills": installed, "proposed": proposed,
            "armed": [], "note": "aucun agent n'est créé ni armé : chaque carte s'active par l'utilisateur, et "
                                 "armer une écriture demande sa carte forte"}
        if to_propose:
            payload["to_propose"] = to_propose
            payload["next"] = "propose chaque modèle avec agent_draft (mêmes arguments), sans rien changer"
        setup = []
        if pack.connectors:
            setup.append(f"connecteurs à poser : {', '.join(pack.connectors)}")
        for role in pack.mcp.values():
            setup.append(f"serveur MCP « {role.name} »{' (facultatif)' if role.optional else ''} : {role.purpose}"
                         + (f" ; secrets par NOM : {', '.join(role.secrets)}" if role.secrets else ""))
        if pack.mcp:
            setup.append("les effets déclarés des outils MCP (jamais plus bas que ceux du noyau) s'appliquent au "
                         "prochain redémarrage du noyau")
        if setup:
            payload["setup"] = setup
        return payload, False

    def _copy(self, pack: Pack) -> list[str]:
        """Copie les compétences dans la racine utilisateur. Refuse AVANT toute copie si un dossier existe déjà sans
        venir de ce pack (jamais d'écrasement d'une compétence de l'utilisateur)."""
        root = self.user_root
        prev = set((self._record(pack.name) or {}).get("skills") or [])
        plan = []
        for family, sname, _d, src in pack.skills:
            rel = f"{family}/{sname}"
            dest = root / family / sname
            if (dest.exists() or dest.is_symlink()) and rel not in prev:
                raise PackError(f"une compétence existe déjà à {rel} (retire-la d'abord : skill_remove)")
            if dest.is_symlink():
                raise PackError(f"{rel} est un lien symbolique : refusé")
            plan.append((rel, src, dest))
        done = []
        for rel, src, dest in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}-"))
            try:
                shutil.copytree(src, tmp / "s", symlinks=True)
                if dest.exists():
                    shutil.rmtree(dest)
                os.replace(tmp / "s", dest)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            done.append(rel)
        return done


__all__ = ["PACKS_ROOT", "PACK_TOOLS", "PRUDENT_WRITES", "Pack", "PackError", "PackToolSource", "auto_approve_problems",
           "builtin_effects", "installed_effect_overrides", "load_all", "load_pack", "resolve_agent", "user_home",
           "with_user_extensions"]
