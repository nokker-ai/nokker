"""Pages par la connexion du téléphone (P11, docs/LOKKAL-PHONE.md §4.5). Contrat de front :
packages/sdk/src/egress.ts et packages/mock-server/src/egress.ts.

Opération `fetch.get` de la file du nœud (P7a), capacité `egress`, COUPÉE par défaut. Deux déclenchements seulement :
- (a) liste d'hôtes de la personne (`PUT /v1/me/egress/hosts`, ≤ 50, migration 079) : pendant un tour qu'elle a ouvert,
  un `web_fetch` vers un hôte de la liste passe par son téléphone (`trigger: allowlist`), jamais par le cloud ;
- (b) un GESTE : un `web_fetch` refusé (403, 451, CAPTCHA, blocage) pose une OFFRE sur le message, si la personne a un
  téléphone `egress` allumé et en ligne. `POST /v1/threads/{id}/phone-fetch {url, call_id}` crée un droit SIGNÉ
  (`phone_fetch_grant`, usage unique, 10 min) et un tour de la personne (`origin: phone_fetch`). Seul ce droit met un
  `fetch.get` explicite en file.
Le modèle ne choisit JAMAIS le téléphone : aucun champ dans le schéma de `web_fetch`, aucune phrase qui le lui
propose, aucun repli automatique. Jamais pour un agent, une tâche planifiée, une suite de carte. 30 fetch par heure et
par nœud. Textes vus par le modèle : `TEXTS`, figés par leur empreinte (`texts_sha256`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

EGRESS_MAX_HOSTS = 50
FETCH_PER_HOUR = 30
GRANT_TTL_S = 600
OFFER_TTL_S = 600
FETCH_WAIT_S = 12.0
URL_MAX = 2000
_BANNED_SUFFIX = re.compile(r"(^|\.)(local|lan|home\.arpa|internal|localhost|intranet|corp|home)$")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# ── textes vus par le modèle : relus et tranchés avant fusion (06.11.2026), MOT POUR MOT ; empreinte figée par un
# test. `<url>` et `<host>` passent par inline(). L'en-tête est un texte du noyau, HORS clôture (comme « Page <url>
# (<n> caractères) : » de web_fetch) ; la page va dans le bloc `web:page` de la clôture commune, titre en tête.
TEXTS = {
    "phone_fetch_input": "Ouvre cette page par mon téléphone : {url}",
    "phone_page_head": ("Page {url} ouverte par le téléphone de la personne, à sa demande (statut {status}, {n} caractères ; "
                        "vue depuis son téléphone : la langue, la région ou la session peuvent différer d'une lecture "
                        "depuis le cloud) :"),
    "phone_fetch_failed": ("phone_fetch_failed : la page « {host} » n'a pas pu être ouverte par le téléphone ({reason}) ; "
                           "rien n'a été lu."),
}
# <raison> : une PHRASE de cette table fermée, jamais le code brut (le code reste dans les données du client).
REASONS = {
    "blocked_address": "adresse refusée (réseau local ou privé)",
    "dns_failed": "nom de domaine introuvable",
    "tls_failed": "connexion sécurisée refusée",
    "timeout": "délai dépassé",
    "too_large": "page trop lourde (plus de 768 Kio)",
    "unsupported_type": "type de contenu non lisible",
    "too_many_redirects": "trop de redirections",
    "redirect_not_allowed": "redirection refusée",
    "scheme_not_allowed": "adresse qui n'est ni http ni https",
    "port_not_allowed": "port refusé",
    "host_not_allowed": "site hors de la liste autorisée sur le téléphone",
    "egress_off": "accès web du téléphone coupé dans l'app",
    "metered_cap": "plafond de données mobiles atteint",
    "constrained": "mode économie de données actif sur le téléphone",
    "no_network": "téléphone sans réseau",
    "rate": "trop de pages ouvertes par le téléphone, réessayer plus tard",
    "grant_invalid": "autorisation expirée, la personne doit toucher à nouveau « ouvrir par le téléphone »",
    "phone_timeout": "le téléphone n'a pas répondu (app fermée ou hors ligne)",
    "low_battery": "batterie du téléphone trop faible (sous 20 %, hors charge)",
    "cellular_off": ("téléphone en données mobiles, non autorisées pour ouvrir des pages "
                     "(réglage « Aussi en données mobiles » dans l'app Nokker)"),
}
UNKNOWN_REASON = "raison non précisée"
ENGINE = "phone"  # moteur porté par un document de web_fetch ouvert par le téléphone (mode liste)


def texts_sha256() -> str:
    """Empreinte des textes vus par le modèle : en-tête, échec, message du geste, table des raisons, moteur."""
    blob = json.dumps({"TEXTS": TEXTS, "REASONS": REASONS, "UNKNOWN_REASON": UNKNOWN_REASON, "ENGINE": ENGINE},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reason_text(code: Any) -> str:
    """La phrase d'un code ; un code hors table : « raison non précisée » et un avertissement au journal."""
    if code in REASONS:
        return REASONS[code]
    import logging

    logging.getLogger("nokker.cloud.egress").warning("phone_fetch : code de refus inconnu %r", str(code)[:40])
    return UNKNOWN_REASON


