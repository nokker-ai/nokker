"""Mémoire en miroir web ⇄ Lokkal, côté machine (docs/LOKKAL.md §6.3, §7.23, L5 ; abonnés, drapeau `local.mirror`,
ÉTEINT dans l'install : décision produit). Le pendant de `nokker/cloud/mirror.py`.

`MirrorWorker` (un fil de fond) fait passer cinq canaux, chacun avec son état et son attente après échec :
- **tours, poussée** (`turns_push`) : relit le magasin du noyau après un curseur DURABLE (`mirror_push_cursor` dans
  l'état local, `setup.json`) et envoie les tours des fils de la machine à `POST /v1/mirror/turns` (jeton de machine).
  Le curseur n'avance qu'après un envoi accepté : un arrêt brutal ne perd rien, l'envoi reprend au même point (le web
  saute ce qu'il a déjà). Les copies venues du web (`mirror-web-*`) ne partent jamais (pas d'aller-retour) ;
- **tours, tirage** (`turns_pull`) : `GET /v1/mirror/turns?since=` range les tours des sessions web sous
  `mirror-web-<fil>` (lecture seule), curseur `mirror_cursor` ;
- **notes de mémoire** (`notes`, lot X21, §6.7) : `POST /v1/mirror/notes` des notes dont la localité permet la sortie
  (`both`), jamais une note `local` : le filtre `nokker/core/locality.py::outbound` est appliqué JUSTE avant l'envoi.
  Une note oubliée, ou un projet retiré du miroir, part en `forget`. Empreinte par note (`mirror_notes_sent`) ;
- **notes de projet d'équipe** (`team_notes`, X17) : poussées ET tirées par projet, `nokker/local/team_notes.py` ;
- **pages** (`pages`, §7.23) : les pages (docs/BUBBLES.md §10) dont l'utilisateur a demandé la copie
  (`PUT /v1/local/mirror/pages/{id} {mirror: true}`, état `mirror_pages`) partent à `PUT /v1/mirror/pages/{id}` ;
  nokker.ai les sert quand la machine est éteinte. Une page non choisie est `local` et ne part jamais (filtre à
  l'envoi) ; une copie retirée, ou une page supprimée ici, est purgée chez nokker.ai (`DELETE`).

La file d'envoi est l'état persistant lui-même (curseurs durables, empreintes envoyées, choix de copie) : après une
coupure ou un redémarrage, tout ce qui diffère repart, rien n'est perdu, et un renvoi est sauté par nokker.ai (même
indice, même empreinte). Après un échec, un canal attend 5 s, 10 s, 20 s… jusqu'à 10 min avant le prochain essai
(`BACKOFF_*`) ; `status()` (route `GET /v1/local/mirror`) dit, par canal, le dernier envoi réussi, les échecs
d'affilée, la dernière erreur et le prochain essai, plus ce qui attend (`pending`) et le retard (`behind_s`).
**Resynchronisation complète** (`POST /v1/local/mirror/resync`) : curseurs remis à zéro, liste de ce que nokker.ai
garde (notes, pages) relue pour renvoyer ce qui manque et purger ce que la machine n'a plus ; les empreintes des notes
d'équipe sont gardées (un tirage complet n'écrase pas une modification locale pas encore partie).

Le fil de fond n'a pas de requête autour de lui : il pose lui-même le locataire du noyau local (`tenant`), sinon un
magasin Postgres refuse tout (NoTenant). Rien du dossier de travail ne part : seulement les tours, les notes et les
pages choisies.

Effacement propagé (§6.3) :
- le propriétaire efface les copies de sa machine chez nokker.ai (`erase_remote`, route locale
  `POST /v1/local/mirror/erase`) : l'envoi passe en pause (`mirror_paused`) jusqu'à `resume`, qui renvoie tout ;
- compte effacé ou machine déliée chez nokker.ai : le tirage reçoit 401. Après `UNLINKED_AFTER` refus d'affilée, les
  copies des fils web gardées ici sont effacées (fonction `nokker_mirror_erase`, copies seulement) et le curseur de
  tirage repart de zéro.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.core import locality
from nokker.store.pg import turn_from_dict
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

logger = logging.getLogger("nokker.local.mirror")
WEB_PREFIX = "mirror-web-"
BATCH = 200
UNLINKED_AFTER = 3  # 401 d'affilée au tirage (≈ 90 s) : un refus isolé n'efface rien
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 600.0
PAGES_PER_PASS = 10  # copies de pages envoyées par passage (5 Mio chacune au plus)
CHANNELS = ("turns_push", "turns_pull", "notes", "team_notes", "pages", "resync")


def public_field(meta: Any) -> dict[str, Any] | None:
    """La publication que la copie de CETTE version porte chez nokker.ai (docs/LOKKAL.md §7.26) : l'empreinte de l'id
    public (jamais l'id), l'indexation et la version. None si la page n'est pas publique, si la version publiée n'est
    pas celle qu'on copie (la copie ne sert alors pas en public), ou si la page est épinglée `local`."""
    if not (getattr(meta, "public", False) and getattr(meta, "public_id", None)
            and getattr(meta, "public_version", None) == meta.version and getattr(meta, "locality", None) != "local"):
        return None
    return {"id_sha256": hashlib.sha256(meta.public_id.encode()).hexdigest(), "index": bool(meta.public_index),
            "version": meta.version}


def page_mark(meta: Any) -> str:
    """Ce qui a été copié : le contenu, plus la publication si elle part avec (un changement de publication recopie)."""
    pub = public_field(meta)
    return meta.sha256 if pub is None else f"{meta.sha256}|pub:{pub['id_sha256'][:16]}:{int(pub['index'])}"


class _Refused(Exception):
    """Refus de nokker.ai qui touche tout le canal (abonnement, machine) : compté comme un échec, sans détail réseau."""


def _cursor(raw: str | None) -> tuple[str, str, int]:
    if not raw:
        return ("-infinity", "", -1)  # avant tout horodatage (Postgres) et tout rang (mémoire)
    created, tid, idx = raw.split("|", 2)
    return (created, tid, int(idx))


def backoff_s(failures: int) -> float:
    """Attente avant l'essai suivant après `failures` échecs d'affilée (5 s, 10 s, 20 s… 10 min au plus)."""
    return 0.0 if failures <= 0 else min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (failures - 1)))


