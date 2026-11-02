"""Consolidation nocturne de la mémoire d'un compte (docs/CONSOLIDATION.md).

Décision produit (01.11.2026) : elle s'applique TOUTE SEULE, chaque nuit, sans validation préalable, et rapporte ce
qu'elle a fait pour un contrôle humain a posteriori ; chaque changement se défait (par opération ou par nuit). Les
garde-fous sont donc dans le noyau :

  - un passage = UN compte de UN locataire ; il ne lit et n'écrit que les notes que ce compte DÉTIENT (sa mémoire de
    profil ; s'il est payeur, la mémoire de son foyer), partition par partition (portée, foyer, audience) : jamais une
    note d'un autre compte, jamais deux partitions mêlées dans un même appel modèle ;
  - un seul passage à la fois par locataire (verrou), au plus `max_ops` opérations par nuit (le reste est reporté) ;
  - jamais touchées : les consignes durables (`kind = rule`, ce que le never clear et le préfixe tiennent pour des
    règles en vigueur), les notes épinglées par l'utilisateur, les épisodes épinglés ou porteurs d'un revirement ou
    d'une exigence (couche 3 du never clear) ;
  - une note d'origine utilisateur n'est jamais réécrite : seulement annotée, ou archivée (réversible) ;
  - rien n'est supprimé pour de bon : une note retirée part dans l'archive, d'où « défaire » la remet telle quelle ;
  - caviardage à l'entrée (tout ce qui part au modèle) et à la sortie (tout texte écrit, le rapport, le message).

Opérations : `merge` (doublons et quasi-doublons), `correct` (note contredite par une plus récente, la source gardée en
preuve et en annotation), `archive` (périmée), `summarize` (épisodes anciens d'un fil → une note), `lesson` (stratégie
ou piège tiré d'une correction de l'utilisateur, d'une carte refusée ou d'une action échouée, avec le fil d'origine).
Chaque opération est journalisée avec son inverse exact (instantanés avant/après, plongement compris)."""
from __future__ import annotations

import hashlib
import logging
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nokker.memory.consolidation import model as M
from nokker.memory.consolidation.stores import StoreError
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant

log = logging.getLogger("nokker.memory.consolidation")

OP_KINDS = ("merge", "correct", "archive", "summarize", "lesson")
AGENT_ORIGINS = ("propose:", "consolidation:")  # le reste (app, import:, pending:…) = mots de l'utilisateur
MAX_TITLE, MAX_TEXT = 120, 1500  # = nokker/memory/tenant_long.py
DAY = 86_400.0
LEDGER_REASON = "consolidation nocturne"  # motif des lignes du ledger

# Textes écrits par le noyau (vus par l'utilisateur, jamais par le modèle).
NOTIFY_TITLE = "Mémoire consolidée cette nuit"
NOTIFY_TEXT = ("{summary}. Tout est réversible, opération par opération ou la nuit entière : Mémoire → "
               "Consolidations.")
NOTIFY_NONE = "rien à changer"
LABELS = {"merge": ("fusion", "fusions"), "correct": ("correction", "corrections"),
          "archive": ("archivage", "archivages"), "summarize": ("résumé d'épisodes", "résumés d'épisodes"),
          "lesson": ("leçon", "leçons")}
ANN_MERGED = "Fusionnée cette nuit avec : {items} (archivées, réversible)."
ANN_MERGED_ITEM = "« {title} » : {text}"
ANN_CORRECTED = "Corrigée cette nuit d'après « {by} », plus récente (ancienne version dans le journal, réversible)."
ANN_SUPERSEDES = "Fait foi cette nuit sur « {title} », archivée (réversible)."


class ConsolidationError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Settings:
    """`memory_consolidation_settings` de la config ; chaque clé est optionnelle."""

    max_ops: int = 40  # plafond d'opérations par nuit et par compte
    tier: str = "flash"  # palier de `tiers` (config) ; absent = passage déterministe seul (doublons exacts)
    max_tokens: int = 1200
    max_notes_per_call: int = 80  # notes d'une partition montrées au modèle (les plus récentes)
    stale_min_age_days: float = 7.0  # une note plus jeune n'est jamais archivée comme périmée
    episode_age_days: float = 21.0  # épisodes clos depuis plus longtemps : résumables
    min_episodes: int = 3  # épisodes anciens d'un fil nécessaires pour un résumé
    max_episodes_per_summary: int = 12
    max_episode_threads: int = 3  # fils résumés par nuit
    lesson_window_days: float = 7.0  # situations relues à la première nuit (ensuite : depuis la nuit précédente)
    max_situations: int = 12
    max_lessons: int = 5
    default_tz: str = "Europe/Zurich"  # heure locale d'un compte sans réglage
    cron: str = "30 3 * * *"  # 03:30 heure locale
    tick_s: float = 60.0
    scheduler: bool = True  # le planificateur tourne dans le noyau (sinon : CLI ou route seulement)
    notify: bool = True  # message court du matin (bulle + Telegram/e-mail du brief s'ils sont réglés)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> Settings:
        s = cls()
        for k, v in (raw or {}).items():
            if hasattr(s, k):
                cur = getattr(s, k)
                setattr(s, k, (str(v).lower() in ("1", "true", "yes", "on")) if isinstance(cur, bool)
                        else type(cur)(v))
            else:
                log.warning("memory_consolidation_settings : clé inconnue %s ignorée", k)
        return s