def _inline(v: str, n: int) -> str:
    from nokker.qa.untrusted import inline

    return " ".join(inline(v).split())[:n]


# ── règles d'hôte (mêmes que l'appareil, packages/sdk/src/egress.ts) ──
def normalize_host(raw: Any) -> str | None:
    """`exemple.ch` (exact) ou `*.exemple.ch` (ses sous-domaines) ; minuscules, ≥ 2 étiquettes, ni IP, ni joker nu, ni
    suffixe local. La règle normalisée, ou None."""
    if not isinstance(raw, str):
        return None
    h = raw.strip().lower().rstrip(".")
    wild = h.startswith("*.")
    name = h[2:] if wild else h
    if not name or len(name) > 253 or "*" in name or _IPV4.match(name) or ":" in name or "/" in name:
        return None
    labels = name.split(".")
    if len(labels) < 2 or any(not _LABEL.match(lab) for lab in labels) or _BANNED_SUFFIX.search(name):
        return None
    return f"*.{name}" if wild else name


def host_allowed(host: str, rules: list[str]) -> bool:
    h = host.lower().rstrip(".")
    return any(h.endswith(r[1:]) and len(h) > len(r) - 1 if r.startswith("*.") else h == r for r in rules)


def url_check(url: Any) -> tuple[str | None, str | None]:
    """(hôte, None) pour une URL https:443 acceptable, sinon (None, code de refus) : même règle que l'appareil."""
    try:
        u = urlsplit(str(url or ""))
    except ValueError:
        return None, "scheme_not_allowed"
    if u.scheme != "https":
        return None, "scheme_not_allowed"
    try:
        port = u.port
    except ValueError:
        return None, "port_not_allowed"
    if port not in (None, 443):
        return None, "port_not_allowed"
    if u.username or u.password:
        return None, "host_not_allowed"
    host = (u.hostname or "").lower()
    if not host or host.startswith("[") or _IPV4.match(host) or normalize_host(host) is None:
        return None, "host_not_allowed"
    return host, None


def refusal_reason(error: str) -> str | None:
    """Raison d'une offre d'après l'erreur d'un `web_fetch` du cloud : http_403 | http_451 | captcha | blocked."""
    e = str(error or "").lower()
    if re.search(r"\b451\b", e):
        return "http_451"
    if re.search(r"\b403\b", e):
        return "http_403"
    if "captcha" in e or "challenge" in e:
        return "captcha"
    if "blocked" in e or "access denied" in e or "forbidden" in e:
        return "blocked"
    return None


