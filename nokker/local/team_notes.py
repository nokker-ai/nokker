"""Notes de projet d'équipe poussées ET tirées par la machine (lot X17 + miroir, docs/LOKKAL.md §7.15) : le pendant
machine de `GET|POST /v1/mirror/projects/{id}/notes`, appelé par `MirrorWorker` (drapeau `local.mirror`) quand
l'équipe est allumée sur la machine (`local.team`).

Ce qui synchronise une paire (projet d'équipe `pj_…`, projet mémoire local `p-…` d'un de ses dépôts) :
  - le projet est en miroir chez nokker.ai (choix du propriétaire ET compte abonné : `mirror` de la liste des projets) ;
  - ET l'utilisateur a mis ce projet mémoire en miroir sur SA machine (`PUT /v1/local/mirror/projects/{p} {mirror:
    true}`) : règle d'or, une note `local` ne quitte jamais la machine, `both` seulement si l'utilisateur l'a choisi ;
  - le filtre `nokker/core/locality.py::outbound` est appliqué JUSTE avant l'envoi, même si la mémoire se trompe ;
  - seules les notes apprises de portée projet (`rule`, `feedback`, `fact`) voyagent, jamais les sections des fichiers.

État persistant (`team_notes` dans l'état local, une entrée par paire `pj_…|p-…`) :
  - `cursor` : le `seq` de nokker.ai jusqu'où la paire a tiré (curseur PAR PROJET, avancé seulement après écriture
    locale de la page reçue : une coupure reprend au même point, une note vue deux fois est sautée par son empreinte) ;
  - `notes[nom]` = `{base, local, kind, deleted}` : `base` = empreinte de la dernière version du projet connue ici (la
    `base_sha` envoyée), `local` = empreinte de la note locale à ce moment-là. Une note dont l'empreinte locale a changé
    est « modifiée ici » : elle part, et un tirage ne l'écrase jamais (nokker.ai tranche le conflit, l'autre version
    est gardée en quarantaine). Une note présente dans l'état mais plus sur le disque a été oubliée ici : sa pierre
    tombale part. C'est cet état qui sert de file d'envoi persistante : après un redémarrage, tout ce qui diffère
    repart, rien n'est perdu, rien n'est envoyé deux fois avec effet (même empreinte → `skipped`).

Droits : la paire ne pousse que si nokker.ai dit `can_push_notes` (membre au moins) ; un viewer tire seulement, et si
la liste locale mentait, nokker.ai refuse (403) — l'erreur est gardée par projet (`status()["errors"]`).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.core import locality
from nokker.team.model import MAX_NOTES_PER_PUSH, NOTE_KINDS, TeamError, clean_note, note_sha

logger = logging.getLogger("nokker.local.team_notes")
STATE_KEY = "team_notes"
MAX_PAGES = 50  # pages de tirage par paire et par passage (200 notes par page côté nokker.ai)
PULL_LIMIT = 200


def _key(project_id: str, memory_project: str) -> str:
    return f"{project_id}|{memory_project}"


@dataclass
class TeamNotesSync:
    base_url: str
    token: Callable[[], str]
    state: dict[str, Any]
    save: Callable[[], None]
    memory: Any  # nokker.context.project_memory.ProjectMemory
    team: Any  # nokker.local.team.TeamRules (liste des projets, `mirror`, `can_push_notes`, dépôts → `memory_project`)
    client: Any = None
    clock: Callable[[], float] = field(default=time.time)
    errors: dict[str, dict[str, Any]] = field(default_factory=dict)  # projet → dernière erreur de nokker.ai
    # note -> True : elle reste sur la machine (dérivée d'un tour gardé ici, boîte mail locale : MirrorWorker.note_stays,
    # docs/LOKKAL.md §7.25) ; None = aucun tri en plus de la localité.
    stays: Callable[[Any], bool] | None = None

    # ── ce qui synchronise ──

    def pairs(self) -> list[tuple[dict[str, Any], str]]:
        """(projet d'équipe, projet mémoire local) à synchroniser maintenant."""
        projects = self.team.projects()
        if not projects and getattr(self.team, "last_error", None) is not None:
            raise self.team.last_error  # hors ligne sans liste connue : le passage échoue (backoff du miroir)
        out = []
        for p in projects:
            if not p.get("mirror") or not str(p.get("id") or "").startswith("pj_"):
                continue
            for r in p.get("repos") or []:
                mp = str((r or {}).get("memory_project") or "") if isinstance(r, dict) else ""
                if mp and self.memory.is_mirrored(mp):
                    out.append((p, mp))
        return out

    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=30.0)

    def _url(self, pid: str) -> str:
        return f"{self.base_url.rstrip('/')}/v1/mirror/projects/{pid}/notes"

    def _entry(self, pid: str, mp: str) -> dict[str, Any]:
        root = self.state.setdefault(STATE_KEY, {})
        return root.setdefault(_key(pid, mp), {"cursor": "0", "notes": {}})

    def _local(self, mp: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(toutes les notes apprises de portée projet par nom, celles qui peuvent sortir par nom)."""
        raw = {n.name: n for n in self.memory.learned(mp) if n.scope == "project" and n.kind in NOTE_KINDS}
        kept = [n for n in raw.values() if self.stays is None or not self.stays(n)]
        leaving = {n.name: n for n in locality.outbound(kept, locality.of)}  # filtre à l'envoi
        return raw, leaving

    @staticmethod
    def _fp(n: Any) -> str:
        return note_sha(n.kind, n.title, n.text, False)

    # ── un passage ──

    def sync_once(self) -> dict[str, int]:
        """Pousse puis tire chaque paire. Lève `httpx.HTTPError` si nokker.ai ne répond pas (le miroir espace alors les
        essais) ; un refus de nokker.ai pour un projet (403, 409, 404) est gardé pour ce projet et n'arrête pas les
        autres."""
        out = {"pushed": 0, "pulled": 0, "forgotten": 0, "kept_local": 0}
        for p, mp in self.pairs():
            if p.get("can_push_notes"):
                out["pushed"] += self._push(p, mp)
            got = self._pull(p, mp)
            for k in ("pulled", "forgotten", "kept_local"):
                out[k] += got[k]
        return out

    def _pull_only(self) -> dict[str, int]:
        out = {"pulled": 0, "forgotten": 0, "kept_local": 0}
        for p, mp in self.pairs():
            got = self._pull(p, mp)
            for k in out:
                out[k] += got[k]
        return out

    def _refused(self, pid: str, r: Any, op: str) -> bool:
        """Réponse de refus de nokker.ai pour ce projet : gardée (par projet, `op` = push | pull), rien n'avance.
        5xx et 429 : lève (essai plus tard, avec l'attente du miroir)."""
        if r.status_code < 400:
            if (self.errors.get(pid) or {}).get("op") == op:
                self.errors.pop(pid, None)
            return False
        if r.status_code >= 500 or r.status_code == 429:
            r.raise_for_status()
        try:
            err = (r.json().get("error") or {}) if r.content else {}
        except ValueError:
            err = {}
        self.errors[pid] = {"op": op, "status": r.status_code, "code": str(err.get("code") or r.status_code),
                            "message": str(err.get("message") or "")[:300], "at": self.clock()}
        logger.info("notes d'équipe : %s refusé par nokker.ai (%s)", pid, self.errors[pid]["code"])
        return True

    def _push(self, p: dict[str, Any], mp: str) -> int:
        pid = str(p["id"])
        st = self._entry(pid, mp)
        if p.get("name"):
            st["name"] = str(p["name"])[:200]  # libellé pour l'écran Miroir (le web n'a que l'id pj_…)
        known: dict[str, Any] = st["notes"]
        raw, leaving = self._local(mp)
        items: list[tuple[str, dict[str, Any], dict[str, Any]]] = []  # (nom, corps envoyé, entrée après succès)
        for name, n in leaving.items():
            fp, e = self._fp(n), known.get(name)
            if e is not None and e.get("local") == fp:
                continue  # inchangée depuis le dernier envoi ou tirage
            body = {"name": name, "kind": n.kind, "scope": "project", "locality": n.locality, "title": n.title,
                    "text": n.text, "base_sha": (e or {}).get("base") or "", "origin": (n.origin or "")[:200],
                    "derived_from": (n.derived_from or "")[:20], "updated_at": self.clock(),
                    "links": [x for x in (n.links or []) if not x.startswith("src-")][:20]}
            try:
                clean_note(body)  # une note illisible refuserait tout l'envoi : elle reste ici, signalée
            except TeamError as err:
                self.errors[f"{pid}/{name}"] = {"status": 400, "code": err.code, "message": err.message,
                                                "at": self.clock()}
                continue
            items.append((name, body, {"base": fp, "local": fp, "kind": n.kind, "deleted": False}))
        for name, e in known.items():
            # Oubliée ici : sur le disque, plus du tout (pas « devenue local » : un projet retiré du miroir ne fait
            # rien oublier à l'équipe). Une pierre tombale n'a ni titre ni texte.
            if name in raw or e.get("deleted"):
                continue
            kind = str(e.get("kind") or "fact")
            items.append((name, {"name": name, "kind": kind, "scope": "project", "locality": locality.BOTH,
                                 "deleted": True, "base_sha": e.get("base") or "", "updated_at": self.clock()},
                          {"base": note_sha(kind, "", "", True), "local": None, "kind": kind, "deleted": True}))
        sent = 0
        for i in range(0, len(items), MAX_NOTES_PER_PUSH):
            chunk = items[i:i + MAX_NOTES_PER_PUSH]
            r = self._http().post(self._url(pid), headers={"Authorization": f"Bearer {self.token()}"},
                                  json={"notes": [b for _, b, _ in chunk]})
            if self._refused(pid, r, "push"):
                break
            for name, _, after in chunk:
                known[name] = after  # l'empreinte de nokker.ai est la même (caviardage : corrigé au tirage)
            sent += len(chunk)
            self.save()
        return sent

    def _pull(self, p: dict[str, Any], mp: str) -> dict[str, int]:
        pid = str(p["id"])
        st = self._entry(pid, mp)
        if p.get("name"):
            st["name"] = str(p["name"])[:200]  # libellé pour l'écran Miroir (le web n'a que l'id pj_…)
        known: dict[str, Any] = st["notes"]
        out = {"pulled": 0, "forgotten": 0, "kept_local": 0}
        for _ in range(MAX_PAGES):
            r = self._http().get(self._url(pid), headers={"Authorization": f"Bearer {self.token()}"},
                                 params={"since": st["cursor"]})
            if self._refused(pid, r, "pull"):
                return out
            data = r.json()
            notes = [n for n in (data.get("notes") or []) if isinstance(n, dict)]
            raw, _ = self._local(mp)
            for rn in notes:
                name, sha = str(rn.get("name") or ""), str(rn.get("sha") or "")
                e, here = known.get(name), raw.get(name)
                if e is not None and e.get("base") == sha:
                    continue  # déjà cette version (dont nos propres envois)
                here_fp = self._fp(here) if here is not None else None
                if e is None and here is not None and here_fp == sha:
                    known[name] = {"base": sha, "local": here_fp, "kind": rn.get("kind"), "deleted": False}
                    continue
                clean = (e is None and here is None) or (e is not None and here_fp == e.get("local"))
                if not clean:
                    out["kept_local"] += 1  # modifiée ici : elle partira, nokker.ai tranchera (rien d'écrasé)
                    continue
                if rn.get("deleted"):
                    if self.memory.forget_synced(mp, name):
                        out["forgotten"] += 1
                    known[name] = {"base": sha, "local": None, "kind": rn.get("kind"), "deleted": True}
                    continue
                got = self.memory.put_synced(mp, {**rn, "team_project": pid,
                                                  "created": _iso(rn.get("updated_at"))})
                if got is None:
                    continue
                known[name] = {"base": sha, "local": self._fp(got), "kind": got.kind, "deleted": False}
                out["pulled"] += 1
            st["cursor"] = str(data.get("cursor") or st["cursor"])
            self.save()
            if len(notes) < PULL_LIMIT:
                break
        return out

    # ── robustesse ──

    def reset_cursors(self) -> None:
        """Resynchronisation complète : chaque paire retire depuis le début. Les empreintes sont gardées : un tirage
        complet n'écrase pas une modification locale pas encore partie, et ne renvoie rien qui n'ait changé."""
        for st in (self.state.get(STATE_KEY) or {}).values():
            st["cursor"] = "0"
        self.save()

    def pending(self) -> int:
        """Notes à envoyer (modifiées ou oubliées ici), toutes paires confondues ; sans réseau."""
        n = 0
        try:
            projects = self.state.get("team_projects", {}).get("projects") or []
        except AttributeError:
            return 0
        for p in projects:
            if not p.get("mirror") or not p.get("can_push_notes"):
                continue
            for r in p.get("repos") or []:
                mp = str((r or {}).get("memory_project") or "") if isinstance(r, dict) else ""
                if not mp or not self.memory.is_mirrored(mp):
                    continue
                known = ((self.state.get(STATE_KEY) or {}).get(_key(str(p.get("id")), mp)) or {}).get("notes") or {}
                raw, leaving = self._local(mp)
                n += sum(1 for name, x in leaving.items() if (known.get(name) or {}).get("local") != self._fp(x))
                n += sum(1 for name, e in known.items() if name not in raw and not e.get("deleted"))
        return n

    def status(self) -> dict[str, Any]:
        return {"projects": {k: {"cursor": v.get("cursor"), "notes": len(v.get("notes") or {}), "name": v.get("name")}
                             for k, v in (self.state.get(STATE_KEY) or {}).items()},
                "errors": dict(self.errors)}


def _iso(ts: Any) -> str:
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


__all__ = ["STATE_KEY", "TeamNotesSync"]
