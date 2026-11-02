"""Mémoire longue multi-locataire (docs/MEMORY-TENANCY.md), drapeau `tenant_long_memory` (défaut false : rien ne change).

Chez nokker.ai, un locataire n'a pas d'index CortHeXis à lui (docs/TENANCY.md §5). Cette mémoire lui en donne un, par
COMPTE, sans jamais rien ouvrir d'un compte à un autre :

  - portée `user` : la mémoire de profil du compte, dans SON locataire, lue et écrite par lui seul ;
  - portée `household` : la mémoire commune de son foyer (lot X23), dans le locataire du foyer, détenue par le payeur,
    lue par chaque membre ACTIF ; une note `audience = adults` n'est jamais rappelée à un profil mineur ; un membre
    propose, le payeur relit (quarantaine de docs/LEARNING.md §3, `nokker_memory_pending`, réutilisée telle quelle) ;
  - `team` et `session` ne passent pas par ici (notes d'équipe : `team_notes` ; fils : never clear).

Stockage (migration 049) : table `nokker_long_memory` de la base du noyau, RLS activée ET forcée, un seul index pgvector
partagé filtré par la RLS ; les plongements viennent du moteur d'embedding de CortHeXis (`corthexis.embed`, service
d'embed `CORTHEXIS_EMBED_URLS`), le format de note est celui de CortHeXis (nom `<type>-<titre>-<hash>`, titre, texte).
Sans pgvector ou sans embed : rappel lexical (mêmes notes, même isolation), jamais une erreur de tour.

Rappel par tour (`[Rappel mémoire]`, format inchangé) : user + household (notes de cette table ET notes du foyer
écrites dans l'app, `HouseholdRecall`), quotas par portée (`user_k`, `household_k`), les places laissées libres par une
portée vont à l'autre. Ligne d'une note de profil : `titre — texte` ; d'une note du foyer : `[Mémoire du foyer] titre —
texte` (texte existant de §7.29.2)."""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import Recall, ToolContext, ToolSpec
from nokker.core.turn import ToolResult
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant

log = logging.getLogger("nokker.memory.tenant_long")

SCOPES = ("user", "household")
AUDIENCES = ("all", "adults")
KINDS = ("rule", "feedback", "fact")  # = nokker.context.project_memory.KINDS
MAX_TITLE = 120
MAX_TEXT = 1500
HOUSEHOLD_PREFIX = "foyer:"  # = nokker.household.memory.PREFIX : même espace de noms que les notes du foyer de l'app
HOUSEHOLD_LINE = "[Mémoire du foyer] {title} — {text}"  # texte existant (§7.29.2), inchangé
USER_LINE = "{title} — {text}"
# Leçon écrite par la consolidation nocturne (« Stratégie : … », « Piège à éviter : … », origine
# `consolidation:lesson:…`, docs/CONSOLIDATION.md) : rappelée dans `[Rappel mémoire]` avec son origine, jamais comme une
# consigne de l'utilisateur. N'existe que si la consolidation a tourné (drapeau `memory_consolidation`, éteint).
LESSON_ORIGIN_PREFIX = "consolidation:lesson:"
LESSON_LINE = "{title} (apprise par l'assistant) — {text}"


def user_line(n: LongNote) -> str:
    """Ligne de rappel d'une note du compte : `USER_LINE`, ou `LESSON_LINE` pour une leçon de la consolidation."""
    line = LESSON_LINE if str(n.origin or "").startswith(LESSON_ORIGIN_PREFIX) else USER_LINE
    return line.format(title=n.title, text=n.text)


class LongMemoryError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Settings:
    user_k: int = 3
    household_k: int = 2
    min_score: float = 0.35  # score hybride (0,7 × cosinus + 0,3 × lexical) — à recaler sur un banc
    relative_floor: float = 0.6  # ≥ 60 % du meilleur, comme les autres rappels
    lexical_min_score: float = 0.3  # sans plongement : seuil des notes du foyer (nokker/household/memory.py)
    vector_weight: float = 0.7
    max_candidates: int = 500  # notes lues par portée et par tour (recherche exacte)
    tools: bool = False  # memory_search / memory_get pour les testeurs (textes existants) — à valider avant d'allumer

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> Settings:
        s = cls()
        for k, v in (raw or {}).items():
            if hasattr(s, k):
                setattr(s, k, type(getattr(s, k))(v))
            else:
                log.warning("tenant_long_memory_settings : clé inconnue %s ignorée", k)
        return s


@dataclass(frozen=True)
class Actor:
    account_id: str
    tenant_id: str


@dataclass(frozen=True)
class Membership:
    """Adhésion ACTIVE d'un compte à un foyer (un foyer par compte). `profile` : adult | teen | child."""

    household_id: str
    tenant_id: str  # locataire du FOYER (celui du payeur)
    payer_account: str
    profile: str = "adult"

    @property
    def adult(self) -> bool:
        return self.profile == "adult"