def person_turn(ctx: Any) -> bool:
    """Tour OUVERT PAR LA PERSONNE : un compte, pas d'origine du noyau (suite de carte…), pas un agent ni le noyau."""
    turn = getattr(ctx, "turn", None)
    sender = str(getattr(ctx, "sender", "") or "")
    origin = getattr(getattr(turn, "input", None), "origin", "") if turn is not None else "x"
    return (turn is not None and sender.startswith("us_") and origin in ("", "phone_fetch")
            and getattr(ctx, "channel", "") not in ("agent", "kernel", "relay", "schedule"))


# ── liste d'hôtes (079) ──
@dataclass
class MemoryEgressStore:
    rows: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def hosts(self, account: str) -> list[str]:
        from nokker.tenancy.context import current_tenant

        with self.lock:
            return list(self.rows.get((current_tenant(), account), []))

    def put_hosts(self, account: str, hosts: list[str]) -> list[str]:
        from nokker.tenancy.context import current_tenant

        with self.lock:
            self.rows[(current_tenant(), account)] = list(hosts)
            return list(hosts)


class PgEgressStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _c(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)

    def hosts(self, account: str) -> list[str]:
        with self._c() as c:
            r = c.execute("SELECT hosts FROM member_egress_hosts WHERE account_id = %s", (account,)).fetchone()
        return list(r["hosts"] or []) if r else []

    def put_hosts(self, account: str, hosts: list[str]) -> list[str]:
        with self._c() as c:
            c.execute("INSERT INTO member_egress_hosts (account_id, hosts) VALUES (%s, %s) ON CONFLICT (account_id) "
                      "DO UPDATE SET hosts = EXCLUDED.hosts, updated_at = now()", (account, list(hosts)))
        return list(hosts)


