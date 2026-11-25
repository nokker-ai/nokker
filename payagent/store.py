"""Stockage de l'agent de paiement : moyens enregistrés, jetons émis (Stripe), cartes émises et défis 3-D Secure
(émetteur `wise`), événements déjà vus.

Tables `pay_agent_*` (migrations nokker/store/migrations/063_pay_agent.sql et 064_pay_agent_wise.sql) ; le rôle de l'agent y écrit, le rôle du
noyau n'y a aucun droit (docs/PAY-AGENT.md §6). Clés des passkeys : table `devices`, en LECTURE SEULE (`Devices`).
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class Method:
    account: str
    payment_method: str          # pm_… préparée par le Payment Element (aucun numéro de carte chez nous)
    kind: str                    # card, link…
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    device_id: str               # appareil dont la passkey a signé l'enregistrement
    created_at: float
    issuer: str = "stripe_spt"   # stripe_spt (pm_…) | wise (payment_method = « wise:<profileId> »)
    holder_name: str | None = None  # wise : nom du titulaire des cartes émises (`cardHolderName`)

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "brand": self.brand, "last4": self.last4, "exp_month": self.exp_month,
                "exp_year": self.exp_year, "created_at": self.created_at, "issuer": self.issuer}


@dataclass(frozen=True)
class Token:
    id: str                      # tk_… (le nôtre)
    mandate_id: str
    mandate_sha256: str
    account: str
    spt_id: str
    status: str                  # active | requires_action | deactivated
    seller_profile: str
    currency: str
    max_amount_minor: int
    expires_at: int
    created_at: float
    updated_at: float
    used: bool = False
    deactivated_reason: str | None = None
    kernel_ref: str = ""         # rendu tel quel dans les rappels au noyau ; aucune décision n'en dépend

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("account", "kernel_ref", "spt_id"):  # le numéro du SPT ne sort pas de l'agent
            d.pop(k)
        return d


@dataclass(frozen=True)
class Link:
    """Compte émetteur ouvert PAR L'AGENT pour un compte Nokker (émetteur `wise`) : seul profil que ce compte peut
    enregistrer comme moyen de paiement (le noyau ni le web ne choisissent un identifiant de profil)."""

    account: str
    profile_ref: str             # wise:<profileId>
    review_id: str | None        # revue KYC en cours chez Wise
    state: str                   # NEW | WAITING_CUSTOMER_INPUT | PROCESSING | PASSED | PASSED_WITH_REQUIREMENTS…
    created_at: float
    updated_at: float
    required_by: str | None = None

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "required_by": self.required_by, "updated_at": self.updated_at}


@dataclass(frozen=True)
class Card:
    """Carte virtuelle émise pour UN mandat (émetteur `wise`). Ni PAN, ni CVV, ni date : références seulement."""

    id: str                      # cd_… (le nôtre)
    mandate_id: str
    mandate_sha256: str
    account: str
    issuer: str
    profile_ref: str             # wise:<profileId>
    card_ref: str                # cardToken chez l'émetteur
    vault_ref: str               # carte chez le coffre
    merchant_host: str
    currency: str
    max_amount_minor: int
    expires_at: int
    status: str                  # active | requires_action | deactivated (même vocabulaire que les jetons)
    created_at: float
    updated_at: float
    used: bool = False
    deactivated_reason: str | None = None
    kernel_ref: str = ""
    vault_sealed: dict[str, str] = field(default_factory=dict)  # chiffrés du coffre (ev:encrypted…), illisibles ici

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": "card", "mandate_id": self.mandate_id, "mandate_sha256": self.mandate_sha256,
                "status": self.status, "merchant_host": self.merchant_host, "currency": self.currency,
                "max_amount_minor": self.max_amount_minor, "expires_at": self.expires_at,
                "created_at": self.created_at, "updated_at": self.updated_at, "used": self.used,
                "deactivated_reason": self.deactivated_reason}


@dataclass(frozen=True)
class ThreeDS:
    """Défi 3-D Secure délégué par l'émetteur pour une carte : détails tels que reçus, décision de la personne."""

    id: str                      # 3ds_… (le nôtre ; c'est le `challenge_id` montré au web)
    card_id: str
    mandate_id: str
    account: str
    challenge_ref: str           # Wise : transaction.reference
    profile_ref: str
    amount_minor: int
    currency: str
    merchant_name: str
    merchant_url: str | None
    merchant_country: str | None
    merchant_match: str          # host (URL du défi = hôte du mandat) | unverified (pas d'URL dans le défi)
    expires_at: float
    status: str                  # pending | approved | rejected | expired | failed
    created_at: float
    decided_at: float | None = None
    device_id: str | None = None  # appareil dont la passkey a confirmé