@dataclass(frozen=True)
class Actor:
    account_id: str
    tenant_id: str


def norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").lower().split())


def user_origin(n: dict[str, Any]) -> bool:
    return not str(n.get("origin") or "").startswith(AGENT_ORIGINS)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _same(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if a is None or b is None:
        return a is b
    keys = ("title", "body", "kind", "audience", "origin", "scope", "household_id", "author_account")
    return all(a.get(k) == b.get(k) for k in keys) and abs(float(a["updated_at"]) - float(b["updated_at"])) < 1e-6


def _partition(n: dict[str, Any]) -> tuple[str, str | None, str]:
    return (n["scope"], n.get("household_id"), n.get("audience") or "all")


@dataclass
class _Planned:
    kind: str
    scope: str
    household_id: str | None
    reason: str
    evidence: dict[str, Any]
    changes: list[dict[str, Any]]  # action ∈ update | archive | create | annotate


@dataclass
class Consolidator:
    store: Any  # MemoryConsolidationStore | PgConsolidationStore
    settings: Settings = field(default_factory=Settings)
    complete: Any = None  # Provider (méthode `complete`) ou fonction (messages) -> str ; None = déterministe seul
    model_name: str | None = None
    signals: Any = None  # nokker/memory/consolidation/signals.py : épisodes et situations d'un compte ; None = aucun
    embed: Callable[[str, str], tuple[Any, str | None]] | None = None  # (titre, texte) → (plongement, modèle)
    scrub: Callable[[str], str] | None = None  # caviardage du noyau (`invariants.scrub_output`)
    notify: Callable[[str, str, str], Any] | None = None  # (compte, titre, texte) → statut de livraison
    ledger: Any = None  # LedgerBackend du noyau : chaque appel modèle compté, motif « consolidation nocturne »
    clock: Callable[[], float] = time.time
    _ledger_n: int = field(default=0, init=False, repr=False)

    # ── acteur ──
    def actor(self, account_id: str) -> Actor:
        if not account_id or not account_id.startswith("us_"):
            raise ConsolidationError(403, "no_account", "consolidation : compte requis")
        tid = current_tenant()
        if tid == INTERNAL_TENANT:
            raise ConsolidationError(403, "internal", "le locataire interne a sa propre revue de mémoire")
        return Actor(account_id=account_id, tenant_id=tid)

    def _s(self, text: str) -> str:
        t = str(text or "")
        if self.scrub is not None:
            t = self.scrub(t)
        else:
            from nokker.qa.secrets import redact

            t = redact(t).text
        return t

    def _ask(self, actor: Actor, system: str, user: str) -> tuple[dict[str, Any] | None, str | None]:
        """Un appel modèle, compté au ledger dans le locataire du compte (fil `consolidation-<compte>`)."""
        def on_call(usage: Any, model: str | None, seconds: float) -> None:
            if self.ledger is None:
                return
            from nokker.core.turn import LLMCall, Tier, Usage
            from nokker.tenancy.context import scoped_thread_id

            self._ledger_n += 1
            u = usage if isinstance(usage, Usage) else Usage()
            self.ledger.record(scoped_thread_id(actor.tenant_id, f"consolidation-{actor.account_id}"),
                               self._ledger_n, LLMCall(tier=Tier.DEEP, model=model or self.model_name or "",
                                                       reasoning=False, prefix_sha256="", layer_tokens={}, usage=u,
                                                       latency_s=seconds, route_reason=LEDGER_REASON))

        return M.call(self.complete, system, user, self.settings.max_tokens, on_call=on_call)

    # ─────────────────────────────── passage ───────────────────────────────

    def run(self, account_id: str, *, dry_run: bool = False, trigger: str = "manual", tz: str | None = None,
            night: str | None = None) -> dict[str, Any]:
        """Une consolidation du compte. Rend le rapport (`report()`), `{status: busy}` si une autre tourne pour ce
        locataire, ou `{status: already}` si la nuit planifiée a déjà été faite."""
        actor = self.actor(account_id)
        now = float(self.clock())
        tz_name = tz or self.settings.default_tz
        night = night or _local_day(now, tz_name)
        with self.store.lock(actor) as got:
            if not got:
                return {"status": "busy", "night": night}
            run = {"id": _uid("cr"), "night": night, "tz": tz_name, "trigger": trigger, "status": "running",
                   "dry_run": dry_run, "model": None, "started_at": now, "finished_at": None, "counts": {},
                   "skipped": 0, "error": None, "undone_at": None, "notified": {}}
            if not dry_run:
                with self.store.transaction(actor) as tx:
                    if trigger == "schedule" and tx.run_for_night(night) is not None:
                        return {"status": "already", "night": night}
                    if not tx.run_insert(run):
                        return {"status": "already", "night": night}
            try:
                planned, model = self._plan(actor, now)
                run["model"] = model
                applied, skipped = [], 0
                for p in planned:
                    if len(applied) >= self.settings.max_ops:
                        skipped += 1
                        continue
                    op = self._op_of(run["id"], len(applied), p, now)
                    if dry_run:
                        applied.append(op)
                        continue
                    if self._apply(actor, run["id"], op):
                        applied.append(op)
                counts = {k: sum(1 for o in applied if o["kind"] == k) for k in OP_KINDS}
                run.update(counts=counts, skipped=skipped, status="done", finished_at=float(self.clock()))
            except Exception as e:
                log.exception("consolidation : passage en échec")
                run.update(status="failed", error=type(e).__name__, finished_at=float(self.clock()))
                applied = []
            if dry_run:
                return self._view(run, applied, actor)
            with self.store.transaction(actor) as tx:
                tx.run_update(run["id"], status=run["status"], model=run["model"], counts=run["counts"],
                              skipped=run["skipped"], error=run["error"], finished_at=run["finished_at"])
        if run["status"] == "done" and self.notify is not None and self.settings.notify:
            self._notify(actor, run)
        return self.report(account_id, run["id"])

    # ─────────────────────────────── plan ───────────────────────────────

    def _plan(self, actor: Actor, now: float) -> tuple[list[_Planned], str | None]:
        with self.store.transaction(actor) as tx:
            notes = tx.notes()
            pins = tx.pins()
            last = next((r for r in tx.runs(20) if r["status"] == "done" and not r["dry_run"]), None)
            summarized = {e for o in tx.live_ops("summarize") for e in (o.get("evidence") or {}).get("episodes", [])}
            used = {(o.get("evidence") or {}).get("key") for o in tx.live_ops("lesson")}
        protected = {n["name"] for n in notes if n["kind"] == "rule" or n["name"] in pins}
        touched: set[str] = set()
        out: list[_Planned] = []
        model_used: str | None = None
        parts: dict[tuple[str, str | None, str], list[dict[str, Any]]] = {}
        for n in notes:
            parts.setdefault(_partition(n), []).append(n)
        for key in sorted(parts, key=lambda k: (k[0], k[1] or "", k[2])):
            group = parts[key]
            out += self._exact_dupes(group, protected, touched)
            if self.complete is not None:
                got, m = self._model_notes(actor, group, protected, touched, now)
                out += got
                model_used = model_used or m
        if self.signals is not None:
            got, m = self._episodes(actor, now, summarized, notes)
            out += got
            model_used = model_used or m
            since = float(last["started_at"]) if last else now - self.settings.lesson_window_days * DAY
            got, m = self._lessons(actor, since, used, notes)
            out += got
            model_used = model_used or m
        return out, (model_used or self.model_name) if self.complete is not None else None

    def _exact_dupes(self, group: list[dict[str, Any]], protected: set[str], touched: set[str]) -> list[_Planned]:
        by_text: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for n in group:
            by_text.setdefault((n["kind"], norm(n["body"])), []).append(n)
        out = []
        for same in by_text.values():
            same = [n for n in same if n["name"] not in touched]
            if len(same) < 2:
                continue
            keep = min(same, key=lambda n: (n["name"] not in protected, not user_origin(n), n["created_at"]))
            drops = [n for n in same if n is not keep and n["name"] not in protected]
            if not drops:
                continue
            out.append(self._merge_plan(keep, drops, None, "doublon exact (même texte)", protected, touched))
        return out

    def _merge_plan(self, keep: dict[str, Any], drops: list[dict[str, Any]], merged: tuple[str, str] | None,
                    reason: str, protected: set[str], touched: set[str]) -> _Planned:
        changes: list[dict[str, Any]] = []
        if merged is not None and not user_origin(keep) and keep["name"] not in protected:
            title, text = merged
            if (title, text) != (keep["title"], keep["body"]):
                changes.append({"name": keep["name"], "action": "update", "before": keep,
                                "after": self._rewritten(keep, title, text)})
        for d in drops:
            changes.append({"name": d["name"], "action": "archive", "before": d, "after": None})
        if keep["name"] not in protected:
            items = " ; ".join(ANN_MERGED_ITEM.format(title=d["title"], text=_clip(d["body"], 300)) for d in drops)
            changes.append({"name": keep["name"], "action": "annotate", "text": ANN_MERGED.format(items=items)})
        touched.update([keep["name"], *(d["name"] for d in drops)])
        return _Planned(kind="merge", scope=keep["scope"], household_id=keep.get("household_id"), reason=reason,
                        evidence={"kept": keep["name"], "merged": [d["name"] for d in drops]}, changes=changes)

    def _rewritten(self, n: dict[str, Any], title: str, text: str) -> dict[str, Any]:
        after = {**n, "title": title, "body": text, "updated_at": float(self.clock())}
        vec, model = self.embed(title, text) if self.embed is not None else (None, None)
        after["embedding"], after["embed_model"] = vec, model
        return after

    def _clean_text(self, title: Any, text: Any) -> tuple[str, str] | None:
        if not isinstance(title, str) or not isinstance(text, str):
            return None
        title = " ".join(title.split())
        text = text.strip()
        for sep in (" — ", " - ", " : "):  # le modèle recopie parfois la ligne entière (« titre — texte »)
            if title and text.startswith(title + sep):
                text = text[len(title) + len(sep):].strip()
        title, text = self._s(title)[:MAX_TITLE], self._s(text)[:MAX_TEXT]
        return (title, text) if title and text else None

    def _model_notes(self, actor: Actor, group: list[dict[str, Any]], protected: set[str], touched: set[str],
                     now: float) -> tuple[list[_Planned], str | None]:
        shown = sorted((n for n in group if n["name"] not in touched), key=lambda n: -n["updated_at"])
        shown = shown[: self.settings.max_notes_per_call]
        if len([n for n in shown if n["name"] not in protected]) < 1 or len(shown) < 2:
            return [], None
        shown.sort(key=lambda n: n["updated_at"])
        user = M.render_notes(shown, protected, user_origin, _day(now))
        obj, model = self._ask(actor, M.NOTES_SYSTEM, user)
        if not obj:
            return [], model

        def pick(i: Any) -> dict[str, Any] | None:
            return shown[i - 1] if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(shown) else None

        def free(n: dict[str, Any] | None) -> bool:
            return n is not None and n["name"] not in protected and n["name"] not in touched

        out: list[_Planned] = []
        for m in obj.get("merges") or []:
            if not isinstance(m, dict):
                continue
            keep = pick(m.get("keep"))
            drops = [pick(i) for i in (m.get("drop") or []) if isinstance(m.get("drop"), list)]
            if not free(keep) or not drops or not all(free(d) for d in drops):
                continue
            if len({d["name"] for d in drops} | {keep["name"]}) != len(drops) + 1:
                continue
            # Déterministe, APRÈS le modèle : si le groupe contient une note écrite par la personne, c'est elle qui
            # est gardée, telle quelle (ses mots) ; la fusion lui ajoute une annotation, les autres sont archivées.
            members = [keep, *drops]
            if not user_origin(keep) and any(user_origin(n) for n in members):
                keep = min((n for n in members if user_origin(n)), key=lambda n: n["created_at"])
                drops = [n for n in members if n is not keep]
            source = "\n".join(f"{n['title']} {n['body']}" for n in [keep, *drops])
            merged = self._clean_text(m.get("title"), m.get("text"))
            if merged is not None and not _grounded(merged, source):
                merged = None  # invention : on fusionne sans réécrire
            out.append(self._merge_plan(keep, drops, merged, self._reason(m, "quasi-doublon"), protected, touched))
        for c in obj.get("corrections") or []:
            if not isinstance(c, dict):
                continue
            target, by = pick(c.get("target")), pick(c.get("by"))
            if not free(target) or by is None or by is target or by["name"] in touched:
                continue
            if float(by["updated_at"]) <= float(target["updated_at"]):
                continue  # seule une note PLUS RÉCENTE fait foi
            out.append(self._correct_plan(target, by, c, protected, touched))
        for st in obj.get("stale") or []:
            if not isinstance(st, dict):
                continue
            target = pick(st.get("target"))
            if not free(target) or now - float(target["updated_at"]) < self.settings.stale_min_age_days * DAY:
                continue
            touched.add(target["name"])
            out.append(_Planned(kind="archive", scope=target["scope"], household_id=target.get("household_id"),
                                reason=self._reason(st, "périmée"), evidence={"note": target["name"]},
                                changes=[{"name": target["name"], "action": "archive", "before": target,
                                          "after": None}]))
        return out, model

    def _reason(self, obj: dict[str, Any], default: str) -> str:
        r = obj.get("reason")
        return self._s(" ".join(r.split()))[:300] if isinstance(r, str) and r.strip() else default

    def _correct_plan(self, target: dict[str, Any], by: dict[str, Any], c: dict[str, Any], protected: set[str],
                      touched: set[str]) -> _Planned:
        changes: list[dict[str, Any]] = []
        evidence = {"note": target["name"], "by": by["name"], "by_title": by["title"], "by_text": by["body"][:300]}
        text = c.get("text")
        fixed = self._clean_text(target["title"], text) if isinstance(text, str) else None
        if fixed is not None and not M.grounded(fixed[1], f"{target['title']} {target['body']}\n{by['title']} "
                                                          f"{by['body']}"):
            fixed = None
        if user_origin(target) or fixed is None:
            # mots de l'utilisateur (ou correction non fondée) : jamais réécrits — archivés, la note récente fait foi
            changes.append({"name": target["name"], "action": "archive", "before": target, "after": None})
            if by["name"] not in protected:
                changes.append({"name": by["name"], "action": "annotate",
                                "text": ANN_SUPERSEDES.format(title=target["title"])})
        else:
            changes.append({"name": target["name"], "action": "update", "before": target,
                            "after": self._rewritten(target, fixed[0], fixed[1])})
            changes.append({"name": target["name"], "action": "annotate",
                            "text": ANN_CORRECTED.format(by=by["title"])})
        touched.update([target["name"], by["name"]])
        return _Planned(kind="correct", scope=target["scope"], household_id=target.get("household_id"),
                        reason=self._reason(c, "contredite par une note plus récente"), evidence=evidence,
                        changes=changes)

    # ── épisodes anciens ──
    def _episodes(self, actor: Actor, now: float, summarized: set[str],
                  notes: list[dict[str, Any]]) -> tuple[list[_Planned], str | None]:
        if self.complete is None:
            return [], None
        cutoff = now - self.settings.episode_age_days * DAY
        out: list[_Planned] = []
        model: str | None = None
        existing = {norm(n["body"]) for n in notes}
        for thread_id, episodes in self.signals.old_episodes(actor.account_id, cutoff):
            if len(out) >= self.settings.max_episode_threads:
                break
            eps = [e for e in episodes if e.id not in summarized][: self.settings.max_episodes_per_summary]
            if len(eps) < self.settings.min_episodes:
                continue
            obj, m = self._ask(actor, M.EPISODES_SYSTEM, M.render_episodes(eps))
            model = model or m
            got = self._clean_text((obj or {}).get("title"), (obj or {}).get("text"))
            source = "\n".join(f"{e.title} {e.summary}" for e in eps)
            if got is None or not _grounded(got, source) or norm(got[1]) in existing:
                continue
            snap = self._new_note("fact", got[0], got[1], actor, f"consolidation:episodes:{thread_id}")
            out.append(_Planned(kind="summarize", scope="user", household_id=None,
                                reason=f"{len(eps)} épisodes clos depuis plus de "
                                       f"{int(self.settings.episode_age_days)} jours",
                                evidence={"thread_id": thread_id, "episodes": [e.id for e in eps],
                                          "titles": [e.title for e in eps]},
                                changes=[{"name": snap["name"], "action": "create", "before": None, "after": snap}]))
        return out, model

    def _new_note(self, kind: str, title: str, text: str, actor: Actor, origin: str) -> dict[str, Any]:
        from nokker.memory.tenant_long import note_name

        now = float(self.clock())
        vec, model = self.embed(title, text) if self.embed is not None else (None, None)
        return {"name": note_name(kind, title, text, now), "scope": "user", "household_id": None, "audience": "all",
                "kind": kind, "title": title, "body": text, "author_account": actor.account_id, "origin": origin,
                "embed_model": model, "embedding": vec, "created_at": now, "updated_at": now}

    # ── leçons (ReasoningBank) ──
    def _lessons(self, actor: Actor, since: float, used: set[Any],
                 notes: list[dict[str, Any]]) -> tuple[list[_Planned], str | None]:
        if self.complete is None:
            return [], None
        sits = [s for s in self.signals.situations(actor.account_id, since) if s["key"] not in used]
        sits = sits[-self.settings.max_situations:]
        if not sits:
            return [], None
        obj, model = self._ask(actor, M.LESSONS_SYSTEM, M.render_situations(sits))
        out: list[_Planned] = []
        existing = {norm(n["body"]) for n in notes}
        seen_src: set[int] = set()
        for le in (obj or {}).get("lessons") or []:
            if len(out) >= self.settings.max_lessons or not isinstance(le, dict):
                break
            i, kind = le.get("source"), le.get("kind")
            if not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= len(sits) or i in seen_src \
                    or kind not in M.LESSON_TITLE:
                continue
            got = self._clean_text(le.get("title"), le.get("text"))
            s = sits[i - 1]
            source = " ".join(str(s.get(k, "")) for k in ("before", "user", "after", "tool", "reason"))
            if got is None or not _grounded(got, source) or norm(got[1]) in existing:
                continue
            seen_src.add(i)
            title = M.LESSON_TITLE[kind].format(title=got[0])[:MAX_TITLE]
            origin = f"consolidation:lesson:{s['thread_id']}#T{s.get('turn', '')}"
            snap = self._new_note("feedback", title, got[1], actor, origin)
            out.append(_Planned(kind="lesson", scope="user", household_id=None,
                                reason={"correction": "correction de l'utilisateur", "refused": "action refusée",
                                        "failed": "action échouée"}[s["source"]],
                                evidence={"key": s["key"], "source": s["source"], "thread_id": s["thread_id"],
                                          "turn": s.get("turn"), "excerpt": self._s(source)[:400],
                                          "lesson_kind": kind},
                                changes=[{"name": snap["name"], "action": "create", "before": None, "after": snap}]))
        return out, model

    # ─────────────────────────────── application ───────────────────────────────

    def _op_of(self, run_id: str, seq: int, p: _Planned, now: float) -> dict[str, Any]:
        authors = sorted({c[k]["author_account"] for c in p.changes for k in ("before", "after")
                          if isinstance(c.get(k), dict)})
        return {"id": _uid("co"), "run_id": run_id, "seq": seq, "kind": p.kind, "scope": p.scope,
                "household_id": p.household_id, "reason": p.reason, "evidence": p.evidence,
                "changes": [dict(c) for c in p.changes], "authors": authors, "at": now, "undone_at": None}

    def _apply(self, actor: Actor, run_id: str, op: dict[str, Any]) -> bool:
        """Une opération, dans une transaction : chaque note touchée doit être encore telle que planifiée (sinon
        l'opération est abandonnée, rien n'est écrit) ; puis les changements, puis la ligne du journal."""
        now = float(self.clock())
        try:
            with self.store.transaction(actor) as tx:
                for c in op["changes"]:
                    if c["action"] in ("update", "archive") and not _same(tx.note(c["name"]), c["before"]):
                        raise _Stale(c["name"])
                    if c["action"] == "create" and tx.note(c["name"]) is not None:
                        raise _Stale(c["name"])
                for c in op["changes"]:
                    a = c["action"]
                    if a == "update":
                        tx.update(c["after"])
                    elif a == "archive":
                        tx.archive_put(c["before"], op["id"], op["reason"], now)
                        tx.delete(c["name"])
                    elif a == "create":
                        tx.insert(c["after"])
                    elif a == "annotate":
                        c["annotation"] = _uid("an")
                        tx.annotate({"id": c["annotation"], "name": c["name"], "run_id": run_id, "op_id": op["id"],
                                     "text": c["text"], "at": now})
                tx.op_insert(op)
            return True
        except _Stale as e:
            log.info("consolidation : note %s modifiée pendant le passage, opération abandonnée", e)
            return False

    # ─────────────────────────────── défaire ───────────────────────────────

    def undo_op(self, account_id: str, op_id: str) -> dict[str, Any]:
        """Défait UNE opération (idempotent : la refaire rend le même état). 409 `conflict` si une note touchée a changé
        depuis (l'utilisateur l'a modifiée) : rien n'est écrit."""
        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            op = tx.op_get(op_id)
            if op is None:
                raise ConsolidationError(404, "not_found", "opération inconnue")
            if op.get("undone_at") is not None:
                return {"op": op_id, "undone": True, "already": True}
            self._undo_in(tx, op)
            return {"op": op_id, "undone": True, "already": False}

    def _undo_in(self, tx: Any, op: dict[str, Any]) -> None:
        now = float(self.clock())
        for c in reversed(op["changes"]):
            a = c["action"]
            cur = tx.note(c["name"]) if a != "annotate" else None
            if a in ("update", "create") and not _same(cur, c["after"]):
                raise ConsolidationError(409, "conflict", f"la note {c['name']} a changé depuis la consolidation")
            if a == "archive":
                arc = tx.archived(c["name"], op["id"])
                if cur is not None or arc is None or arc.get("restored_at") is not None:
                    raise ConsolidationError(409, "conflict", f"la note {c['name']} est déjà revenue ou remplacée")
        for c in reversed(op["changes"]):
            a = c["action"]
            if a == "update":
                tx.update(c["before"])
            elif a == "create":
                tx.archive_put(c["after"], op["id"], "défaite", now)  # rien n'est supprimé pour de bon
                tx.delete(c["name"])
            elif a == "archive":
                tx.insert(c["before"])
                tx.archive_restored(c["name"], op["id"], now)
            elif a == "annotate" and c.get("annotation"):
                tx.annotation_undo(c["annotation"], now)
        tx.op_undone(op["id"], now)

    def undo_run(self, account_id: str, run_id: str) -> dict[str, Any]:
        """Défait la nuit entière, de la dernière opération à la première ; chaque opération dans sa transaction.
        Idempotent. Une opération en conflit est rapportée et laissée en place ; les autres sont défaites."""
        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            run = tx.run_get(run_id)
            if run is None:
                raise ConsolidationError(404, "not_found", "consolidation inconnue")
            ops = tx.ops(run_id)
        undone, already, conflicts = [], [], []
        for op in reversed(ops):
            if op.get("undone_at") is not None:
                already.append(op["id"])
                continue
            try:
                with self.store.transaction(actor) as tx:
                    self._undo_in(tx, op)
                undone.append(op["id"])
            except ConsolidationError as e:
                if e.code != "conflict":
                    raise
                conflicts.append({"op": op["id"], "message": e.message})
        if not conflicts:
            with self.store.transaction(actor) as tx:
                if tx.run_get(run_id).get("undone_at") is None:
                    tx.run_update(run_id, undone_at=float(self.clock()))
        return {"run": run_id, "undone": undone, "already": already, "conflicts": conflicts,
                "complete": not conflicts}

    # ─────────────────────────────── rapport ───────────────────────────────

    def runs(self, account_id: str, limit: int = 30) -> dict[str, Any]:
        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            rows = tx.runs(max(1, min(int(limit), 100)))
        return {"runs": [self._run_view(r) for r in rows]}

    def report(self, account_id: str, run_id: str) -> dict[str, Any]:
        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            run = tx.run_get(run_id)
            if run is None:
                raise ConsolidationError(404, "not_found", "consolidation inconnue")
            ops = tx.ops(run_id)
        return self._view(run, ops, actor)

    def _run_view(self, r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "night": r["night"], "tz": r["tz"], "trigger": r["trigger"], "status": r["status"],
                "dry_run": bool(r["dry_run"]), "model": r.get("model"), "started_at": r["started_at"],
                "finished_at": r.get("finished_at"), "counts": r.get("counts") or {}, "skipped": r.get("skipped", 0),
                "undone_at": r.get("undone_at"), "summary": self._summary(r.get("counts") or {})}

    def _note_view(self, n: dict[str, Any] | None) -> dict[str, Any] | None:
        if n is None:
            return None
        return {"title": self._s(n["title"]), "text": self._s(n["body"]), "kind": n["kind"],
                "origin": "user" if user_origin(n) else "agent", "audience": n.get("audience") or "all"}

    def _evidence_view(self, o: dict[str, Any], actor: Actor, clean: Callable[[Any], Any]) -> dict[str, Any]:
        """Preuve d'une opération dans le rapport. Opération de portée FOYER, ou preuve qui cite un fil qui n'est pas au
        compte qui lit : réduite à des comptes et à des identifiants opaques — ni titre, ni extrait, ni texte d'une
        note, ni fil (un membre ne voit jamais le titre ou l'extrait d'un fil privé d'un autre membre). Sauf si le fil
        cité appartient au compte qui lit (son propre locataire)."""
        ev = o.get("evidence") or {}
        thread = ev.get("thread_id")
        own = isinstance(thread, str) and thread.startswith(actor.tenant_id + ":")
        if own or (o.get("scope") != "household" and thread is None):
            return {k: clean(v) for k, v in ev.items()}

        def opaque(x: Any) -> str:
            return "ev_" + hashlib.sha256(f"{actor.tenant_id}\x00{x}".encode()).hexdigest()[:12]

        counts = {k: len(v) for k, v in ev.items() if isinstance(v, list)}
        ids = [opaque(ev[k]) for k in ("key", "note", "by", "kept") if isinstance(ev.get(k), str)]
        ids += [opaque(x) for k in ("merged", "episodes") for x in ev.get(k) or [] if isinstance(x, str)]
        out: dict[str, Any] = {"counts": counts, "ids": ids, "reduced": True}
        for k in ("source", "lesson_kind"):  # étiquettes fixes du noyau, jamais du texte
            if isinstance(ev.get(k), str):
                out[k] = ev[k]
        return out

    def _view(self, run: dict[str, Any], ops: list[dict[str, Any]], actor: Actor) -> dict[str, Any]:
        """Rapport consultable : jamais le locataire, un plongement, ni l'auteur d'une note ; tout texte recaviardé ;
        preuve d'une opération du foyer réduite (`_evidence_view`)."""
        def clean(v: Any) -> Any:
            if isinstance(v, str):
                return self._s(v)
            if isinstance(v, list):
                return [clean(x) for x in v]
            return v

        out_ops = []
        for o in ops:
            ev = self._evidence_view(o, actor, clean)
            out_ops.append({
                "id": o["id"], "seq": o["seq"], "kind": o["kind"], "scope": o["scope"], "reason": self._s(o["reason"]),
                "evidence": ev, "at": o["at"], "undone_at": o.get("undone_at"),
                "notes": [{"name": c["name"], "action": c["action"],
                           **({"text": self._s(c.get("text", ""))} if c["action"] == "annotate" else
                              {"before": self._note_view(c.get("before")), "after": self._note_view(c.get("after"))})}
                          for c in o["changes"]]})
        return {"run": self._run_view(run), "ops": out_ops}

    def _summary(self, counts: dict[str, int]) -> str:
        parts = [f"{n} {LABELS[k][0] if n == 1 else LABELS[k][1]}" for k in OP_KINDS if (n := counts.get(k, 0))]
        return ", ".join(parts) if parts else NOTIFY_NONE

    def _notify(self, actor: Actor, run: dict[str, Any]) -> None:
        """Message court du matin : des nombres, jamais le contenu d'une note (aucun secret ne peut y passer)."""
        if not any((run.get("counts") or {}).values()):
            return
        text = NOTIFY_TEXT.format(summary=self._summary(run["counts"]).capitalize())
        try:
            res = self.notify(actor.account_id, NOTIFY_TITLE, self._s(text))
        except Exception:
            log.warning("consolidation : message du matin non envoyé", exc_info=True)
            res = {"error": "failed"}
        try:
            with self.store.transaction(actor) as tx:
                tx.run_update(run["id"], notified=res if isinstance(res, dict) else {"sent": bool(res)})
        except StoreError:
            log.warning("consolidation : statut du message non noté", exc_info=True)

    # ─────────────────────────────── épingles, réglages ───────────────────────────────

    def pin(self, account_id: str, name: str, pinned: bool) -> dict[str, Any]:
        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            if pinned:
                if tx.note(name) is None:
                    raise ConsolidationError(404, "not_found", "note inconnue (ou d'un autre compte)")
                tx.pin(name, float(self.clock()))
            else:
                tx.unpin(name)
            return {"name": name, "pinned": pinned, "pins": sorted(tx.pins())}

    def pins(self, account_id: str) -> dict[str, Any]:
        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            return {"pins": sorted(tx.pins())}

    def get_settings(self, account_id: str) -> dict[str, Any]:
        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            row = tx.settings_get()
        return self._settings_view(row)

    def _settings_view(self, row: dict[str, Any] | None) -> dict[str, Any]:
        row = row or {}
        return {"enabled": bool(row.get("enabled", True)), "tz": row.get("tz") or self.settings.default_tz,
                "cron": row.get("cron") or self.settings.cron, "max_ops": self.settings.max_ops}

    def put_settings(self, account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from nokker.agents.cron import Cron, CronError, zone

        actor = self.actor(account_id)
        with self.store.transaction(actor) as tx:
            cur = self._settings_view(tx.settings_get())
            enabled = body.get("enabled", cur["enabled"])
            tz, cron = body.get("tz", cur["tz"]), body.get("cron", cur["cron"])
            if not isinstance(enabled, bool) or not isinstance(tz, str) or not isinstance(cron, str):
                raise ConsolidationError(400, "bad_body", "corps {enabled?: bool, tz?: fuseau, cron?: 5 champs}")
            try:
                zone(tz)
                Cron.parse(cron)
            except CronError as e:
                raise ConsolidationError(400, "bad_schedule", str(e)) from None
            tx.settings_put({"enabled": enabled, "tz": tz, "cron": cron, "updated_at": float(self.clock())})
            return self._settings_view(tx.settings_get())

    def schedule_of(self, actor: Actor) -> dict[str, Any]:
        with self.store.transaction(actor) as tx:
            return self._settings_view(tx.settings_get())


class _Stale(Exception):
    pass


def _clip(text: str, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _grounded(pair: tuple[str, str], source: str) -> bool:
    """Titre et texte vérifiés À PART (une majuscule de début de texte n'est pas un nom propre inventé)."""
    return M.grounded(pair[0], source) and M.grounded(pair[1], source)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _local_day(ts: float, tz_name: str) -> str:
    from nokker.agents.cron import CronError, zone

    try:
        tz = zone(tz_name)
    except CronError:
        tz = UTC  # type: ignore[assignment]
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


__all__ = ["AGENT_ORIGINS", "Actor", "ConsolidationError", "Consolidator", "Settings", "user_origin"]