@dataclass
class LongNote:
    name: str
    scope: str
    kind: str
    title: str
    text: str
    account_id: str  # détenteur : le compte (user) ; le payeur (household)
    tenant_id: str
    author_account: str
    household_id: str | None = None
    audience: str = "all"
    origin: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    embed_model: str | None = None
    embedding: list[float] | None = field(default=None, repr=False)

    @property
    def seq(self) -> float:  # `household.memory.rank` départage par récence
        return self.updated_at

    def public(self, viewer: str) -> dict[str, Any]:
        """Vue d'un compte : jamais le locataire, ni le plongement, ni l'auteur d'une note d'un autre membre."""
        d = {"name": self.name, "scope": self.scope, "kind": self.kind, "title": self.title, "text": self.text,
             "audience": self.audience, "created_at": self.created_at, "updated_at": self.updated_at,
             "mine": self.author_account == viewer}
        if self.scope == "household":
            d["household_id"] = self.household_id
        return d


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").lower().split())


def note_name(kind: str, title: str, text: str, created: float) -> str:
    from nokker.context.project_memory import slug

    return f"{kind}-{slug(title)}-{hashlib.sha1(f'{text}{created}'.encode()).hexdigest()[:6]}"


# ─────────────────────────────── plongements ───────────────────────────────


class HashEmbedder:
    """Plongement déterministe sans service (tests, développement) : sac de mots haché, normé. Un texte identique donne
    un vecteur identique — ce que les tests d'isolation utilisent pour prouver qu'aucune similarité ne traverse."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def identity(self) -> str:
        return f"hash-{self.dim}"

    def _vec(self, text: str) -> list[float]:
        from nokker.context.text import content_words

        v = [0.0] * self.dim
        for w in content_words(text or ""):
            v[int(hashlib.sha1(w.encode()).hexdigest()[:8], 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class CorthexisEmbedder:
    """Le moteur d'embedding de CortHeXis (`corthexis.embed.get()`, service `CORTHEXIS_EMBED_URLS`, profil
    `CORTHEXIS_MEMORY_PROFILE`), importé paresseusement : sans le paquet, `available()` est faux et le rappel lexical."""

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def _engine(self) -> Any:
        from corthexis import embed as cx_embed

        return cx_embed.get()

    def available(self) -> bool:
        try:
            self._engine()
            return True
        except Exception:  # noqa: BLE001
            return False

    def identity(self) -> str:
        return str(self._engine().identity())

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._engine().embed_docs(texts, timeout=max(self.timeout, 30.0))]

    def embed_query(self, text: str) -> list[float]:
        return list(map(float, self._engine().embed_query(text, timeout=self.timeout)))


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    num = sum(x * y for x, y in zip(a, b, strict=True))
    da, db = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else None


# ─────────────────────────────── magasins ───────────────────────────────


class MemoryLongStore:
    """En mémoire (tests, instance sans base) : mêmes règles de visibilité que les politiques de la migration 049,
    écrites en Python. `membership(compte)` : l'adhésion ACTIVE au foyer, ou None."""

    def __init__(self, membership: Callable[[str], Membership | None]) -> None:
        self.membership = membership
        self.notes: dict[tuple[str, str, str], LongNote] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.vector = True

    def _visible(self, actor: Actor, n: LongNote) -> bool:
        if n.scope == "user":
            return n.tenant_id == actor.tenant_id and n.account_id == actor.account_id
        if n.tenant_id == actor.tenant_id and n.account_id == actor.account_id:
            return True  # le payeur, détenteur
        m = self.membership(actor.account_id)
        return (m is not None and m.household_id == n.household_id and m.tenant_id == n.tenant_id
                and (n.audience == "all" or m.adult))

    def put(self, actor: Actor, n: LongNote, vec: list[float] | None, model: str | None) -> None:
        if n.tenant_id != actor.tenant_id or n.account_id != actor.account_id:
            raise LongMemoryError(403, "forbidden", "écriture hors du compte")
        n.embedding, n.embed_model = vec, model
        with self._lock:
            self.notes[(n.tenant_id, n.account_id, n.name)] = n

    def candidates(self, actor: Actor, scope: str, m: Membership | None, qvec: list[float] | None, model: str | None,
                   limit: int) -> list[tuple[LongNote, float | None]]:
        with self._lock:
            rows = [n for n in self.notes.values() if n.scope == scope and self._visible(actor, n)
                    and (scope == "user" or (m is not None and n.household_id == m.household_id
                                             and n.tenant_id == m.tenant_id))]
        rows.sort(key=lambda n: -n.updated_at)
        out = []
        for n in rows[:limit]:
            cos = cosine(qvec, n.embedding) if qvec is not None and n.embed_model == model else None
            out.append((n, cos))
        return out

    def find(self, actor: Actor, name: str, m: Membership | None) -> LongNote | None:
        with self._lock:
            for n in self.notes.values():
                if n.name == name and self._visible(actor, n) and (
                        n.scope == "user" or (m is not None and n.household_id == m.household_id)):
                    return n
        return None

    def delete(self, actor: Actor, n: LongNote) -> bool:
        with self._lock:
            if not self._visible(actor, n):
                return False
            owner = n.tenant_id == actor.tenant_id and n.account_id == actor.account_id
            if not owner and not (n.scope == "household" and n.author_account == actor.account_id):
                return False
            return self.notes.pop((n.tenant_id, n.account_id, n.name), None) is not None

    def set_audience(self, actor: Actor, n: LongNote, audience: str) -> bool:
        with self._lock:
            if n.tenant_id != actor.tenant_id or n.account_id != actor.account_id:
                return False
            n.audience = audience
            n.updated_at = time.time()
            return True

    def submit(self, actor: Actor, m: Membership, row: dict[str, Any]) -> None:
        with self._lock:
            self.pending[row["id"]] = {**row, "tenant_id": m.tenant_id, "account_id": m.payer_account,
                                       "author_account": actor.account_id}

    def pending_meta(self, actor: Actor, pending_id: str) -> tuple[str, str] | None:
        with self._lock:
            r = self.pending.get(pending_id)
        if r is None or r["tenant_id"] != actor.tenant_id or r["account_id"] != actor.account_id:
            return None
        return r["author_account"], r.get("audience", "all")

    def authored(self, actor: Actor, m: Membership | None) -> list[LongNote]:
        with self._lock:
            return [n for n in self.notes.values() if n.scope == "household" and n.author_account == actor.account_id
                    and self._visible(actor, n)]

    def erase_account(self, account_id: str, tenant_id: str) -> int:
        """`nokker erase` en mémoire : ce que la migration fait par `tenant_id` et `author_account`."""
        with self._lock:
            gone = [k for k, n in self.notes.items() if n.tenant_id == tenant_id or n.author_account == account_id]
            for k in gone:
                del self.notes[k]
            for pid in [p for p, r in self.pending.items()
                        if r["tenant_id"] == tenant_id or r["author_account"] == account_id]:
                del self.pending[pid]
        return len(gone)

    def drop_household(self, household_id: str, tenant_id: str) -> None:
        with self._lock:
            for k in [k for k, n in self.notes.items() if n.household_id == household_id and n.tenant_id == tenant_id]:
                del self.notes[k]