@dataclass(frozen=True)
class DeviceKey:
    id: str
    user: str
    public_key_spki: str
    status: str


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.methods: dict[str, Method] = {}
        self.tokens: dict[str, Token] = {}
        self.cards: dict[str, Card] = {}
        self.links: dict[str, Link] = {}
        self.threeds: dict[str, ThreeDS] = {}
        self.events: set[str] = set()

    def put_method(self, m: Method) -> None:
        with self._lock:
            self.methods[m.account] = m

    def get_method(self, account: str) -> Method | None:
        with self._lock:
            return self.methods.get(account)

    def delete_method(self, account: str) -> bool:
        with self._lock:
            return self.methods.pop(account, None) is not None

    def insert_token(self, t: Token) -> bool:
        """Faux si un jeton existe déjà pour ce mandat (un seul jeton par mandat)."""
        with self._lock:
            if any(x.mandate_id == t.mandate_id for x in self.tokens.values()):
                return False
            self.tokens[t.id] = t
            return True

    def update_token(self, t: Token) -> None:
        with self._lock:
            self.tokens[t.id] = t

    def token_by_mandate(self, mandate_id: str) -> Token | None:
        with self._lock:
            return next((t for t in self.tokens.values() if t.mandate_id == mandate_id), None)

    def token_by_spt(self, spt_id: str) -> Token | None:
        with self._lock:
            return next((t for t in self.tokens.values() if t.spt_id == spt_id), None)

    def live_tokens(self, account: str) -> list[Token]:
        with self._lock:
            return [t for t in self.tokens.values() if t.account == account and t.status != "deactivated"]

    def first_event(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self.events:
                return False
            self.events.add(event_id)
            return True

    # comptes émetteur (wise)
    def put_link(self, link: Link) -> None:
        with self._lock:
            self.links[link.account] = link

    def get_link(self, account: str) -> Link | None:
        with self._lock:
            return self.links.get(account)

    def link_by_profile(self, profile_ref: str) -> Link | None:
        with self._lock:
            return next((x for x in self.links.values() if x.profile_ref == profile_ref), None)

    # cartes (wise)
    def insert_card(self, c: Card) -> bool:
        with self._lock:
            if any(x.mandate_id == c.mandate_id for x in self.cards.values()):
                return False
            self.cards[c.id] = c
            return True

    def update_card(self, c: Card) -> None:
        with self._lock:
            self.cards[c.id] = c

    def card_by_mandate(self, mandate_id: str) -> Card | None:
        with self._lock:
            return next((c for c in self.cards.values() if c.mandate_id == mandate_id), None)

    def card_by_ref(self, card_ref: str) -> Card | None:
        with self._lock:
            return next((c for c in self.cards.values() if c.card_ref == card_ref), None)

    def live_cards(self, account: str | None = None) -> list[Card]:
        with self._lock:
            return [c for c in self.cards.values()
                    if c.status != "deactivated" and (account is None or c.account == account)]

    def put_threeds(self, t: ThreeDS) -> bool:
        """Faux si ce défi (même référence d'émetteur) est déjà connu (webhook redélivré)."""
        with self._lock:
            if any(x.challenge_ref == t.challenge_ref and x.card_id == t.card_id for x in self.threeds.values()):
                return False
            self.threeds[t.id] = t
            return True

    def update_threeds(self, t: ThreeDS) -> None:
        with self._lock:
            self.threeds[t.id] = t

    def pending_threeds(self, card_id: str) -> ThreeDS | None:
        with self._lock:
            return next((t for t in sorted(self.threeds.values(), key=lambda x: -x.created_at)
                         if t.card_id == card_id and t.status == "pending"), None)


class MemoryDevices:
    def __init__(self) -> None:
        self.keys: dict[str, DeviceKey] = {}

    def get(self, device_id: str) -> DeviceKey | None:
        return self.keys.get(device_id)


class PgStore:  # pragma: no cover - couvert par tests/integration/test_pay_agent_pg.py
    _TOKEN_COLS = ("id, mandate_id, mandate_sha256, account, spt_id, status, seller_profile, currency, "
                   "max_amount_minor, expires_at, created_at, updated_at, used, deactivated_reason, kernel_ref")

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.Lock()

    def _one(self, sql: str, args: tuple) -> tuple | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()

    def _all(self, sql: str, args: tuple) -> list[tuple]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def _exec(self, sql: str, args: tuple) -> int:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.rowcount

    def put_method(self, m: Method) -> None:
        self._exec("INSERT INTO pay_agent_methods (account, payment_method, kind, brand, last4, exp_month, exp_year, "
                   "device_id, created_at, issuer, holder_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                   "ON CONFLICT (account) DO UPDATE SET "
                   "payment_method=EXCLUDED.payment_method, kind=EXCLUDED.kind, brand=EXCLUDED.brand, "
                   "last4=EXCLUDED.last4, exp_month=EXCLUDED.exp_month, exp_year=EXCLUDED.exp_year, "
                   "device_id=EXCLUDED.device_id, created_at=EXCLUDED.created_at, issuer=EXCLUDED.issuer, "
                   "holder_name=EXCLUDED.holder_name",
                   (m.account, m.payment_method, m.kind, m.brand, m.last4, m.exp_month, m.exp_year, m.device_id,
                    m.created_at, m.issuer, m.holder_name))

    def get_method(self, account: str) -> Method | None:
        row = self._one("SELECT account, payment_method, kind, brand, last4, exp_month, exp_year, device_id, created_at, "
                        "issuer, holder_name FROM pay_agent_methods WHERE account = %s", (account,))
        return Method(*row) if row else None

    def delete_method(self, account: str) -> bool:
        return self._exec("DELETE FROM pay_agent_methods WHERE account = %s", (account,)) > 0

    def insert_token(self, t: Token) -> bool:
        return self._exec(f"INSERT INTO pay_agent_tokens ({self._TOKEN_COLS}) VALUES "
                          "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (mandate_id) DO NOTHING",
                          tuple(asdict(t).values())) > 0

    def update_token(self, t: Token) -> None:
        self._exec("UPDATE pay_agent_tokens SET status=%s, used=%s, deactivated_reason=%s, updated_at=%s WHERE id=%s",
                   (t.status, t.used, t.deactivated_reason, t.updated_at, t.id))

    def token_by_mandate(self, mandate_id: str) -> Token | None:
        row = self._one(f"SELECT {self._TOKEN_COLS} FROM pay_agent_tokens WHERE mandate_id = %s", (mandate_id,))
        return Token(*row) if row else None

    def token_by_spt(self, spt_id: str) -> Token | None:
        row = self._one(f"SELECT {self._TOKEN_COLS} FROM pay_agent_tokens WHERE spt_id = %s", (spt_id,))
        return Token(*row) if row else None

    def live_tokens(self, account: str) -> list[Token]:
        return [Token(*r) for r in self._all(f"SELECT {self._TOKEN_COLS} FROM pay_agent_tokens WHERE account = %s "
                                              "AND status <> 'deactivated'", (account,))]

    def first_event(self, event_id: str) -> bool:
        return self._exec("INSERT INTO pay_agent_events (id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,)) > 0

    # comptes émetteur (wise)
    _LINK_COLS = "account, profile_ref, review_id, state, created_at, updated_at, required_by"

    def put_link(self, link: Link) -> None:
        self._exec(f"INSERT INTO pay_agent_links ({self._LINK_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                   "ON CONFLICT (account) DO UPDATE SET profile_ref=EXCLUDED.profile_ref, review_id=EXCLUDED.review_id, "
                   "state=EXCLUDED.state, updated_at=EXCLUDED.updated_at, required_by=EXCLUDED.required_by",
                   tuple(asdict(link).values()))

    def get_link(self, account: str) -> Link | None:
        row = self._one(f"SELECT {self._LINK_COLS} FROM pay_agent_links WHERE account = %s", (account,))
        return Link(*row) if row else None

    def link_by_profile(self, profile_ref: str) -> Link | None:
        row = self._one(f"SELECT {self._LINK_COLS} FROM pay_agent_links WHERE profile_ref = %s", (profile_ref,))
        return Link(*row) if row else None

    # cartes (wise)
    _CARD_COLS = ("id, mandate_id, mandate_sha256, account, issuer, profile_ref, card_ref, vault_ref, merchant_host, "
                  "currency, max_amount_minor, expires_at, status, created_at, updated_at, used, deactivated_reason, "
                  "kernel_ref, vault_sealed")
    _3DS_COLS = ("id, card_id, mandate_id, account, challenge_ref, profile_ref, amount_minor, currency, merchant_name, "
                 "merchant_url, merchant_country, merchant_match, expires_at, status, created_at, decided_at, device_id")

    @staticmethod
    def _card(row: tuple | None) -> Card | None:
        if not row:
            return None
        sealed = row[-1]
        return Card(*row[:-1], vault_sealed=sealed if isinstance(sealed, dict) else json.loads(sealed or "{}"))

    def insert_card(self, c: Card) -> bool:
        vals = list(asdict(c).values())
        vals[-1] = json.dumps(c.vault_sealed)
        return self._exec(f"INSERT INTO pay_agent_cards ({self._CARD_COLS}) VALUES ({','.join(['%s'] * 19)}) "
                          "ON CONFLICT (mandate_id) DO NOTHING", tuple(vals)) > 0

    def update_card(self, c: Card) -> None:
        self._exec("UPDATE pay_agent_cards SET status=%s, used=%s, deactivated_reason=%s, updated_at=%s, "
                   "vault_sealed=%s WHERE id=%s",
                   (c.status, c.used, c.deactivated_reason, c.updated_at, json.dumps(c.vault_sealed), c.id))

    def card_by_mandate(self, mandate_id: str) -> Card | None:
        return self._card(self._one(f"SELECT {self._CARD_COLS} FROM pay_agent_cards WHERE mandate_id = %s",
                                    (mandate_id,)))

    def card_by_ref(self, card_ref: str) -> Card | None:
        return self._card(self._one(f"SELECT {self._CARD_COLS} FROM pay_agent_cards WHERE card_ref = %s", (card_ref,)))

    def live_cards(self, account: str | None = None) -> list[Card]:
        if account is None:
            rows = self._all(f"SELECT {self._CARD_COLS} FROM pay_agent_cards WHERE status <> 'deactivated'", ())
        else:
            rows = self._all(f"SELECT {self._CARD_COLS} FROM pay_agent_cards WHERE account = %s "
                             "AND status <> 'deactivated'", (account,))
        return [c for c in (self._card(r) for r in rows) if c is not None]

    def put_threeds(self, t: ThreeDS) -> bool:
        return self._exec(f"INSERT INTO pay_agent_3ds ({self._3DS_COLS}) VALUES ({','.join(['%s'] * 17)}) "
                          "ON CONFLICT (card_id, challenge_ref) DO NOTHING", tuple(asdict(t).values())) > 0

    def update_threeds(self, t: ThreeDS) -> None:
        self._exec("UPDATE pay_agent_3ds SET status=%s, decided_at=%s, device_id=%s WHERE id=%s",
                   (t.status, t.decided_at, t.device_id, t.id))

    def pending_threeds(self, card_id: str) -> ThreeDS | None:
        row = self._one(f"SELECT {self._3DS_COLS} FROM pay_agent_3ds WHERE card_id = %s AND status = 'pending' "
                        "ORDER BY created_at DESC LIMIT 1", (card_id,))
        return ThreeDS(*row) if row else None


class PgDevices:  # pragma: no cover
    """Clés des passkeys, lues avec un rôle en LECTURE SEULE sur `devices` (le noyau ne les fournit pas)."""

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.Lock()

    def get(self, device_id: str) -> DeviceKey | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute('SELECT id, "user", public_key_spki, status FROM devices WHERE id = %s', (device_id,))
            row = cur.fetchone()
        return DeviceKey(*row) if row else None


__all__ = ["Card", "Link", "ThreeDS", "DeviceKey", "MemoryDevices", "MemoryStore", "Method", "PgDevices", "PgStore", "Token", "replace"]
