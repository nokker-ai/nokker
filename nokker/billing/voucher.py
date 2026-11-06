"""Vouchers d'essai d'un mois (docs/VOUCHERS.md).

Le voucher EST l'essai de Nokker : sans code, pas d'essai. Avec un code valide, le Checkout Stripe enregistre la carte
(`payment_method_collection=always`) et pose `trial_end` au même jour du mois suivant, 23:59 heure de Zurich : premier
débit à cette date. Toutes les formules, en mensuel ; jamais cumulable avec le coupon bêta.

Règles (refus en 400 `code_*`, aucune écriture) : code inconnu ou désactivé (`code_invalid`), date limite passée
(`code_expired`), `max_uses` atteint (`code_exhausted`), compte ou adresse qui a déjà eu un essai, toutes campagnes
(`code_used`), locataire qui a déjà eu un abonnement, même annulé (`code_customer`).

Les tables (067) sont HORS RLS : le locataire et l'adresse n'y sont que des empreintes HMAC salées. Un code unique ne
part pas deux fois : l'usage est RÉSERVÉ à la création du Checkout (verrou sur la ligne du code), libéré à l'expiration
de la session.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

ERRORS = ("code_invalid", "code_expired", "code_exhausted", "code_used", "code_customer")
STATES = ("reserved", "trialing", "converted", "canceled", "expired")
RESERVE_S = 1800  # = durée de vie de la session Checkout (`expires_at`, minimum Stripe : 30 min)
# Code utilisé comme INVITATION (`voucher_grants_invite`) : l'usage est réservé AVANT la création du compte, au nom de
# l'adresse (`tenant_key` = PENDING + empreinte de l'adresse), pour la durée de validité de l'invitation ; le premier
# Checkout de ce compte avec ce code reprend la réservation (rien n'est compté deux fois, un code épuisé ne crée pas de
# compte orphelin).
PENDING = "pending:"
INVITE_HOLD_S = 7 * 86400
TZ = "Europe/Zurich"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O ni 1/I : un code se lit et se recopie
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")
_CAMPAIGN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
REMINDERS = (("reminded_23", 7), ("reminded_28", 2))  # J+23 et J+28 = 7 et 2 jours avant le premier débit
MAX_BATCH = 5000


class VoucherError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def norm_code(code: str) -> str:
    """Majuscules, sans espaces ; '' si la forme n'est pas celle d'un code."""
    c = re.sub(r"\s+", "", str(code or "")).upper()
    return c if _CODE_RE.match(c) else ""


def norm_email(email: str) -> str:
    """Minuscules, sans `+étiquette` : a+x@b.ch et A@B.ch sont la même personne."""
    local, _, domain = str(email or "").strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}" if domain else local


def new_single_code() -> str:
    """`NK-XXXX-XXXX` (32^8 ≈ 10^12 : ne se devine pas sous la limite de vérifications)."""
    pick = lambda: "".join(secrets.choice(ALPHABET) for _ in range(4))  # noqa: E731
    return f"NK-{pick()}-{pick()}"


def trial_end_for(now: float, tz: str = TZ) -> datetime:
    """Même jour le mois suivant, 23:59:59 à `tz` (31 janvier → 28/29 février) : « date + 1 mois »."""
    local = datetime.fromtimestamp(now, ZoneInfo(tz))
    y, m = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    d = min(local.day, calendar.monthrange(y, m)[1])
    return datetime(y, m, d, 23, 59, 59, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def end_of_day(day: str, tz: str = TZ) -> datetime:
    """`YYYY-MM-DD` → 23:59:59 ce jour-là à `tz`, en UTC (date limite d'utilisation d'un code)."""
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, 23, 59, 59, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


# ── stockage ──────────────────────────────────────────────────────────────────────────────────────────────────────
class VoucherStore(Protocol):
    def insert_codes(self, rows: list[dict[str, Any]]) -> None:
        """Lève `VoucherError("code_exists", 409)` si un code existe déjà (rien n'est écrit)."""
        ...

    def get(self, code: str) -> dict[str, Any] | None: ...

    def check(self, code: str, tenant_key: str, email_hash: str, now: datetime) -> str | None:
        """Raison du refus (hors `code_customer`, jugé par l'appelant) ou None."""
        ...

    def reserve(self, code: str, tenant_key: str, email_hash: str, plan: str, now: datetime, until: datetime,
                trial_end: datetime) -> tuple[int | None, str | None, bool]:
        """Contrôle ET réservation, atomiques (verrou sur le code) : (id de l'usage, None, held) ou (None, raison,
        False) ; `held` = une réservation de cette adresse pour ce code existait déjà et a été reprise."""
        ...

    def update(self, use_id: int, **fields: Any) -> None: ...

    def by_session(self, session: str) -> dict[str, Any] | None: ...

    def by_subscription(self, subscription: str) -> dict[str, Any] | None: ...

    def expire_reservations(self, now: datetime) -> int: ...

    def report(self) -> list[dict[str, Any]]: ...

    def codes(self, campaign: str, free_only: bool) -> list[str]: ...

    def disable(self, code: str, at: datetime) -> bool: ...

    def trialing(self) -> list[dict[str, Any]]: ...


def _counts_live(u: dict[str, Any], now: datetime) -> bool:
    """Un usage compte (unicité, `max_uses`) s'il n'est pas expiré, ni une réservation périmée."""
    if u["state"] == "expired":
        return False
    return not (u["state"] == "reserved" and u["reserved_until"] and u["reserved_until"] < now)


def _pending(uses: Iterable[dict[str, Any]], code: str, email_hash: str, now: datetime) -> dict[str, Any] | None:
    """Réservation d'invitation vivante de cette adresse pour CE code (reprise au premier Checkout)."""
    return next((u for u in uses if _counts_live(u, now) and u["state"] == "reserved" and u["code"] == code
                 and u["email_hash"] == email_hash and str(u["tenant_key"]).startswith(PENDING)), None)


def _refusal(v: dict[str, Any] | None, uses: Iterable[dict[str, Any]], tenant_key: str, email_hash: str,
             now: datetime) -> str | None:
    if v is None or v.get("disabled_at"):
        return "code_invalid"
    if v["expires_at"] < now:
        return "code_expired"
    uses = list(uses)
    if v and _pending(uses, v["code"], email_hash, now) is not None:
        return None  # déjà réservé pour cette personne à l'inscription : c'est le même usage
    live = [u for u in uses if _counts_live(u, now)]
    if any(u["tenant_key"] == tenant_key or u["email_hash"] == email_hash for u in live):
        return "code_used"
    if v["max_uses"] is not None and sum(u["code"] == v["code"] for u in live) >= v["max_uses"]:
        return "code_exhausted"
    return None


@dataclass
class MemoryVouchers:
    vouchers: dict[str, dict[str, Any]] = field(default_factory=dict)
    uses: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def insert_codes(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            if any(r["code"] in self.vouchers for r in rows) or len({r["code"] for r in rows}) != len(rows):
                raise VoucherError("code_exists", 409)
            for r in rows:
                self.vouchers[r["code"]] = {"disabled_at": None, **r}

    def get(self, code: str) -> dict[str, Any] | None:
        return self.vouchers.get(code)

    def check(self, code: str, tenant_key: str, email_hash: str, now: datetime) -> str | None:
        with self._lock:
            return _refusal(self.vouchers.get(code), self.uses, tenant_key, email_hash, now)

    def reserve(self, code, tenant_key, email_hash, plan, now, until, trial_end):
        with self._lock:
            for u in self.uses:  # réservations périmées : libérées (comme l'index partiel côté Postgres)
                if not _counts_live(u, now) and u["state"] == "reserved":
                    u["state"] = "expired"
            held = _pending(self.uses, code, email_hash, now)
            if held is not None:  # même adresse, même code : invitation redemandée, ou premier Checkout du compte
                held.update(tenant_key=tenant_key, reserved_until=until, plan=plan or held["plan"],
                            trial_end=trial_end or held["trial_end"])
                return held["id"], None, True
            why = _refusal(self.vouchers.get(code), self.uses, tenant_key, email_hash, now)
            if why:
                return None, why, False
            uid = len(self.uses) + 1
            self.uses.append({"id": uid, "code": code, "tenant_key": tenant_key, "email_hash": email_hash, "plan": plan,
                              "state": "reserved", "checkout_session": None, "reserved_until": until,
                              "stripe_subscription": None, "stripe_customer": None, "trial_end": trial_end,
                              "card_fingerprint": None, "reminded_23": None, "reminded_28": None, "created_at": now})
            return uid, None, False

    def update(self, use_id: int, **fields: Any) -> None:
        with self._lock:
            self.uses[use_id - 1].update(fields)

    def by_session(self, session: str) -> dict[str, Any] | None:
        return next((dict(u) for u in self.uses if session and u["checkout_session"] == session), None)

    def by_subscription(self, subscription: str) -> dict[str, Any] | None:
        return next((dict(u) for u in self.uses if subscription and u["stripe_subscription"] == subscription), None)

    def expire_reservations(self, now: datetime) -> int:
        with self._lock:
            stale = [u for u in self.uses if u["state"] == "reserved" and u["reserved_until"] < now]
            for u in stale:
                u["state"] = "expired"
            return len(stale)

    def report(self) -> list[dict[str, Any]]:
        out = []
        for v in self.vouchers.values():
            states = [u["state"] for u in self.uses if u["code"] == v["code"]]
            out.append({"campaign": v["campaign"], "code": v["code"], "kind": v["kind"], "max_uses": v["max_uses"],
                        "expires_at": v["expires_at"], "disabled": bool(v["disabled_at"]),
                        **{s: states.count(s) for s in STATES}})
        return out

    def codes(self, campaign: str, free_only: bool) -> list[str]:
        used = {u["code"] for u in self.uses if u["state"] != "expired"}
        return sorted(c for c, v in self.vouchers.items()
                      if v["campaign"] == campaign and not v["disabled_at"] and not (free_only and c in used))

    def disable(self, code: str, at: datetime) -> bool:
        v = self.vouchers.get(code)
        if v is None:
            return False
        v["disabled_at"] = v["disabled_at"] or at
        return True

    def trialing(self) -> list[dict[str, Any]]:
        return [dict(u) for u in self.uses if u["state"] == "trialing"]


class PgVouchers:
    """Tables hors RLS (067) : connexion d'administration, comme `billing_events`."""

    COLS = ("id, code, tenant_key, email_hash, plan, state, checkout_session, reserved_until, stripe_subscription, "
            "stripe_customer, trial_end, card_fingerprint, reminded_23, reminded_28, created_at")

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self, autocommit: bool = True):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        return connect_admin(self.dsn, row_factory=dict_row, autocommit=autocommit)

    def insert_codes(self, rows: list[dict[str, Any]]) -> None:
        import psycopg

        try:
            with self._conn(autocommit=False) as conn, conn.transaction():
                for r in rows:
                    conn.execute("INSERT INTO vouchers (code, campaign, kind, max_uses, expires_at, note, created_by) "
                                 "VALUES (%s,%s,%s,%s,%s,%s,%s)", (r["code"], r["campaign"], r["kind"], r["max_uses"],
                                                                   r["expires_at"], r["note"], r["created_by"]))
        except psycopg.errors.UniqueViolation:
            raise VoucherError("code_exists", 409) from None

    def get(self, code: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM vouchers WHERE code = %s", (code,)).fetchone()

    def _uses_for(self, conn, code: str, tenant_key: str, email_hash: str) -> list[dict[str, Any]]:
        return conn.execute(f"SELECT {self.COLS} FROM voucher_uses WHERE state <> 'expired' AND "
                            "(code = %s OR tenant_key = %s OR email_hash = %s)",
                            (code, tenant_key, email_hash)).fetchall()

    def check(self, code: str, tenant_key: str, email_hash: str, now: datetime) -> str | None:
        with self._conn() as conn:
            v = conn.execute("SELECT * FROM vouchers WHERE code = %s", (code,)).fetchone()
            return _refusal(v, self._uses_for(conn, code, tenant_key, email_hash), tenant_key, email_hash, now)

    def reserve(self, code, tenant_key, email_hash, plan, now, until, trial_end):
        import psycopg

        try:
            with self._conn(autocommit=False) as conn, conn.transaction():
                v = conn.execute("SELECT * FROM vouchers WHERE code = %s FOR UPDATE", (code,)).fetchone()
                conn.execute("UPDATE voucher_uses SET state = 'expired', updated_at = now() WHERE state = 'reserved' "
                             "AND reserved_until < %s AND (code = %s OR tenant_key = %s OR email_hash = %s)",
                             (now, code, tenant_key, email_hash))
                uses = self._uses_for(conn, code, tenant_key, email_hash)
                held = _pending(uses, code, email_hash, now)
                if held is not None:  # même adresse, même code : invitation redemandée, ou premier Checkout du compte
                    conn.execute("UPDATE voucher_uses SET tenant_key = %s, reserved_until = %s, "
                                 "plan = COALESCE(NULLIF(%s, ''), plan), trial_end = COALESCE(%s, trial_end), "
                                 "updated_at = now() WHERE id = %s", (tenant_key, until, plan, trial_end, held["id"]))
                    return int(held["id"]), None, True
                why = _refusal(v, uses, tenant_key, email_hash, now)
                if why:
                    return None, why, False
                row = conn.execute("INSERT INTO voucher_uses (code, tenant_key, email_hash, plan, state, reserved_until, "
                                   "trial_end) VALUES (%s,%s,%s,%s,'reserved',%s,%s) RETURNING id",
                                   (code, tenant_key, email_hash, plan, until, trial_end)).fetchone()
                return int(row["id"]), None, False
        except psycopg.errors.UniqueViolation:  # course entre deux codes différents pour la même personne
            return None, "code_used", False

    _UPDATABLE = frozenset({"state", "checkout_session", "stripe_subscription", "stripe_customer", "trial_end",
                            "card_fingerprint", "reminded_23", "reminded_28", "reserved_until"})

    def update(self, use_id: int, **fields: Any) -> None:
        bad = set(fields) - self._UPDATABLE
        if bad:
            raise ValueError(f"colonnes non modifiables : {sorted(bad)}")
        sets = ", ".join(f"{k} = %s" for k in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE voucher_uses SET {sets}, updated_at = now() WHERE id = %s", (*fields.values(), use_id))

    def by_session(self, session: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            return conn.execute(f"SELECT {self.COLS} FROM voucher_uses WHERE checkout_session = %s", (session,)).fetchone()

    def by_subscription(self, subscription: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            return conn.execute(f"SELECT {self.COLS} FROM voucher_uses WHERE stripe_subscription = %s",
                                (subscription,)).fetchone()

    def expire_reservations(self, now: datetime) -> int:
        with self._conn() as conn:
            return conn.execute("UPDATE voucher_uses SET state = 'expired', updated_at = now() WHERE state = 'reserved' "
                                "AND reserved_until < %s", (now,)).rowcount

    def report(self) -> list[dict[str, Any]]:
        counts = ", ".join(f"count(u.id) FILTER (WHERE u.state = '{s}') AS {s}" for s in STATES)
        with self._conn() as conn:
            return conn.execute(f"SELECT v.campaign, v.code, v.kind, v.max_uses, v.expires_at, "
                                f"v.disabled_at IS NOT NULL AS disabled, {counts} FROM vouchers v "
                                "LEFT JOIN voucher_uses u ON u.code = v.code GROUP BY v.code "
                                "ORDER BY v.campaign, v.code").fetchall()

    def codes(self, campaign: str, free_only: bool) -> list[str]:
        q = ("SELECT code FROM vouchers v WHERE campaign = %s AND disabled_at IS NULL"
             + (" AND NOT EXISTS (SELECT 1 FROM voucher_uses u WHERE u.code = v.code AND u.state <> 'expired')"
                if free_only else "") + " ORDER BY code")
        with self._conn() as conn:
            return [r["code"] for r in conn.execute(q, (campaign,)).fetchall()]

    def disable(self, code: str, at: datetime) -> bool:
        with self._conn() as conn:
            return conn.execute("UPDATE vouchers SET disabled_at = COALESCE(disabled_at, %s) WHERE code = %s",
                                (at, code)).rowcount == 1

    def trialing(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return conn.execute(f"SELECT {self.COLS} FROM voucher_uses WHERE state = 'trialing'").fetchall()


# ── service ───────────────────────────────────────────────────────────────────────────────────────────────────────
class Vouchers:
    """Règles des vouchers. `salt` : secret de config (empreintes) ; le changer rouvre l'essai à tout le monde."""

    def __init__(self, store: VoucherStore, salt: str, *, clock: Callable[[], float], tz: str = TZ,
                 grants_invite: bool = False) -> None:
        if len(salt or "") < 16:
            raise ValueError("sel des vouchers requis (16 caractères au moins)")
        self.store, self._salt, self._clock, self.tz = store, salt.encode(), clock, tz
        # `voucher_grants_invite` : un code valide ouvre aussi la porte d'un compte, comme une invitation (décision
        # produit du 05.11 ; éteint par défaut, allumé en bêta). Seul `invite` le lit.
        self.grants_invite = grants_invite

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    def _h(self, kind: str, value: str) -> str:
        return hmac.new(self._salt, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

    def keys(self, tenant_id: str, email: str) -> tuple[str, str]:
        return self._h("tenant", tenant_id), self._h("email", norm_email(email))

    def check(self, code: str, tenant_id: str, email: str) -> str | None:
        c = norm_code(code)
        if not c:
            return "code_invalid"
        tk, eh = self.keys(tenant_id, email)
        return self.store.check(c, tk, eh, self._now())

    def invite(self, code: str, email: str) -> int:
        return self.invite_hold(code, email)[0]

    def invite_hold(self, code: str, email: str) -> tuple[int, bool]:
        """Code utilisé comme invitation (`voucher_grants_invite`) : refus ou réservation au nom de l'ADRESSE, avant
        tout compte. Rend (id de l'usage, `held`) : `held` = la réservation de cette adresse existait déjà (invitation
        redemandée, dont un lien est peut-être déjà parti : un envoi raté ne doit pas la libérer). Lève
        `VoucherError("code_*")`, ou `not_enabled` si le réglage est éteint."""
        if not self.grants_invite:
            raise VoucherError("not_enabled", 404)
        c = norm_code(code)
        if not c:
            raise VoucherError("code_invalid")
        eh = self._h("email", norm_email(email))
        now = self._now()
        uid, why, held = self.store.reserve(c, PENDING + eh, eh, "", now, now + timedelta(seconds=INVITE_HOLD_S), None)
        if why or uid is None:
            raise VoucherError(why or "code_invalid")
        return uid, held

    def reserve(self, code: str, tenant_id: str, email: str, plan: str) -> tuple[int, datetime]:
        """(id de l'usage, fin d'essai) ; lève `VoucherError(code_*)`."""
        c = norm_code(code)
        if not c:
            raise VoucherError("code_invalid")
        tk, eh = self.keys(tenant_id, email)
        now = self._now()
        trial_end = trial_end_for(self._clock(), self.tz)
        uid, why, _ = self.store.reserve(c, tk, eh, plan, now, now + timedelta(seconds=RESERVE_S), trial_end)
        if why or uid is None:
            raise VoucherError(why or "code_invalid")
        return uid, trial_end

    def release(self, use_id: int) -> None:
        self.store.update(use_id, state="expired")

    def attach_session(self, use_id: int, session: str) -> None:
        self.store.update(use_id, checkout_session=session)

    # ── webhook ──
    def on_checkout(self, obj: dict[str, Any], completed: bool) -> dict[str, Any]:
        use = self.store.by_session(str(obj.get("id") or ""))
        if use is None:
            return {"voucher": "unknown_session"}
        if not completed:
            if use["state"] == "reserved":
                self.store.update(use["id"], state="expired")
            return {"voucher": "expired"}
        if use["state"] in ("reserved", "expired"):  # une réservation expirée de peu mais payée reste un essai
            self.store.update(use["id"], state="trialing", stripe_subscription=str(obj.get("subscription") or ""),
                              stripe_customer=str(obj.get("customer") or ""))
        return {"voucher": "trialing"}

    def on_subscription(self, sub: dict[str, Any], status: str) -> str | None:
        use = self.store.by_subscription(str(sub.get("id") or ""))
        if use is None:
            return None
        if use["state"] == "trialing" and status == "active":
            self.store.update(use["id"], state="converted")
            return "converted"
        if use["state"] == "trialing" and status in ("canceled", "incomplete_expired", "unpaid"):
            self.store.update(use["id"], state="canceled")
            return "canceled"
        return None

    # ── console ──
    def create(self, *, campaign: str, kind: str, expires: str, actor: str, code: str = "", count: int = 1,
               max_uses: int | None = None, note: str = "") -> list[str]:
        if not _CAMPAIGN_RE.match(campaign or ""):
            raise VoucherError("bad_campaign")
        if kind not in ("shared", "single"):
            raise VoucherError("bad_kind")
        try:
            expires_at = end_of_day(expires, self.tz)
        except (ValueError, TypeError):
            raise VoucherError("bad_expires") from None
        if expires_at < self._now():
            raise VoucherError("bad_expires")
        base = {"campaign": campaign, "kind": kind, "expires_at": expires_at, "note": str(note or "")[:200],
                "created_by": actor}
        if kind == "shared":
            c = norm_code(code)
            if not c:
                raise VoucherError("bad_code")
            if max_uses is not None and int(max_uses) < 1:
                raise VoucherError("bad_max_uses")
            rows = [{**base, "code": c, "max_uses": int(max_uses) if max_uses else None}]
        else:
            n = int(count)
            if not 1 <= n <= MAX_BATCH:
                raise VoucherError("bad_count")
            rows = [{**base, "code": new_single_code(), "max_uses": 1} for _ in range(n)]
        self.store.insert_codes(rows)
        return [r["code"] for r in rows]

    def report(self) -> dict[str, Any]:
        rows = self.store.report()
        camps: dict[str, dict[str, Any]] = {}
        for r in rows:
            c = camps.setdefault(r["campaign"], {"campaign": r["campaign"], "codes": 0, **{s: 0 for s in STATES}})
            c["codes"] += 1
            for s in STATES:
                c[s] += int(r[s])
        for c in camps.values():
            started = c["trialing"] + c["converted"] + c["canceled"]
            ended = c["converted"] + c["canceled"]
            c["conversion"] = round(c["converted"] / ended, 3) if ended else None
            c["started"] = started
        return {"campaigns": sorted(camps.values(), key=lambda c: c["campaign"]),
                "codes": [{**r, "expires_at": r["expires_at"].isoformat()} for r in rows if r["kind"] == "shared"]}

    # ── rappels de fin d'essai ──
    def due_reminders(self) -> list[tuple[dict[str, Any], str]]:
        """(usage, colonne) des rappels à envoyer maintenant : J+23 et J+28 (7 et 2 jours avant le premier débit),
        une seule fois chacun ; le plus tardif seulement si les deux sont dus (pas de double mail)."""
        now = self._now()
        out = []
        for u in self.store.trialing():
            end = u["trial_end"]
            if not end or end <= now:
                continue
            due = [col for col, days in REMINDERS if not u[col] and end - timedelta(days=days) <= now]
            if due:
                out.append((u, due[-1]))
        return out

    def mark_reminded(self, use: dict[str, Any], col: str) -> None:
        """Marque `col` et les rappels plus précoces (un rappel sauté ne part plus après un plus tardif)."""
        stamp = self._now()
        names = [c for c, _ in REMINDERS]
        self.store.update(use["id"], **{c: use[c] or stamp for c in names[:names.index(col) + 1]})


__all__ = ["ERRORS", "MemoryVouchers", "PgVouchers", "VoucherError", "Vouchers", "end_of_day", "new_single_code",
           "norm_code", "norm_email", "trial_end_for"]