_COLS = ("tenant_id, account_id, name, scope, household_id, audience, kind, title, body, author_account, origin, "
         "embed_model, created_at, updated_at")


def _row(r: tuple) -> LongNote:
    (tenant_id, account_id, name, scope, household_id, audience, kind, title, body, author, origin, model, created,
     updated) = r[:14]
    return LongNote(name=name, scope=scope, kind=kind, title=title, text=body, account_id=account_id,
                    tenant_id=tenant_id, author_account=author, household_id=household_id, audience=audience,
                    origin=origin, created_at=float(created), updated_at=float(updated), embed_model=model)


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class PgLongMemoryStore:
    """nokker.ai : migration 049, sous RLS. Une connexion courte par opération : `app.tenant_id` = locataire courant
    (`nokker/store/conn.py::connect`), puis `app.account_id` = le compte qui agit (les politiques s'y ouvrent). Chaque
    requête restreint AUSSI au compte ou au foyer (défense en profondeur : la RLS reste la garantie)."""

    def __init__(self, dsn: str, *, migrate_now: bool = True) -> None:
        if not dsn:
            raise ValueError("PgLongMemoryStore requiert un DSN Postgres")
        self.dsn = dsn
        self._vector: tuple[float, bool] | None = None
        if migrate_now:
            from pathlib import Path

            from nokker.store.conn import migrate

            mig = Path(__file__).parent.parent / "store" / "migrations" / "049_tenant_long_memory.sql"
            migrate(dsn, mig.read_text(encoding="utf-8"))

    def _conn(self, actor: Actor):
        from nokker.store.conn import connect

        if actor.tenant_id != current_tenant():
            raise LongMemoryError(403, "forbidden", "compte hors du locataire courant")
        conn = connect(self.dsn, autocommit=True)
        try:
            conn.execute("SELECT set_config('app.account_id', %s, false)", (actor.account_id,))
        except BaseException:
            conn.close()
            raise
        return conn

    @property
    def vector(self) -> bool:
        """La colonne `embedding` existe (extension pgvector posée) ; relu toutes les 5 minutes."""
        now = time.monotonic()
        if self._vector is not None and now - self._vector[0] < 300:
            return self._vector[1]
        from nokker.store.conn import connect_admin

        try:
            with connect_admin(self.dsn) as c:
                ok = c.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = "
                               "to_regclass('nokker_long_memory') AND attname = 'embedding' AND NOT attisdropped)"
                               ).fetchone()[0]
        except Exception:
            log.warning("mémoire longue : présence de pgvector illisible", exc_info=True)
            ok = False
        self._vector = (now, bool(ok))
        return bool(ok)

    def put(self, actor: Actor, n: LongNote, vec: list[float] | None, model: str | None) -> None:
        vector = self.vector and vec is not None
        cols = _COLS + (", embedding" if vector else "")
        vals = [n.tenant_id, n.account_id, n.name, n.scope, n.household_id, n.audience, n.kind, n.title, n.text,
                n.author_account, n.origin, model if vector else None, n.created_at, n.updated_at]
        marks = ", ".join(["%s"] * len(vals)) + (", %s::vector" if vector else "")
        if vector:
            vals.append(_vec_literal(vec))
        upd = ("title = EXCLUDED.title, body = EXCLUDED.body, kind = EXCLUDED.kind, audience = EXCLUDED.audience, "
               "origin = EXCLUDED.origin, embed_model = EXCLUDED.embed_model, updated_at = EXCLUDED.updated_at"
               + (", embedding = EXCLUDED.embedding" if vector else ""))
        with self._conn(actor) as c:
            c.execute(f"INSERT INTO nokker_long_memory ({cols}) VALUES ({marks}) "
                      f"ON CONFLICT (tenant_id, account_id, name) DO UPDATE SET {upd}", vals)

    def _where(self, actor: Actor, scope: str, m: Membership | None) -> tuple[str, list[Any]]:
        if scope == "user":
            return "scope = 'user' AND tenant_id = %s AND account_id = %s", [actor.tenant_id, actor.account_id]
        if m is None:
            return "false", []
        return "scope = 'household' AND tenant_id = %s AND household_id = %s", [m.tenant_id, m.household_id]

    def candidates(self, actor: Actor, scope: str, m: Membership | None, qvec: list[float] | None, model: str | None,
                   limit: int) -> list[tuple[LongNote, float | None]]:
        where, params = self._where(actor, scope, m)
        if qvec is not None and self.vector:
            sel = (f"SELECT {_COLS}, CASE WHEN embedding IS NOT NULL AND embed_model = %s "
                   f"THEN 1 - (embedding <=> %s::vector) END AS cos FROM nokker_long_memory WHERE {where} "
                   "ORDER BY cos DESC NULLS LAST, updated_at DESC LIMIT %s")
            args = [model, _vec_literal(qvec), *params, int(limit)]
        else:
            sel = f"SELECT {_COLS}, NULL AS cos FROM nokker_long_memory WHERE {where} ORDER BY updated_at DESC LIMIT %s"
            args = [*params, int(limit)]
        with self._conn(actor) as c:
            rows = c.execute(sel, args).fetchall()
        return [(_row(r), float(r[14]) if r[14] is not None else None) for r in rows]

    def find(self, actor: Actor, name: str, m: Membership | None) -> LongNote | None:
        with self._conn(actor) as c:
            r = c.execute(f"SELECT {_COLS} FROM nokker_long_memory WHERE name = %s AND ("
                          "(scope = 'user' AND tenant_id = %s AND account_id = %s) OR "
                          "(scope = 'household' AND tenant_id = %s AND household_id = %s)) LIMIT 1",
                          (name, actor.tenant_id, actor.account_id, m.tenant_id if m else None,
                           m.household_id if m else None)).fetchone()
        return _row(r) if r else None

    def delete(self, actor: Actor, n: LongNote) -> bool:
        with self._conn(actor) as c:
            return c.execute("DELETE FROM nokker_long_memory WHERE tenant_id = %s AND account_id = %s AND name = %s",
                             (n.tenant_id, n.account_id, n.name)).rowcount > 0

    def set_audience(self, actor: Actor, n: LongNote, audience: str) -> bool:
        with self._conn(actor) as c:
            return c.execute("UPDATE nokker_long_memory SET audience = %s, updated_at = %s WHERE tenant_id = %s "
                             "AND account_id = %s AND name = %s AND scope = 'household'",
                             (audience, time.time(), n.tenant_id, n.account_id, n.name)).rowcount > 0

    def submit(self, actor: Actor, m: Membership, row: dict[str, Any]) -> None:
        # Dans le locataire du FOYER, au nom du payeur : seule la politique `long_memory_household_submit` le permet
        # (membre actif, à son nom d'auteur). Pas de RETURNING : le membre ne relit pas la proposition.
        from psycopg.types.json import Jsonb

        with self._conn(actor) as c:
            c.execute("INSERT INTO nokker_memory_pending (tenant_id, account_id, id, source, kind, title, body, scope, "
                      "derived_from, project, links, thread_id, turn_index, at, status, author_account, audience) "
                      "VALUES (%s, %s, %s, 'household', %s, %s, %s, 'household', 'user', %s, %s, '', NULL, %s, "
                      "'pending', %s, %s)",
                      (m.tenant_id, m.payer_account, row["id"], row["kind"], row["title"], row["text"],
                       m.household_id, Jsonb([]), row["at"], actor.account_id, row.get("audience", "all")))

    def pending_meta(self, actor: Actor, pending_id: str) -> tuple[str, str] | None:
        with self._conn(actor) as c:
            r = c.execute("SELECT author_account, audience FROM nokker_memory_pending WHERE tenant_id = %s AND "
                          "account_id = %s AND id = %s", (actor.tenant_id, actor.account_id, pending_id)).fetchone()
        return (r[0], r[1]) if r and r[0] else None

    def authored(self, actor: Actor, m: Membership | None) -> list[LongNote]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_COLS} FROM nokker_long_memory WHERE scope = 'household' "
                             "AND author_account = %s ORDER BY created_at", (actor.account_id,)).fetchall()
        return [_row(r) for r in rows]


