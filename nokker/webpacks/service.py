"""nokker/webpacks/service.py — packs d'agents chez nokker.ai, dans le périmètre d'un compte (docs/en/packs.md,
docs/LOKKAL.md §7.30).

Le contenu reste celui livré avec le noyau (`nokker/packs/*`) : aucun pack tiers, rien de téléchargé. Installer un pack
pour un compte :

  - **ce qui va au web** (`web_split`) : les compétences qui ne demandent pas la machine (frontmatter sans
    `locality: local`) et les modèles d'agents dont TOUS les outils existent au web (`WEB_TOOLS` : compétences, web,
    courrier et agenda du compte, cartes) et sans besoin de GPU ;
  - **ce qui reste à la machine** : les compétences `local`, les modèles qui appellent un serveur MCP (rôle du pack),
    le bac à sable, les hôtes ou la mémoire longue, ou qui demandent un GPU. Jamais installé ici ; le résultat le dit
    (`partial`, `machine`) et, si une machine liée déclare la ressource, propose le relais X21 (`handoff`, créé
    seulement sur demande : rien ne s'exécute avant l'acceptation de l'utilisateur) ;
  - **garde** : chaque compétence installée, le manifeste et chaque modèle installé passent par la garde des
    compétences (source « community », seul un verdict sûr passe, échec FERMÉ sans le scanner, comme en local) ;
  - **rien n'est copié** : la ligne du compte (`nokker_pack_installs`, RLS) garde les noms et l'EMPREINTE de chaque
    compétence au moment de la garde. Les outils `skill_list` / `skill_view` du compte relisent le contenu livré et le
    comparent à l'empreinte : un contenu changé depuis (mise à jour du noyau) est masqué jusqu'à une réinstallation,
    qui repasse la garde ;
  - **agents** : PROPOSÉS en cartes `agent_draft` dans un fil du compte, jamais créés ni armés ici (mêmes règles que
    la carte du modèle, §9.5).

Retirer (`remove`, idempotent) : la ligne part, les cartes non activées se ferment, et les agents déjà créés depuis le
pack (champ `origin`) ne sont jamais touchés : une carte `pack_agents` propose de les désarmer ou de les supprimer.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext
from nokker.tools.packs import (
    _NAME, PACKS_ROOT, AgentTemplate, Pack, PackError, load_all, load_pack, resolve_agent,
)

log = logging.getLogger("nokker.webpacks")

# Outils qu'un compte de nokker.ai a pour ses tours et ses agents : compétences (celles de ses packs), recherche web,
# courrier et agenda de SES connecteurs, cartes, packs. Tout autre outil (fichiers, terminal, processus, ssh, serveurs
# MCP, mémoire longue) est sur une machine.
WEB_TOOLS = frozenset({
    "skill_view", "skill_list", "web_search", "web_fetch", "mail_search", "mail_read", "mail_draft", "mail_move",
    "mail_trash", "mail_send", "calendar_events", "calendar_create", "calendar_update", "calendar_delete",
    "agent_draft", "bubble_show", "pack_list", "pack_install", "pack_remove"})


class WebPackError(Exception):
    """Erreur de route : `status` HTTP, `code` stable, message ; `payload` : détails servis avec l'erreur."""

    def __init__(self, status: int, code: str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.payload = status, code, message, dict(payload or {})


# ─────────────────────────────── ce qui va au web ───────────────────────────────


@dataclass
class WebSplit:
    skills: list[tuple[str, str, str, Path]]  # (famille, nom, description, dossier) installables au web
    templates: list[tuple[AgentTemplate, dict[str, Any], dict[str, Any]]]  # (modèle, carte agent_draft, extra)
    machine_skills: list[str]
    machine_agents: list[dict[str, Any]]  # [{template, needs: [{kind: mcp|tool|gpu, name?}]}]


def template_needs(pack: Pack, tpl: AgentTemplate) -> list[dict[str, Any]]:
    """Ce qu'un modèle demande à une machine : rôles MCP, outils absents du web, GPU. Vide = il tourne au web."""
    needs: list[dict[str, Any]] = []
    for raw in tpl.fields.get("tools") or []:
        tool = str(raw)
        need = {"kind": "mcp", "name": tool.partition(".")[0]} if "." in tool else \
            ({"kind": "tool", "name": tool} if tool not in WEB_TOOLS else None)
        if need is not None and need not in needs:
            needs.append(need)
    if {**pack.needs, **tpl.needs}.get("gpu"):
        needs.append({"kind": "gpu"})
    return needs


def web_split(pack: Pack, bind: dict[str, str] | None = None) -> WebSplit:
    """Partage d'un pack entre le web et une machine. Lève `PackError` (liaison invalide)."""
    out = WebSplit(skills=[s for s in pack.skills if s[1] not in pack.local_skills], templates=[],
                   machine_skills=[n for _f, n, _d, _p in pack.skills if n in pack.local_skills], machine_agents=[])
    for tpl in pack.agents:
        needs = template_needs(pack, tpl)
        if needs:
            out.machine_agents.append({"template": tpl.id, "needs": needs})
            continue
        args, _effects = resolve_agent(pack, tpl, bind or {})
        out.templates.append((tpl, args, {"pack": pack.name, "template": tpl.id,
                                          "needs": {**pack.needs, **tpl.needs}}))
    return out


# ─────────────────────────────── stockage ───────────────────────────────


@dataclass
class PackInstall:
    account: str
    pack: str
    version: int
    skills: list[dict[str, str]]  # [{family, name, sha256}]
    templates: list[str]
    machine: dict[str, Any]
    bind: dict[str, str]
    proposed: list[dict[str, str]]  # [{template, bubble_id}]
    installed_at: float


class MemoryPackStore:
    """Packs installés (tests, dev). `PgPackStore` (nokker/webpacks/pg.py) a la même interface, sous RLS."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], PackInstall] = {}
        self.lock = threading.Lock()

    def get(self, tenant: str, account: str, pack: str) -> PackInstall | None:
        with self.lock:
            r = self.rows.get((tenant, account, pack))
            return copy.deepcopy(r) if r is not None else None

    def list(self, tenant: str, account: str) -> list[PackInstall]:
        with self.lock:
            return [copy.deepcopy(r) for (t, a, _p), r in sorted(self.rows.items()) if t == tenant and a == account]

    def put(self, tenant: str, rec: PackInstall) -> None:
        with self.lock:
            self.rows[(tenant, rec.account, rec.pack)] = copy.deepcopy(rec)

    def delete(self, tenant: str, account: str, pack: str) -> bool:
        with self.lock:
            return self.rows.pop((tenant, account, pack), None) is not None


# ─────────────────────────────── service ───────────────────────────────


def _bad_bind(bind: Any) -> dict[str, str]:
    if bind in (None, ""):
        return {}
    if not isinstance(bind, dict):
        raise WebPackError(400, "bad_bind", "bind doit être un objet")
    clean = {str(k): str(v) for k, v in bind.items()}
    bad = [k for k in clean if not (k.startswith("secret:") or "." in k)]
    if bad:
        raise WebPackError(400, "bad_bind", f"bind : clés « rôle.outil » ou « secret:NOM » seulement ({bad})")
    return clean


@dataclass
class WebPacks:
    """Packs d'un compte de nokker.ai. `hub` (BubbleHub, avec `agents_hub`) : cartes et agents ; `links` (LinkService)
    et `handoffs` (HandoffService) : machines liées et relais X21, facultatifs. Toutes les méthodes s'appellent DANS
    le locataire du compte (`tenant_scope`), le compte nommé (`us_…`) ; un fil donné est un fil du noyau (identifiant
    déjà rangé sous le locataire) dont le compte doit être membre."""

    store: Any = field(default_factory=MemoryPackStore)
    packs_root: Path = PACKS_ROOT
    hub: Any = None
    links: Any = None
    handoffs: Any = None
    audit: Callable[[dict[str, Any]], None] | None = None
    clock: Callable[[], float] = time.time

    def _emit(self, **event: Any) -> None:
        try:
            (self.audit or (lambda ev: log.info("%s", json.dumps(ev, ensure_ascii=False, default=str))))(event)
        except Exception:  # noqa: BLE001 — l'audit ne casse jamais une route
            log.exception("audit en échec")

    @staticmethod
    def _tenant() -> str:
        from nokker.tenancy.context import current_tenant

        return current_tenant()

    def _pack(self, name: str) -> Pack:
        d = Path(self.packs_root) / str(name or "")
        if not _NAME.match(str(name or "")) or not (d / "pack.yaml").is_file():
            raise WebPackError(404, "unknown_pack", f"pack inconnu : {str(name)[:40]!r}")
        try:
            return load_pack(d)
        except PackError as e:
            raise WebPackError(409, "invalid_pack", f"pack refusé : {e}") from None

    def _member(self, thread_id: str | None, account: str) -> None:
        if not thread_id:
            return
        if self.hub is None:
            raise WebPackError(503, "no_bubbles", "pas de bulles sur cette instance : installe sans thread_id")
        from nokker.threads.types import ThreadError

        try:
            self.hub.require_member(thread_id, account)
        except ThreadError:
            raise WebPackError(404, "unknown_thread", "fil inconnu") from None

    def _agents_of(self, account: str, pack: str) -> list[Any]:
        agents = getattr(self.hub, "agents_hub", None) if self.hub is not None else None
        return [a for a in agents.from_pack(pack) if a.created_by == account] if agents is not None else []

    # ── lecture ──

    @staticmethod
    def _split_view(split: WebSplit) -> tuple[dict[str, Any], dict[str, Any]]:
        return ({"skills": [n for _f, n, _d, _p in split.skills], "agents": [t.id for t, _a, _e in split.templates]},
                {"skills": list(split.machine_skills), "agents": [dict(a) for a in split.machine_agents]})

    def catalog(self, account: str) -> dict[str, Any]:
        tenant = self._tenant()
        mine = {r.pack for r in self.store.list(tenant, account)}
        packs, errors = load_all(self.packs_root)
        items = []
        for p in packs:
            item = p.summary()
            item["installed"] = p.name in mine
            try:
                item["web"], item["machine"] = self._split_view(web_split(p))
            except PackError:
                continue
            items.append(item)
        out: dict[str, Any] = {"packs": items}
        if errors:
            out["refused"] = sorted(errors)
        return out

    def detail(self, account: str, name: str) -> dict[str, Any]:
        pack = self._pack(name)
        tenant = self._tenant()
        rec = self.store.get(tenant, account, pack.name)
        out = pack.detail()
        out["web"], out["machine"] = self._split_view(web_split(pack, rec.bind if rec else None))
        out["installed"] = rec is not None
        if rec is not None:
            out["install"] = {"version": rec.version, "installed_at": rec.installed_at,
                              "skills": [s["name"] for s in rec.skills], "templates": list(rec.templates),
                              "proposed": list(rec.proposed), "bind": dict(rec.bind)}
        out["created_agents"] = [{"id": a.id, "name": a.name, "status": a.status, "armed": a.armed,
                                  "template": (a.origin or {}).get("template")} for a in self._agents_of(account, name)]
        return out

    # ── installation ──

    def _scan(self, pack: Pack, split: WebSplit) -> None:
        from nokker.tools.skills import _clean, _guard_module

        guard = _guard_module()
        if guard is None:
            raise WebPackError(503, "no_scanner", "installation impossible ici : le scanner de sécurité des "
                                                  "compétences n'est pas disponible (rien n'a été installé)")
        refused = []
        for path in [*(p for _f, _n, _d, p in split.skills), pack.path / "pack.yaml",
                     *(t.path for t, _a, _e in split.templates)]:
            result = guard.scan_skill(path, source="community")
            allowed, _reason = guard.should_allow_install(result)
            if allowed is not True:
                refused.append(_clean(guard.format_scan_report(result)))
        if refused:
            raise WebPackError(422, "scan_refused", "installation REFUSÉE par le scanner (rien n'a été installé)",
                               {"report": refused})

    def _cards(self, account: str, templates: list[tuple[AgentTemplate, dict[str, Any], dict[str, Any]]],
               thread_id: str | None) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """(cartes posées, cartes à proposer plus tard). Sans fil : toutes à proposer (`POST …/propose`)."""
        proposed: list[dict[str, str]] = []
        later: list[dict[str, Any]] = []
        if not thread_id or self.hub is None:
            return proposed, [{"template": t.id, "agent_draft": a, "needs": e["needs"]} for t, a, e in templates]
        from nokker.bubbles.tools import agents_engine, propose_agent_draft

        ctx = ToolContext(thread_id=thread_id, turn_index=0, channel="api", sender=account)
        for tpl, args, extra in templates:
            try:
                res = propose_agent_draft(self.hub, ctx, args, extra, scheduled=agents_engine(self.hub))
                error = res.content if res.is_error else None
            except Exception as e:  # noqa: BLE001 — une carte non posée ne défait pas l'installation
                res, error = None, f"{type(e).__name__}: {e}"
            if error is not None or res is None:
                later.append({"template": tpl.id, "agent_draft": args, "needs": extra["needs"], "error": error})
                continue
            proposed.append({"template": tpl.id, "bubble_id": json.loads(res.content).get("bubble_id")})
        return proposed, later

    def install(self, account: str, name: str, bind: Any = None, thread_id: str | None = None,
                handoff: bool = False) -> dict[str, Any]:
        bind = _bad_bind(bind)
        pack = self._pack(name)
        tenant = self._tenant()
        self._member(thread_id, account)
        try:
            split = web_split(pack, bind)
        except PackError as e:
            raise WebPackError(400, "bad_bind", f"refusé : {e}") from None
        if not split.skills and not split.templates:
            raise WebPackError(409, "machine_only", f"rien du pack « {pack.name} » ne s'installe au web : tout "
                                                    "demande une machine",
                               {"machine": self._machine(account, tenant, pack, split, thread_id, handoff)})
        self._scan(pack, split)
        machine = self._machine(account, tenant, pack, split, thread_id, handoff) \
            if split.machine_skills or split.machine_agents else None
        from nokker.tools.skills import project_skills_sha256

        skills = []
        for family, sname, _d, path in split.skills:
            sha, _count, why = project_skills_sha256(path)
            if sha is None:
                raise WebPackError(409, "invalid_pack", f"compétence {sname} illisible : {why}")
            skills.append({"family": family, "name": sname, "sha256": sha})
        rec = PackInstall(account=account, pack=pack.name, version=pack.version, skills=skills,
                          templates=[t.id for t, _a, _e in split.templates],
                          machine=self._split_view(split)[1], bind=bind, proposed=[], installed_at=self.clock())
        self.store.put(tenant, rec)
        proposed, later = self._cards(account, split.templates, thread_id)
        if proposed:
            rec.proposed = proposed
            self.store.put(tenant, rec)
        self._emit(event="pack_installed", where="web", account=account, pack=pack.name, version=pack.version,
                   skills=[s["name"] for s in skills], proposed=[p["template"] for p in proposed],
                   partial=machine is not None)
        payload: dict[str, Any] = {
            "installed": pack.name, "version": pack.version, "skills": [s["name"] for s in skills],
            "proposed": proposed, "armed": [], "partial": machine is not None, "machine": machine,
            "note": "aucun agent n'est créé ni armé : chaque carte s'active par l'utilisateur"}
        if later:
            payload["to_propose"] = later
        if pack.connectors:
            payload["setup"] = [f"connecteurs du compte à poser : {', '.join(pack.connectors)}"]
        return payload

    def propose(self, account: str, name: str, thread_id: str, templates: list[str] | None = None) -> dict[str, Any]:
        """Repropose les cartes des modèles installés au web pas encore activés (dans `thread_id`, membre)."""
        tenant = self._tenant()
        rec = self.store.get(tenant, account, str(name or ""))
        if rec is None:
            raise WebPackError(404, "not_installed", f"pack non installé sur ce compte : {str(name)[:40]!r}")
        if not thread_id:
            raise WebPackError(400, "thread_required", "thread_id requis : les cartes se posent dans un fil")
        self._member(thread_id, account)
        pack = self._pack(rec.pack)
        created = {(a.origin or {}).get("template") for a in self._agents_of(account, rec.pack)}
        wanted = [t for t in web_split(pack, rec.bind).templates if t[0].id in rec.templates and t[0].id not in created
                  and (templates is None or t[0].id in templates)]
        proposed, later = self._cards(account, wanted, thread_id)
        if proposed:
            rec.proposed = [*rec.proposed, *proposed]
            self.store.put(tenant, rec)
        out: dict[str, Any] = {"pack": rec.pack, "proposed": proposed}
        if later:
            out["to_propose"] = later
        return out

    # ── machine : ce qui reste, relais X21 ──

    def _need(self, pack: Pack, split: WebSplit, machines: list[Any]) -> tuple[str, str] | None:
        roles: list[str] = []
        for a in split.machine_agents:
            roles += [n["name"] for n in a["needs"] if n["kind"] == "mcp" and n["name"] not in roles]
        roles += [r for r in pack.mcp if r not in roles]
        for role in roles:
            if any(role in ((m.resources or {}).get("mcp") or []) for m in machines):
                return "mcp", role
        if any(t.startswith("ssh_") for t in pack.tools):
            for m in machines:
                hosts = (m.resources or {}).get("hosts") or []
                if hosts:
                    return "host", str(hosts[0])
        return None

    def _machine(self, account: str, tenant: str, pack: Pack, split: WebSplit, thread_id: str | None,
                 handoff: bool) -> dict[str, Any]:
        from nokker.cloud.handoff import is_online, view

        now = self.clock()
        try:
            machines = list(self.links.machines_with_load(account, tenant)) if self.links is not None else []
        except Exception:  # noqa: BLE001 — machines illisibles : on le dit sans relais
            log.exception("machines du compte illisibles")
            machines = []
        linked = [{"machine_id": m.id, "name": m.name, "online": is_online(m, now)} for m in machines]
        need = self._need(pack, split, machines) if machines else None
        proposal = None
        what = ", ".join([*split.machine_skills, *(a["template"] for a in split.machine_agents)])
        if handoff and need is not None and thread_id and self.handoffs is not None:
            task = (f"Installe le pack d'agents « {pack.name} » sur cette machine : pack_install {{name: \"{pack.name}\"}}"
                    f", puis propose ses agents (cartes). Chez nokker.ai, ce compte l'a installé en partie ; ce qui "
                    f"demande la machine : {what}.")
            h, message = self.handoffs.propose(account, tenant, thread_id, need[0], need[1], task)
            proposal = view(h) if h is not None else None
            if h is None:
                log.info("relais non proposé : %s", message)
        if not machines:
            nxt = (f"installe Nokker Lokkal sur ta machine et relie-la à ce compte : pack_install {pack.name} y pose "
                   "le reste (serveurs MCP, bac à sable, hôtes, GPU)")
        elif proposal is not None:
            nxt = f"relais proposé vers « {proposal['machine_name']} » : accepte-le pour installer le reste là-bas"
        elif need is not None:
            nxt = (f"relais possible vers une machine liée qui déclare le {need[0]} « {need[1]} » : lokkal_handoff "
                   f"{{need: {{kind: \"{need[0]}\", name: \"{need[1]}\"}}, task: \"pack_install {pack.name}\"}}, ou "
                   "la route d'installation avec handoff: true")
        else:
            nxt = (f"aucune machine liée ne déclare ce que demande le reste du pack : pas de relais possible ; ouvre "
                   f"Lokkal sur ta machine et lance pack_install {pack.name}")
        return {"skills": list(split.machine_skills), "agents": [dict(a) for a in split.machine_agents],
                "linked": linked, "handoff": proposal, "next": nxt}

    # ── retrait ──

    def remove(self, account: str, name: str, thread_id: str | None = None) -> dict[str, Any]:
        """Idempotent : la ligne du compte, ses cartes non activées ; les agents créés → carte (jamais en silence)."""
        name = str(name or "")
        if not _NAME.match(name):
            raise WebPackError(404, "unknown_pack", f"pack inconnu : {name[:40]!r}")
        tenant = self._tenant()
        rec = self.store.get(tenant, account, name)
        if rec is None and not (Path(self.packs_root) / name / "pack.yaml").is_file():
            raise WebPackError(404, "unknown_pack", f"pack inconnu : {name!r}")
        self._member(thread_id, account)
        if rec is not None:
            self.store.delete(tenant, account, name)
        follow: dict[str, Any] = {"drafts_closed": [], "agents": [], "card": None}
        if self.hub is not None:
            from nokker.bubbles.tools import pack_removed

            ctx = ToolContext(thread_id=thread_id or "", turn_index=0, channel="api", sender=account)
            follow = pack_removed(self.hub, ctx, name, [p.get("bubble_id") for p in (rec.proposed if rec else [])
                                                       if p.get("bubble_id")], owner=account)
        self._emit(event="pack_removed", where="web", account=account, pack=name, already=rec is None)
        out: dict[str, Any] = {"removed": name, "skills": [s["name"] for s in rec.skills] if rec else [],
                               "already_removed": rec is None, **follow}
        if out["agents"]:
            out["note"] = ("les agents créés depuis ce pack sont gardés tels quels : la carte (ou /v1/agents) "
                           "propose de les désarmer ou de les supprimer, l'utilisateur choisit")
        return out

    # ── compétences du compte (outils skill_list / skill_view) ──

    def _entries(self, account: str) -> tuple[list[dict[str, Any]], int]:
        """(compétences visibles, nombre masquées) : contenu livré relu, empreinte comparée à celle de la garde."""
        from nokker.tools.skills import parse_frontmatter, project_skills_sha256

        out, hidden = [], 0
        for rec in self.store.list(self._tenant(), account):
            for s in rec.skills:
                path = Path(self.packs_root) / rec.pack / "skills" / s["family"] / s["name"]
                ok = path.is_dir() and not path.is_symlink() and project_skills_sha256(path)[0] == s["sha256"]
                if not ok:
                    hidden += 1
                    continue
                fm, _ = parse_frontmatter((path / "SKILL.md").read_text(encoding="utf-8"))
                out.append({"name": s["name"], "category": s["family"],
                            "description": " ".join(str(fm.get("description") or "").split()),
                            "source": f"pack:{rec.pack}", "_path": path, "_sha": s["sha256"]})
        return out, hidden

    def list_skills(self, account: str, category: str | None = None) -> dict[str, Any]:
        entries, hidden = self._entries(account)
        skills = [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries
                  if not category or e["category"] == category]
        out: dict[str, Any] = {"skills": sorted(skills, key=lambda e: (e["category"], e["name"]))}
        if hidden:
            out["note"] = (f"{hidden} compétence(s) masquée(s) : leur contenu a changé depuis l'installation ; "
                           "réinstalle le pack (pack_install) pour une nouvelle vérification")
        return out

    def view_skill(self, account: str, name: str, file: str | None = None) -> tuple[Any, bool]:
        from nokker.tools.skills import SkillToolSource, _Entry, parse_frontmatter, project_skills_sha256

        name = str(name or "").strip()
        entries, _hidden = self._entries(account)
        e = next((x for x in entries if name in (x["name"], f"{x['category']}/{x['name']}")), None)
        if e is None:
            known = any(name in (s["name"], f"{s['family']}/{s['name']}") for r in
                        self.store.list(self._tenant(), account) for s in r.skills)
            if known:
                return (f"REFUSÉ : le contenu de la compétence {name!r} a changé depuis l'installation ; réinstalle "
                        "le pack (pack_install) pour une nouvelle vérification"), True
            return f"compétence introuvable : {name!r} (skill_list pour voir les noms)", True
        entry = _Entry(name=e["name"], category=e["category"], description=e["description"], source=e["source"],
                       path=e["_path"], trusted=True, root=e["_path"].parent.parent)
        if file:
            payload, err = SkillToolSource._read_linked(entry, str(file))
        else:
            _, body = parse_frontmatter((entry.path / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace"))
            payload, err = {"name": entry.name, "category": entry.category, "source": entry.source,
                            "description": entry.description, "content": body.lstrip("\n"),
                            "linked_files": SkillToolSource._linked_files(entry)}, False
        if not err and project_skills_sha256(entry.path)[0] != e["_sha"]:  # changé pendant la lecture
            return "REFUSÉ : contenu changé pendant la lecture ; réinstalle le pack", True
        return payload, err


__all__ = ["WEB_TOOLS", "MemoryPackStore", "PackInstall", "WebPackError", "WebPacks", "WebSplit", "template_needs",
           "web_split"]