@dataclass
class MirrorWorker:
    store: Any  # Store du noyau local
    base_url: str  # https://app.nokker.ai
    token: Any  # () -> jeton de machine
    state: dict[str, Any]  # setup.state (curseurs « mirror_push_cursor », « mirror_cursor », choix, empreintes)
    save: Any  # () -> None (persiste l'état)
    tenant: str = INTERNAL_TENANT  # locataire du noyau local (le propriétaire)
    push_s: float = 5.0
    pull_s: float = 30.0
    client: Any = None
    notes: Any = None  # nokker.context.project_memory.ProjectMemory (X1) ; None = pas de notes en miroir
    pages: Any = None  # nokker.pages.store.PageStore ; None = pas de pages en miroir
    team_notes: Any = None  # nokker.local.team_notes.TeamNotesSync (X17) ; None = équipe éteinte sur la machine
    # (fil, tour sérialisé) -> True : le tour reste sur la machine (boîte mail locale, passage `mail:`,
    # nokker/local/connectors.py::keep_local_fn, docs/LOKKAL.md §7.25) ; le curseur le saute comme une copie web, et
    # une note qui en dérive (`origin` = fil#tour) ne part pas non plus (`note_stays`). Dans le doute : reste ici.
    keep_local: Any = None
    clock: Callable[[], float] = field(default=time.time)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _wake: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _refused: int = field(default=0, init=False, repr=False)
    _retry_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _ticks: int = field(default=0, init=False, repr=False)
    _wake_all: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        threading.Thread(target=self._run, name="nokker-mirror", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Passage tout de suite (choix de copie, page supprimée, resynchronisation demandée)."""
        self._wake.set()

    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    # ── état des canaux ──

    def _ch(self, name: str) -> dict[str, Any]:
        return self.state.setdefault("mirror_status", {}).setdefault(name, {"failures": 0})

    def _ok(self, name: str) -> None:
        ch = self._ch(name)
        changed = ch.get("failures") or ch.get("last_error")
        ch.update(failures=0, last_ok_at=self.clock())
        ch.pop("last_error", None)
        self._retry_at.pop(name, None)
        if changed:
            self.save()

    def _fail(self, name: str, e: BaseException) -> None:
        ch = self._ch(name)
        ch["failures"] = int(ch.get("failures") or 0) + 1
        kind = type(e).__name__
        msg = str(e)[:300] if isinstance(e, _Refused) else kind  # jamais une URL ni un jeton dans l'état
        ch["last_error"] = {"at": self.clock(), "kind": kind, "message": msg}
        self._retry_at[name] = self.clock() + backoff_s(ch["failures"])
        self.save()
        logger.info("miroir : %s reporté (%s), essai suivant dans %.0f s", name, kind, backoff_s(ch["failures"]))

    def _due(self, name: str) -> bool:
        return self.clock() >= self._retry_at.get(name, 0.0)

    # ── tours ──

    def push_once(self) -> int:
        """Envoie au plus `BATCH` tours après le curseur durable ; rend le nombre de tours envoyés."""
        if self.state.get("mirror_paused"):
            return 0
        batch, last = [], None
        with tenant_scope(self.tenant):
            rows = self.store.turns_after(_cursor(self.state.get("mirror_push_cursor")), BATCH * 2)
            for created, tid, idx, d in rows:
                last = f"{created}|{tid}|{idx}"  # avance aussi sur les copies web et les tours restés ici
                if tid.startswith(WEB_PREFIX) or self._stays(tid, d):
                    continue
                batch.append({"thread_id": tid, "turn": d})
                if len(batch) >= BATCH:
                    break
        if last is None:
            return 0
        if batch:
            try:
                r = self._http().post(self._url("/v1/mirror/turns"), headers=self._headers(), json={"turns": batch})
                r.raise_for_status()
            except httpx.HTTPError as e:
                self._fail("turns_push", e)
                return 0  # curseur inchangé : le même lot repart au prochain passage
        self.state["mirror_push_cursor"] = last
        self._ok("turns_push")
        self.save()
        return len(batch)

    def _stays(self, thread_id: str, turn: dict[str, Any]) -> bool:
        """Le tour reste-t-il sur la machine (`keep_local`) ? Appelé dans le locataire du noyau local."""
        if self.keep_local is None:
            return False
        try:
            return bool(self.keep_local(thread_id, turn))
        except Exception:  # noqa: BLE001 — dans le doute, le tour reste sur la machine
            logger.exception("miroir : tri local d'un tour en échec, tour gardé ici")
            return True

    def note_stays(self, note: Any) -> bool:
        """Une note dérivée d'un tour gardé sur la machine (`origin` = « fil#tour ») ne part pas : ni par le canal
        `notes`, ni par les notes d'équipe (`TeamNotesSync.stays`). Tour introuvable : seul le fil compte (un passage
        `mail:`) ; erreur de lecture : la note reste ici."""
        if self.keep_local is None:
            return False
        origin = str(getattr(note, "origin", "") or "")
        thread_id, sep, index = origin.rpartition("#")
        if not sep or not thread_id or not index.isdigit():
            return False  # pas dérivée d'un tour (dépôt, équipe, import)
        try:
            from nokker.store.pg import turn_to_dict

            with tenant_scope(self.tenant):
                found = next((t for t in self.store.history_from(thread_id, int(index)) if t.index == int(index)),
                             None)
                return bool(self.keep_local(thread_id, turn_to_dict(found) if found is not None else {}))
        except Exception:  # noqa: BLE001 — dans le doute, la note reste sur la machine
            logger.exception("miroir : origine d'une note illisible, note gardée ici")
            return True

    def pull_once(self) -> int:
        params = {"since": self.state["mirror_cursor"]} if self.state.get("mirror_cursor") else {}
        try:
            r = self._http().get(self._url("/v1/mirror/turns"), headers=self._headers(), params=params)
            if r.status_code == 401:
                self._refused += 1
                self._fail("turns_pull", _Refused("machine refusée par nokker.ai (401)"))
                if self._refused >= UNLINKED_AFTER:
                    self.purge_web_copies()
                return 0
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            self._fail("turns_pull", e)
            return 0
        self._refused = 0
        n = 0
        with tenant_scope(self.tenant):
            for it in data.get("turns") or []:
                target = WEB_PREFIX + str(it["thread_id"])
                d = dict(it["turn"])
                if self.store.next_index(target) > int(d["index"]):
                    continue
                d["thread_id"] = target
                self.store.append(dataclasses.replace(turn_from_dict(d), thread_id=target))
                n += 1
        if data.get("cursor"):
            self.state["mirror_cursor"] = data["cursor"]
        self._ok("turns_pull")
        self.save()
        return n

    def purge_web_copies(self) -> int:
        """Efface les copies des fils web gardées sur la machine (compte effacé ou machine déliée chez nokker.ai)."""
        with tenant_scope(self.tenant):
            n = self.store.erase_mirror_copies(WEB_PREFIX)
        self.state.pop("mirror_cursor", None)
        self.save()
        self._refused = 0
        logger.info("miroir : %d tour(s) des copies web effacés (machine refusée par nokker.ai)", n)
        return n

    # ── notes de mémoire (X21) ──

    def _note_candidates(self) -> dict[str, tuple[str, dict[str, Any]]]:
        current: dict[str, tuple[str, dict[str, Any]]] = {}
        leaving = [n for n in self.notes.notes_for_mirror() if not self.note_stays(n)]  # tours gardés ici (§7.25)
        for n in locality.outbound(leaving, locality.of):  # filtre à l'envoi
            d = {"project": n.project, "name": n.name, "kind": n.kind, "scope": n.scope, "locality": n.locality,
                 "title": n.title, "text": n.text, "created": n.created, "derived_from": n.derived_from}
            sha = hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            current[f"{n.project}/{n.name}"] = (sha, d)
        return current

    def push_notes_once(self) -> int:
        """Envoie les notes nouvelles ou changées dont la localité permet la sortie, et l'oubli de celles qui ne sont
        plus candidates ; rend le nombre de notes envoyées (oublis compris). Rien n'est noté envoyé sans 200."""
        if self.notes is None or self.state.get("mirror_paused"):
            return 0
        current = self._note_candidates()
        sent: dict[str, str] = dict(self.state.get("mirror_notes_sent") or {})
        notes = [d for key, (sha, d) in current.items() if sent.get(key) != sha][:BATCH]
        forget = [{"project": k.split("/", 1)[0], "name": k.split("/", 1)[1]} for k in sent if k not in current][:BATCH]
        if not notes and not forget:
            return 0
        try:
            r = self._http().post(self._url("/v1/mirror/notes"), headers=self._headers(),
                                  json={"notes": notes, "forget": forget})
            r.raise_for_status()
        except httpx.HTTPError as e:
            self._fail("notes", e)
            return 0
        for d in notes:
            key = f"{d['project']}/{d['name']}"
            sent[key] = current[key][0]
        for f in forget:
            sent.pop(f"{f['project']}/{f['name']}", None)
        self.state["mirror_notes_sent"] = sent
        self._ok("notes")
        self.save()
        return len(notes) + len(forget)

    # ── notes de projet d'équipe (X17) ──

    def sync_team_notes_once(self) -> int:
        if self.team_notes is None or self.state.get("mirror_paused"):
            return 0
        try:
            got = self.team_notes.sync_once()
        except httpx.HTTPError as e:
            self._fail("team_notes", e)
            return 0
        self._ok("team_notes")
        return sum(got.values())

    # ── pages (§7.23) ──

    def page_locality(self, page_id: str) -> str:
        """Une page vit sur la machine (`local`) tant que l'utilisateur n'en a pas demandé la copie (`both`) ; une page
        qu'il a épinglée `local` y reste quoi qu'il ait choisi avant."""
        meta = self.pages.get(page_id) if self.pages is not None else None
        if meta is not None and getattr(meta, "locality", None) == locality.LOCAL:
            return locality.LOCAL
        return locality.BOTH if (self.state.get("mirror_pages") or {}).get(page_id) is True else locality.LOCAL

    def set_page_mirror(self, page_id: str, on: bool) -> bool:
        """Choix de l'utilisateur pour UNE page ; False si la page n'existe pas ici. Le retrait purge la copie au
        passage suivant (tout de suite : le fil est réveillé). ValueError : copier une page épinglée `local`."""
        meta = self.pages.get(page_id) if self.pages is not None else None
        if meta is None:
            return False
        if on and getattr(meta, "locality", None) == locality.LOCAL:
            raise ValueError("page épinglée `local` : elle ne quitte pas la machine")
        chosen = dict(self.state.get("mirror_pages") or {})
        if on:
            chosen[page_id] = True
        else:
            chosen.pop(page_id, None)
        self.state["mirror_pages"] = chosen
        self.save()
        self.wake()
        return True

    def pages_view(self) -> dict[str, Any]:
        """Écran « pages copiées » : chaque page de la machine, son choix et l'état de sa copie (`off` | `pending` |
        `sent` | `quota` | `refused`)."""
        chosen = self.state.get("mirror_pages") or {}
        sent = self.state.get("mirror_pages_sent") or {}
        refused = self.state.get("mirror_pages_refused") or {}
        out = []
        for meta in (self.pages.all() if self.pages is not None else []):
            st = "off"
            if chosen.get(meta.page_id) is True:
                ref = refused.get(meta.page_id) or {}
                st = "sent" if sent.get(meta.page_id) == page_mark(meta) else (
                    ("quota" if ref.get("code") == "mirror_quota" else "refused")
                    if ref.get("sha") == meta.sha256 else "pending")
            out.append({"page_id": meta.page_id, "title": meta.title, "version": meta.version, "size": meta.size,
                        "mirror": chosen.get(meta.page_id) is True, "state": st,
                        "error": (refused.get(meta.page_id) or {}).get("message") if st in ("quota", "refused")
                        else None})
        return {"pages": out, "space": self.state.get("mirror_pages_space")}

    def _page_candidates(self) -> dict[str, Any]:
        if self.pages is None:
            return {}
        items = []
        for pid, on in (self.state.get("mirror_pages") or {}).items():
            meta = self.pages.get(pid) if on is True else None
            if meta is not None:
                items.append({"page_id": pid, "locality": self.page_locality(pid), "meta": meta})
        return {it["page_id"]: it["meta"] for it in locality.outbound(items, locality.of)}  # filtre à l'envoi

    # ── publication (§7.26) : retirer tout de suite la copie publique ──

    def purge_public(self, page_id: str) -> str:
        """Dépublier : la copie PUBLIQUE de la page part tout de suite chez nokker.ai (`DELETE …/public`, un droit), même
        si la copie privée reste. `done` ; `pending` si nokker.ai ne répond pas (refait en tête du prochain passage des
        pages, avant toute autre copie)."""
        try:
            r = self._http().delete(self._url(f"/v1/mirror/pages/{page_id}/public"), headers=self._headers())
            if r.status_code in (200, 404):
                self._public_purged(page_id)
                return "done"
        except httpx.HTTPError:
            pass
        pend = dict(self.state.get("mirror_public_purge") or {})
        pend[page_id] = self.clock()
        self.state["mirror_public_purge"] = pend
        self.save()
        self._retry_at.pop("pages", None)
        self.wake()
        return "pending"

    def _public_purged(self, page_id: str) -> None:
        pend = dict(self.state.get("mirror_public_purge") or {})
        pend.pop(page_id, None)
        self.state["mirror_public_purge"] = pend
        sent = dict(self.state.get("mirror_pages_sent") or {})
        if page_id in sent:  # la copie privée reste, sans sa publication
            sent[page_id] = sent[page_id].split("|", 1)[0]
            self.state["mirror_pages_sent"] = sent
        self.save()

    def _push_pages(self) -> int:
        for pid in list(self.state.get("mirror_public_purge") or {}):  # dépublications en retard : d'abord
            r = self._http().delete(self._url(f"/v1/mirror/pages/{pid}/public"), headers=self._headers())
            if r.status_code not in (200, 404):
                r.raise_for_status()
            self._public_purged(pid)
        leaving = self._page_candidates()
        sent: dict[str, str] = dict(self.state.get("mirror_pages_sent") or {})
        refused: dict[str, Any] = dict(self.state.get("mirror_pages_refused") or {})
        n, freed = 0, False
        for pid in [p for p in sent if p not in leaving]:  # retraits d'abord : ils libèrent de la place
            r = self._http().delete(self._url(f"/v1/mirror/pages/{pid}"), headers=self._headers())
            if r.status_code not in (200, 404):
                r.raise_for_status()
            sent.pop(pid, None)
            freed, n = True, n + 1
            self.state["mirror_pages_sent"] = sent
            self.save()
        if freed:
            refused = {k: v for k, v in refused.items() if v.get("code") != "mirror_quota"}
        puts = 0
        for pid, meta in leaving.items():
            if sent.get(pid) == page_mark(meta) or (refused.get(pid) or {}).get("sha") == meta.sha256:
                continue
            if puts >= PAGES_PER_PASS:
                break
            found = self.pages.read(pid)
            if found is None:
                continue
            meta, content = found
            body = {"title": meta.title, "version": meta.version, "sha256": hashlib.sha256(content).hexdigest(),
                    "locality": self.page_locality(pid), "updated_at": meta.updated_at,
                    "html_b64": base64.b64encode(content).decode(),
                    "public": public_field(meta)}  # §7.26 : null = la copie ne sert pas en public
            r = self._http().put(self._url(f"/v1/mirror/pages/{pid}"), headers=self._headers(), json=body)
            puts += 1
            try:
                data = r.json() if r.content else {}
            except ValueError:
                data = {}
            if r.status_code == 200:
                sent[pid] = page_mark(meta)
                refused.pop(pid, None)
                self.state["mirror_pages_space"] = {"used": data.get("used"), "quota": data.get("quota")}
                n += 1
            elif r.status_code in (400, 403, 413):
                err = data.get("error") if isinstance(data.get("error"), dict) else {}
                code = str(err.get("code") or r.status_code)
                if code == "mirror_subscription":
                    raise _Refused("miroir réservé aux abonnés (403)")
                refused[pid] = {"sha": body["sha256"], "code": code, "message": str(err.get("message") or "")[:300],
                                "at": self.clock()}
                if code == "mirror_quota":
                    self.state["mirror_pages_space"] = {"used": data.get("used"), "quota": data.get("quota")}
            else:
                r.raise_for_status()
            self.state["mirror_pages_sent"], self.state["mirror_pages_refused"] = sent, refused
            self.save()
        self.state["mirror_pages_refused"] = refused
        return n

    def push_pages_once(self) -> int:
        """Copies et retraits de pages ; rend le nombre d'opérations faites (une page refusée, plafond ou forme, est
        gardée refusée pour CETTE version : elle n'est pas renvoyée à chaque passage)."""
        if self.pages is None or self.state.get("mirror_paused"):
            return 0
        try:
            n = self._push_pages()
        except (httpx.HTTPError, _Refused) as e:
            self._fail("pages", e)
            return 0
        self._ok("pages")
        return n

    # ── effacement, reprise, resynchronisation ──

    def erase_remote(self) -> int:
        """Efface chez nokker.ai les copies de cette machine et met l'envoi en pause. Lève httpx.HTTPError si nokker.ai
        ne répond pas : rien n'est mis en pause tant que l'effacement n'est pas confirmé."""
        r = self._http().post(self._url("/v1/mirror/erase"), headers=self._headers(), json={})
        r.raise_for_status()
        self.state["mirror_paused"] = True
        self.state.pop("mirror_push_cursor", None)  # une reprise renvoie tout
        self.state.pop("mirror_notes_sent", None)  # notes comprises
        self.state.pop("mirror_pages_sent", None)  # et pages (les choix restent : la reprise les recopie)
        self.state.pop("mirror_pages_refused", None)
        self.save()
        return int(r.json().get("erased_turns", 0))

    def resume(self) -> None:
        self.state.pop("mirror_paused", None)
        self.save()
        self.wake()

    def request_resync(self) -> bool:
        """Resynchronisation complète au prochain passage (tout de suite) ; False si l'envoi est en pause."""
        if self.state.get("mirror_paused"):
            return False
        self.state["mirror_resync"] = {"requested_at": self.clock()}
        self.save()
        for ch in CHANNELS:  # un canal en attente repart sans attendre
            self._retry_at.pop(ch, None)
        self.wake()
        return True

    def _listing(self, path: str, key: str) -> list[Any]:
        """Ce que nokker.ai garde de cette machine (404 : route absente d'un nokker.ai plus ancien = rien)."""
        r = self._http().get(self._url(path), headers=self._headers())
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return list(r.json().get(key) or [])

    def resync_once(self) -> bool:
        """Applique une resynchronisation demandée : rend True si elle est faite (ou s'il n'y en avait pas)."""
        if not self.state.get("mirror_resync"):
            return True
        try:
            if self.notes is not None:
                keys = self._listing("/v1/mirror/notes", "notes")
                # Empreinte vide : tout ce qui est ici repart ; ce que nokker.ai garde et que la machine n'a plus
                # part en `forget`.
                self.state["mirror_notes_sent"] = {f"{k['project']}/{k['name']}": "" for k in keys
                                                   if isinstance(k, dict) and k.get("project") and k.get("name")}
            if self.pages is not None:
                rows = self._listing("/v1/mirror/pages", "pages")
                self.state["mirror_pages_sent"] = {x["page_id"]: x.get("sha256") or "" for x in rows
                                                   if isinstance(x, dict) and x.get("page_id")}
                self.state.pop("mirror_pages_refused", None)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            self._fail("resync", e if isinstance(e, httpx.HTTPError) else _Refused("réponse illisible"))
            return False
        self.state.pop("mirror_push_cursor", None)  # tours : renvoyés (nokker.ai saute les indices présents)
        self.state.pop("mirror_cursor", None)  # et retirés (la machine saute les indices présents)
        if self.team_notes is not None:
            self.team_notes.reset_cursors()
        self.state.pop("mirror_resync", None)
        self.state["mirror_resynced_at"] = self.clock()
        self._ok("resync")
        self.save()
        return True

    # ── état visible ──

    def _pending_turns(self) -> int:
        try:
            with tenant_scope(self.tenant):
                rows = self.store.turns_after(_cursor(self.state.get("mirror_push_cursor")), BATCH * 2)
        except Exception:  # noqa: BLE001 — l'état ne casse jamais la route
            return 0
        try:
            with tenant_scope(self.tenant):
                return sum(1 for _, tid, _, d in rows if not tid.startswith(WEB_PREFIX) and not self._stays(tid, d))
        except Exception:  # noqa: BLE001
            return 0

    def status(self) -> dict[str, Any]:
        now = self.clock()
        channels = {}
        for name in CHANNELS:
            ch = (self.state.get("mirror_status") or {}).get(name)
            if ch is None:
                continue
            retry = self._retry_at.get(name)
            channels[name] = {"last_ok_at": ch.get("last_ok_at"), "failures": int(ch.get("failures") or 0),
                              "last_error": ch.get("last_error"),
                              "retry_in_s": max(0, round(retry - now)) if retry and retry > now else 0}
        pending = {"turns": self._pending_turns()}
        if self.notes is not None:
            try:
                cur, sent = self._note_candidates(), self.state.get("mirror_notes_sent") or {}
                pending["notes"] = sum(1 for k, (sha, _) in cur.items() if sent.get(k) != sha) + \
                    sum(1 for k in sent if k not in cur)
            except Exception:  # noqa: BLE001
                pending["notes"] = 0
        if self.pages is not None:
            leaving, sent = self._page_candidates(), self.state.get("mirror_pages_sent") or {}
            refused = self.state.get("mirror_pages_refused") or {}
            pending["pages"] = sum(1 for p, m in leaving.items() if sent.get(p) != page_mark(m)
                                   and (refused.get(p) or {}).get("sha") != m.sha256) + \
                sum(1 for p in sent if p not in leaving) + len(self.state.get("mirror_public_purge") or {})
        if self.team_notes is not None:
            try:
                pending["team_notes"] = self.team_notes.pending()
            except Exception:  # noqa: BLE001
                pending["team_notes"] = 0
        sends = [c["last_ok_at"] for n, c in channels.items() if n in ("turns_push", "notes", "pages", "team_notes")
                 and c.get("last_ok_at")]
        last_sent = max(sends) if sends else None
        behind = round(now - last_sent) if last_sent and any(pending.values()) else (0 if not any(pending.values())
                                                                                       else None)
        out = {"enabled": True, "paused": bool(self.state.get("mirror_paused")),
               "push_cursor": self.state.get("mirror_push_cursor"), "pull_cursor": self.state.get("mirror_cursor"),
               "last_sent_at": last_sent, "behind_s": behind, "pending": pending, "channels": channels,
               "resync": {"requested": bool(self.state.get("mirror_resync")),
                          "last_at": self.state.get("mirror_resynced_at")},
               "pages_space": self.state.get("mirror_pages_space")}
        if self.team_notes is not None:
            out["team"] = self.team_notes.status()
        return out

    # ── boucle ──

    def tick(self) -> None:
        """Un passage : la resynchronisation demandée, puis chaque canal dont l'attente est échue. Les tours partent à
        chaque passage (`push_s`), le reste toutes les `pull_s`."""
        if self._due("resync"):
            self.resync_once()
        if self._due("turns_push"):
            while self.push_once() >= BATCH:  # rattrapage (premier miroir d'un long historique)
                pass
        self._ticks += 1
        if self._ticks * self.push_s >= self.pull_s or self._wake_all:
            self._ticks, self._wake_all = 0, False
            for name, fn in (("turns_pull", self.pull_once), ("notes", self.push_notes_once),
                             ("team_notes", self.sync_team_notes_once), ("pages", self.push_pages_once)):
                if self._due(name):
                    fn()

    def _run(self) -> None:
        while not self._stop.is_set():
            woken = self._wake.wait(self.push_s)
            self._wake.clear()
            if self._stop.is_set():
                return
            if woken:
                self._wake_all = True  # un réveil (choix, resync) fait passer tous les canaux
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — un fil de fond qui meurt arrête le miroir en silence
                logger.warning("miroir : passage en échec, repris au suivant", exc_info=True)


__all__ = ["BACKOFF_BASE_S", "BACKOFF_CAP_S", "WEB_PREFIX", "MirrorWorker", "backoff_s"]