# ─────────────────────────────── service ───────────────────────────────


def household_membership(house: Any) -> Callable[[str], Membership | None]:
    """Adhésion ACTIVE d'après le service du foyer (`nokker/household/service.py`) : un profil mineur suspendu, un
    ancien membre, un foyer illisible → None (comme le rappel des notes du foyer)."""

    def membership(account_id: str) -> Membership | None:
        found = house.membership(account_id)
        if found is None:
            return None
        _acc, p, row = found
        if row.status != "active":
            return None
        if row.minor:
            st = house.minor_state(account_id)
            if st is None or not st.active:
                return None
        return Membership(household_id=p.id, tenant_id=p.tenant_id, payer_account=p.owner_account,
                          profile=row.profile)

    return membership


@dataclass
class LongMemory:
    store: Any  # MemoryLongStore | PgLongMemoryStore
    membership: Callable[[str], Membership | None] = lambda _a: None
    account_for_tenant: Callable[[str], Any] | None = None
    embedder: Any = None  # HashEmbedder | CorthexisEmbedder | None (lexical)
    household_recall: Any = None  # nokker.household.memory.HouseholdRecall : notes du foyer écrites dans l'app
    settings: Settings = field(default_factory=Settings)
    scrub: Callable[[str], str] | None = None
    review: bool = False  # quarantaine allumée (`memory_pending`) : un membre peut proposer au foyer
    now_fn: Callable[[], float] = time.time
    _last_warn: float = field(default=0.0, init=False, repr=False)

    # ── comptes ──
    def actor(self, account_id: str) -> Actor:
        if not account_id or not account_id.startswith("us_"):
            raise LongMemoryError(403, "no_account", "mémoire longue : compte requis")
        tid = current_tenant()
        if tid == INTERNAL_TENANT:
            raise LongMemoryError(403, "internal", "le locataire interne a sa propre mémoire longue")
        return Actor(account_id=account_id, tenant_id=tid)

    def _member(self, account_id: str) -> Membership | None:
        try:
            return self.membership(account_id)
        except Exception:
            log.warning("mémoire longue : adhésion au foyer illisible", exc_info=True)
            return None

    def _warn(self, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_warn >= 60.0:
            self._last_warn = now
            log.warning("mémoire longue : %s — rappel lexical", msg, exc_info=True)  # noqa: LOG014 — appelé dans un except

    def _model(self) -> str | None:
        if self.embedder is None:
            return None
        try:
            return self.embedder.identity()
        except Exception:  # noqa: BLE001
            self._warn("moteur d'embedding indisponible")
            return None

    def _embed_doc(self, title: str, text: str) -> tuple[list[float] | None, str | None]:
        model = self._model()
        if model is None:
            return None, None
        try:
            return self.embedder.embed_docs([f"{title}\n{text}"])[0], model
        except Exception:  # noqa: BLE001
            self._warn("plongement d'une note impossible")
            return None, None

    def _embed_query(self, query: str) -> tuple[list[float] | None, str | None]:
        model = self._model()
        if model is None:
            return None, None
        try:
            return self.embedder.embed_query(query), model
        except Exception:  # noqa: BLE001
            self._warn("plongement de la requête impossible")
            return None, None

    # ── écriture ──
    def _clean(self, kind: str, title: str, text: str, audience: str, scope: str) -> tuple[str, str]:
        title, text = " ".join(str(title or "").split()), str(text or "").strip()
        if kind not in KINDS or scope not in SCOPES or audience not in AUDIENCES:
            raise LongMemoryError(400, "bad_note", f"kind ∈ {KINDS}, scope ∈ {SCOPES}, audience ∈ {AUDIENCES}")
        if scope == "user" and audience != "all":
            raise LongMemoryError(400, "bad_note", "audience : seulement pour une note du foyer")
        if not title or not text or len(title) > MAX_TITLE or len(text) > MAX_TEXT:
            raise LongMemoryError(400, "bad_note", f"title (≤ {MAX_TITLE} car.) et text (≤ {MAX_TEXT} car.) requis")
        if self.scrub is not None:
            title, text = self.scrub(title), self.scrub(text)
        return title, text

    def write(self, account_id: str, *, scope: str, kind: str = "fact", title: str, text: str, audience: str = "all",
              origin: str = "app") -> dict[str, Any]:
        """Geste du compte (route) ou fait approuvé : `{note}` écrite, ou `{pending}` (proposition d'un membre au
        foyer, relue par le payeur)."""
        actor = self.actor(account_id)
        title, text = self._clean(kind, title, text, audience, scope)
        m = None
        if scope == "household":
            m = self._member(account_id)
            if m is None:
                raise LongMemoryError(404, "no_household", "aucun foyer actif pour ce compte")
            if m.payer_account != account_id:
                return {"pending": self._submit(actor, m, kind, title, text, audience)}
        for n, _ in self.store.candidates(actor, scope, m, None, None, self.settings.max_candidates):
            if n.kind == kind and _norm(n.text) == _norm(text):
                return {"note": n.public(account_id), "duplicate": True}
        return {"note": self._put(actor, scope, m, kind, title, text, audience, account_id, origin).public(account_id)}

    def _put(self, actor: Actor, scope: str, m: Membership | None, kind: str, title: str, text: str, audience: str,
             author: str, origin: str) -> LongNote:
        now = float(self.now_fn())
        if scope == "household" and (m is None or m.tenant_id != actor.tenant_id or m.payer_account != actor.account_id):
            raise LongMemoryError(403, "payer_only", "seul le payeur écrit dans la mémoire du foyer")
        n = LongNote(name=note_name(kind, title, text, now), scope=scope, kind=kind, title=title, text=text,
                     account_id=actor.account_id, tenant_id=actor.tenant_id, author_account=author,
                     household_id=m.household_id if scope == "household" and m else None,
                     audience=audience if scope == "household" else "all", origin=origin, created_at=now,
                     updated_at=now)
        vec, model = self._embed_doc(title, text)
        self.store.put(actor, n, vec, model)
        return n

    def _submit(self, actor: Actor, m: Membership, kind: str, title: str, text: str, audience: str) -> dict[str, Any]:
        if not self.review:
            raise LongMemoryError(409, "review_off", "proposer au foyer demande la relecture du payeur "
                                                    "(memory_pending éteint)")
        import uuid
        from datetime import UTC, datetime

        pid = f"mp_{uuid.uuid4().hex[:16]}"
        self.store.submit(actor, m, {"id": pid, "kind": kind, "title": title, "text": text, "audience": audience,
                                     "at": datetime.now(UTC).isoformat(timespec="seconds")})
        return {"id": pid, "status": "pending", "scope": "household"}

    def commit_pending(self, note: Any) -> str | None:
        """Approbation par le payeur d'une proposition `source = household` (PendingMemory.decide, dans la session du
        payeur) : la note entre au foyer, au nom de son auteur. Rend son nom."""
        acc = getattr(note, "decided_by", None) or ""
        if not acc:
            tid = current_tenant()
            a = self.account_for_tenant(tid) if self.account_for_tenant else None
            acc = getattr(a, "account_id", "") if a is not None else ""
        actor = self.actor(acc)
        m = self._member(acc)
        if m is None or m.payer_account != acc or m.household_id != note.project:
            raise LongMemoryError(409, "not_payer", "seul le payeur du foyer approuve une note du foyer")
        meta = self.store.pending_meta(actor, note.id)
        author, audience = meta if meta else (acc, "all")
        title, text = self._clean(note.kind, note.title, note.text, audience, "household")
        return self._put(actor, "household", m, note.kind, title, text, audience, author, f"pending:{note.id}").name

    def delete(self, account_id: str, name: str) -> bool:
        actor = self.actor(account_id)
        m = self._member(account_id)
        n = self.store.find(actor, name, m)
        if n is None:
            raise LongMemoryError(404, "unknown_note", "note inconnue")
        if not self.store.delete(actor, n):
            raise LongMemoryError(403, "forbidden", "une note du foyer se retire par le payeur ou par son auteur")
        return True

    def set_audience(self, account_id: str, name: str, audience: str) -> dict[str, Any]:
        if audience not in AUDIENCES:
            raise LongMemoryError(400, "bad_note", f"audience ∈ {AUDIENCES}")
        actor = self.actor(account_id)
        m = self._member(account_id)
        n = self.store.find(actor, name, m)
        if n is None or n.scope != "household":
            raise LongMemoryError(404, "unknown_note", "note du foyer inconnue")
        if m is None or m.payer_account != account_id or not self.store.set_audience(actor, n, audience):
            raise LongMemoryError(403, "payer_only", "réservé au payeur du foyer")
        n.audience = audience
        return n.public(account_id)

    # ── lecture ──
    def listing(self, account_id: str, scope: str | None = None) -> dict[str, Any]:
        actor = self.actor(account_id)
        m = self._member(account_id)
        notes: list[LongNote] = []
        for s in ([scope] if scope else list(SCOPES)):
            notes += [n for n, _ in self.store.candidates(actor, s, m, None, None, self.settings.max_candidates)]
        hh = None if m is None else {"id": m.household_id, "payer": m.payer_account == account_id,
                                     "profile": m.profile}
        return {"household": hh, "notes": [n.public(account_id) for n in notes]}

    def export(self, account_id: str) -> dict[str, Any]:
        """Données de la personne : sa mémoire de profil et ses contributions au foyer qu'elle voit encore."""
        actor = self.actor(account_id)
        m = self._member(account_id)
        user = [n.public(account_id) for n, _ in self.store.candidates(actor, "user", None, None, None, 100000)]
        mine = [n.public(account_id) for n in self.store.authored(actor, m)]
        return {"user": user, "household_authored": mine}

    def get_note(self, account_id: str, name: str) -> LongNote | None:
        actor = self.actor(account_id)
        return self.store.find(actor, name.removeprefix(HOUSEHOLD_PREFIX), self._member(account_id))

    def _scored(self, pairs: list[tuple[LongNote, float | None]], query: str) -> list[tuple[LongNote, float]]:
        from nokker.household.memory import rank
        from nokker.memory.corthexis import named

        if not pairs:
            return []
        notes = [n for n, _ in pairs]
        lex = {id(n): s for n, s in rank(notes, query, len(notes), min_score=0.0)}
        w = self.settings.vector_weight
        out: list[tuple[LongNote, float, bool]] = []
        for n, cos in pairs:
            lx = lex.get(id(n), 0.0)
            if cos is not None:
                score, floor = w * max(cos, 0.0) + (1 - w) * lx, self.settings.min_score
            else:
                score, floor = lx, self.settings.lexical_min_score
            cited = named(n.name, query)
            if cited or score >= floor:
                out.append((n, round(min(1.0, score), 4), cited))
        if not out:
            return []
        best = max(s for _, s, _ in out)
        return sorted(((n, s) for n, s, cited in out if cited or s >= self.settings.relative_floor * best),
                      key=lambda p: (-p[1], -p[0].updated_at))

    def _search(self, account_id: str, query: str, limit: int) -> tuple[list[tuple[LongNote, float]],
                                                                         list[tuple[LongNote, float]], Membership | None]:
        actor = self.actor(account_id)
        m = self._member(account_id)
        qvec, model = self._embed_query(query)
        n_cand = self.settings.max_candidates
        user = self._scored(self.store.candidates(actor, "user", None, qvec, model, n_cand), query)
        hh = self._scored(self.store.candidates(actor, "household", m, qvec, model, n_cand), query) if m else []
        return user[:limit], hh[:limit], m

    def recall(self, account_id: str, query: str, k: int = 5) -> list[Recall]:
        """`[Rappel mémoire]` du tour : profil + foyer, quotas par portée, places libres à l'autre portée."""
        if not query.strip():
            return []
        try:
            user, hh, _m = self._search(account_id, query, k)
        except LongMemoryError:
            return []
        except Exception:  # noqa: BLE001 — la mémoire en panne ne casse jamais un tour
            self._warn("recherche en échec")
            return []
        u = [Recall(note=n.name, text=user_line(n), score=s) for n, s in user]
        h = [Recall(note=HOUSEHOLD_PREFIX + n.name, text=HOUSEHOLD_LINE.format(title=n.title, text=n.text), score=s)
             for n, s in hh]
        if self.household_recall is not None:  # notes du foyer écrites dans l'app (team_notes), rappel lexical
            try:
                h += self.household_recall.recall_for_account(account_id, query, k)
            except Exception:
                log.warning("notes du foyer illisibles", exc_info=True)
        return merge_quotas({"user": u, "household": h},
                            {"user": self.settings.user_k, "household": self.settings.household_k}, k)

    def search(self, account_id: str, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        """`memory_search` d'un testeur : sans seuil de rappel, mais seulement ce que le compte voit."""
        user, hh, _ = self._search(account_id, query, max(1, min(int(top_k), 20)))
        rows = [{"note_name": n.name, "scope": "user", "title": n.title, "score": s, "snippet": n.text[:300]}
                for n, s in user]
        rows += [{"note_name": HOUSEHOLD_PREFIX + n.name, "scope": "household", "title": n.title, "score": s,
                  "snippet": n.text[:300]} for n, s in hh]
        return sorted(rows, key=lambda r: -r["score"])[:top_k]

    # ── noyau ──
    def for_tenant(self, tenant_id: str) -> LongMemoryPort | None:
        """Port mémoire d'un locataire testeur (`TenantMemory.tenant_memory`) : celui de son compte."""
        if tenant_id == INTERNAL_TENANT or self.account_for_tenant is None:
            return None
        acc = self.account_for_tenant(tenant_id)
        account_id = getattr(acc, "account_id", None) if acc is not None else None
        if not account_id or getattr(acc, "status", "active") != "active":
            return None
        return LongMemoryPort(self, account_id)


def merge_quotas(buckets: dict[str, list[Recall]], quotas: dict[str, int], k: int) -> list[Recall]:
    """Chaque portée garde ses `quotas` meilleures notes ; les places restantes (jusqu'à `k`) vont aux meilleures
    notes restantes, toutes portées confondues. Une note n'entre qu'une fois (meilleur score)."""
    seen: dict[str, Recall] = {}
    ordered = {b: sorted(rs, key=lambda r: -r.score) for b, rs in buckets.items()}
    for rs in ordered.values():
        for r in rs:
            if r.note not in seen or r.score > seen[r.note].score:
                seen[r.note] = r
    ordered = {b: [r for r in rs if seen.get(r.note) is r] for b, rs in ordered.items()}
    picked: list[Recall] = []
    for b, rs in ordered.items():
        picked += rs[:max(0, quotas.get(b, 0))]
    picked = sorted(picked, key=lambda r: -r.score)[:k]
    rest = sorted((r for rs in ordered.values() for r in rs if r not in picked), key=lambda r: -r.score)
    picked += rest[:max(0, k - len(picked))]
    return sorted(picked, key=lambda r: -r.score)


@dataclass
class LongMemoryPort:
    """`MemoryPort` du tour d'un testeur : le rappel de SA mémoire (profil + foyer). `propose` (faits approuvés de la
    consolidation, docs/LEARNING.md §3) entre dans sa mémoire de profil. Aucun outil ici : ils sont dans le registre
    du locataire (`LongMemoryTools`, réglage `tools`)."""

    service: LongMemory
    account_id: str

    def recall(self, query: str, k: int = 5) -> list[Recall]:
        return self.service.recall(self.account_id, query, k)

    def recall_for(self, thread_id: str, query: str, k: int = 5) -> list[Recall]:
        return self.recall(query, k)

    def propose(self, thread_id: str, facts: list[str]) -> None:
        for fact in facts:
            text = " ".join(str(fact or "").split())[:MAX_TEXT]
            if not text:
                continue
            title = text if len(text) <= MAX_TITLE else text[:MAX_TITLE - 3].rstrip() + "…"
            try:
                self.service.write(self.account_id, scope="user", kind="fact", title=title, text=text,
                                   origin=f"propose:{thread_id}")
            except Exception:
                log.warning("fait proposé non écrit (fil %s)", thread_id, exc_info=True)

    def specs(self) -> list[ToolSpec]:
        return []

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)


class LongMemoryTools:
    """`ToolSource` des testeurs (réglage `tools`, éteint par défaut) : `memory_search` et `memory_get`, AVEC LES
    TEXTES EXISTANTS de la mémoire longue interne (`nokker/memory/corthexis.py::_tool_specs`), sur ce que le compte
    voit seulement."""

    def __init__(self, service: LongMemory) -> None:
        from nokker.memory.corthexis import _tool_specs

        self.service = service
        self._specs = _tool_specs("tenant:long-memory")

    def available_for(self, tenant_id: str) -> bool:
        return self.service.settings.tools and self.service.for_tenant(tenant_id) is not None

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        import json

        port = self.service.for_tenant(current_tenant())
        if port is None:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        try:
            if name == "memory_search":
                rows = self.service.search(port.account_id, str(arguments.get("query") or ""),
                                           int(arguments.get("top_k") or 8))
                return ToolResult(call_id="", name=name, content=json.dumps({"result": rows}, ensure_ascii=False))
            if name == "memory_get":
                nm = str(arguments.get("note_name") or "")
                n = self.service.get_note(port.account_id, nm)
                if n is None:
                    return ToolResult(call_id="", name=name, content=f"note introuvable : {nm}", is_error=True)
                return ToolResult(call_id="", name=name, content=f"# {n.title}\n\n{n.text}")
        except LongMemoryError as e:
            return ToolResult(call_id="", name=name, content=e.message, is_error=True)
        return ToolResult(call_id="", name=name, content=f"outil mémoire inconnu : {name}", is_error=True)


__all__ = [
    "AUDIENCES",
    "SCOPES",
    "Actor",
    "CorthexisEmbedder",
    "HashEmbedder",
    "LongMemory",
    "LongMemoryError",
    "LongMemoryPort",
    "LongMemoryTools",
    "LongNote",
    "Membership",
    "MemoryLongStore",
    "PgLongMemoryStore",
    "Settings",
    "household_membership",
    "merge_quotas",
]