class EgressError(Exception):
    def __init__(self, status: int, code: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra or {}


@dataclass
class EgressService:
    phone: Any  # nokker.cloud.phone.PhoneService
    store: Any  # MemoryEgressStore | PgEgressStore
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))  # signature des droits
    clock: Callable[[], float] = time.time
    # Offres et droits : 10 min, EN MÉMOIRE du processus (un réplica, comme le reste du chemin d'un tour) ; une offre
    # perdue (redémarrage) se redemande en relançant la page.
    _offers: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _grants: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _by_turn: dict[tuple[str, int], dict[str, list[dict[str, Any]]]] = field(default_factory=dict, init=False)
    _fetched: dict[str, deque] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ── liste ──
    def get_hosts(self, account: str) -> dict[str, Any]:
        return {"hosts": self.store.hosts(account)}

    def put_hosts(self, account: str, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or set(body) != {"hosts"} or not isinstance(body["hosts"], list):
            raise EgressError(400, "bad_hosts")
        if len(body["hosts"]) > EGRESS_MAX_HOSTS:
            raise EgressError(400, "too_many_hosts")
        out: list[str] = []
        for i, raw in enumerate(body["hosts"]):
            h = normalize_host(raw)
            if h is None:
                raise EgressError(400, "bad_host", {"index": i})
            if h not in out:
                out.append(h)
        return {"hosts": self.store.put_hosts(account, out)}

    # ── téléphone ──
    def _node(self, account: str) -> Any:
        nodes = self.phone.online_nodes(account, "egress") if self.phone is not None else []
        return nodes[0] if nodes else None

    def _rate(self, machine_id: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._fetched.setdefault(machine_id, deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= FETCH_PER_HOUR:
                return False
            q.append(now)
            return True

    def _run(self, account: str, args: dict[str, Any], thread_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """`fetch.get` par la file, attendu 12 s : (phone_fetched, résultat FetchGetResult | None)."""
        host = (urlsplit(args["url"]).hostname or "").lower()
        node = self._node(account)
        if node is None:
            return {"host": host, "ok": False, "error": "egress_off"}, None
        if not self._rate(node.machine_id):
            return {"host": host, "ok": False, "error": "rate"}, None
        t = self.phone.enqueue(account, node, "fetch.get", args, deadline_s=60,
                               origin={"kind": "turn", "thread_id": thread_id})
        done = self.phone.wait(t.task_id, FETCH_WAIT_S)
        if done is None:
            self.phone.cancel(t.task_id)
            return {"host": host, "ok": False, "error": "phone_timeout"}, None
        if done.status == "failed":
            code = str((done.error or {}).get("code") or "")
            return {"host": host, "ok": False, "error": code or "no_network"}, None  # code brut : données du client
        try:
            r = json.loads(done.result or "{}")
            status = int(r.get("status") or 0)
        except (ValueError, TypeError):
            return {"host": host, "ok": False, "error": "unsupported_type"}, None
        return {"host": host, "ok": 200 <= status < 400, "status": status}, r

    # ── (a) mode liste, dans web_fetch ──
    def allowlist_doc(self, ctx: Any, url: str) -> dict[str, Any] | None:
        """Document web_fetch ouvert par le téléphone si l'hôte est dans la liste de la personne, que le tour est le
        sien et qu'un téléphone `egress` est en ligne ; None sinon (web_fetch du cloud, inchangé). Un échec du
        téléphone rend un document en erreur, JAMAIS un repli sur le cloud."""
        if not person_turn(ctx):
            return None
        account = str(ctx.sender)
        host, bad = url_check(url)
        if bad or not host:
            return None
        rules = self.store.hosts(account)
        if not host_allowed(host, rules) or self._node(account) is None:
            return None
        turn_id = f"{ctx.thread_id}:{ctx.turn_index}"
        fetched, r = self._run(account, {"url": url, "trigger": "allowlist", "turn_id": turn_id, "allow_hosts": rules},
                               ctx.thread_id)
        self._note(ctx.thread_id, ctx.turn_index, "phone_fetched", fetched)
        if r is None or not fetched["ok"]:  # la phrase de la table, jamais un repli sur le cloud
            err = reason_text(fetched["error"]) if fetched.get("error") else f"statut {fetched.get('status')}"
            return {"url": url, "title": "", "content": "", "engine": ENGINE, "error": err}
        return {"url": url, "title": str(r.get("title") or ""), "content": str(r.get("text") or ""), "engine": ENGINE}

    # ── (b) offre sur un refus, puis le geste ──
    def offer(self, ctx: Any, url: str, error: str) -> dict[str, Any] | None:
        """Offre posée sur le message d'un web_fetch du cloud refusé, si la personne a un téléphone `egress` en ligne."""
        reason = refusal_reason(error)
        if reason is None or not person_turn(ctx):
            return None
        host, bad = url_check(url)
        if bad or not host or self._node(str(ctx.sender)) is None:
            return None
        o = {"call_id": "call_" + secrets.token_hex(6), "url": url, "host": host, "reason": reason}
        with self._lock:
            self._offers[o["call_id"]] = {**o, "thread": ctx.thread_id, "user": str(ctx.sender), "used": False,
                                          "exp": self.clock() + OFFER_TTL_S}
        self._note(ctx.thread_id, ctx.turn_index, "phone_fetch_offers", o)
        return o

    def _note(self, thread_id: str, index: int, key: str, item: dict[str, Any]) -> None:
        with self._lock:
            self._by_turn.setdefault((thread_id, index), {}).setdefault(key, []).append(dict(item))

    def message_fields(self, thread_id: str, index: int) -> dict[str, Any]:
        """Champs du message assistant d'un tour (`phone_fetch_offers`, `phone_fetched`), pour le client."""
        with self._lock:
            return {k: list(v) for k, v in self._by_turn.get((thread_id, index), {}).items()}

    def _sign(self, g: dict[str, Any]) -> str:
        msg = f"{g['id']}\n{g['url']}\n{g['turn_id']}\n{int(g['exp'])}".encode()
        return base64.urlsafe_b64encode(hmac.new(self.secret, msg, "sha256").digest()).decode().rstrip("=")

    def claim(self, account: str, thread_id: str, body: Any, turn_index: int) -> dict[str, Any]:
        """`POST /v1/threads/{id}/phone-fetch` : l'offre (même fil, même url, pas encore prise) → droit signé (10 min,
        usage unique). 404 no_offer, 410 offer_used, 409 no_phone_egress."""
        b = body if isinstance(body, dict) else {}
        with self._lock:
            o = self._offers.get(str(b.get("call_id") or ""))
            if o is None or o["user"] != account or o["thread"] != thread_id or o["url"] != b.get("url") \
                    or self.clock() > o["exp"]:
                raise EgressError(404, "no_offer")
            if o["used"]:
                raise EgressError(410, "offer_used")
        if self._node(account) is None:
            raise EgressError(409, "no_phone_egress")
        with self._lock:
            if o["used"]:
                raise EgressError(410, "offer_used")
            o["used"] = True
            g = {"id": "pfg_" + secrets.token_hex(8), "user": account, "thread": thread_id, "url": o["url"],
                 "turn_id": f"{thread_id}:{turn_index}", "exp": self.clock() + GRANT_TTL_S, "used": False}
            g["sig"] = self._sign(g)
            self._grants[g["id"]] = g
        return g

    def explicit_fetch(self, grant: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Le `fetch.get` explicite, seulement avec un droit valable (signature, échéance, usage unique, même url)."""
        host = (urlsplit(grant["url"]).hostname or "").lower()
        with self._lock:
            g = self._grants.get(grant["id"])
            ok = g is not None and not g["used"] and self.clock() < g["exp"] and hmac.compare_digest(
                g["sig"], self._sign(g)) and g["url"] == grant["url"]
            if g is not None:
                g["used"] = True
        if not ok:
            return {"host": host, "ok": False, "error": "grant_invalid"}, None
        return self._run(grant["user"], {"url": grant["url"], "trigger": "explicit", "turn_id": grant["turn_id"],
                                         "grant_id": grant["id"]}, grant["thread"])

    # ── textes du tour du geste ──
    @staticmethod
    def turn_text(url: str) -> str:
        return TEXTS["phone_fetch_input"].format(url=_inline(url, URL_MAX))

    @staticmethod
    def attached(url: str, fetched: dict[str, Any], result: dict[str, Any] | None) -> str:
        """Bloc joint au tour du geste : l'en-tête du noyau HORS clôture puis la page dans `web:page` (titre en tête),
        ou l'échec « code : texte » avec la phrase de la table."""
        from nokker.tools.web_fence import _Out

        if result is None or not fetched["ok"]:
            reason = reason_text(fetched["error"]) if fetched.get("error") else f"statut {int(fetched.get('status') or 0)}"
            return TEXTS["phone_fetch_failed"].format(host=_inline(fetched["host"], 253), reason=reason)
        title, text = str(result.get("title") or "").strip(), str(result.get("text") or "")
        out = _Out()
        out.lines.append(TEXTS["phone_page_head"].format(url=_inline(url, URL_MAX), status=int(fetched.get("status") or 0),
                                                         n=len(text)))
        out.fence("web:page", "\n\n".join(x for x in (title, text) if x))
        return out.render()


_service: EgressService | None = None


def configure(svc: EgressService | None) -> None:
    global _service
    _service = svc


def service() -> EgressService | None:
    return _service


__all__ = ["configure", "service", "EGRESS_MAX_HOSTS", "ENGINE", "FETCH_PER_HOUR", "REASONS", "TEXTS", "UNKNOWN_REASON", "reason_text", "EgressError", "EgressService",
           "MemoryEgressStore", "PgEgressStore", "host_allowed", "normalize_host", "person_turn", "refusal_reason",
           "texts_sha256", "url_check"]
