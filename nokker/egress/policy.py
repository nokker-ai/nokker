"""Politique d'injection des secrets à la sortie (docs/EGRESS-SECRETS.md) : la partie pure, sans réseau.

Le bac à sable n'écrit que des références `secret:NOM` (la forme de nokker/secrets/refs.py). Le proxy de sortie, qui
seul détient les valeurs d'une session, applique `substitute()` à chaque requête sortante et `Redactor` à chaque
réponse entrante :

- une référence à un secret ACCORDÉ à la session est remplacée par sa valeur seulement si l'hôte de destination est
  dans la liste de CE secret et si la référence est dans un emplacement déclaré pour lui (en-tête, paramètre de
  requête, champ du corps) ; sinon refus net (`Refused`, 403), rien ne part ;
- une référence à un nom NON accordé (autre locataire, nom absent du coffre) dans un en-tête ou dans l'URL : refus net
  (`secret_unknown`) ; dans un corps, elle passe telle quelle (texte ordinaire, aucune valeur n'y est attachée) ;
- toute valeur accordée qui revient dans une réponse (en-têtes ou corps, y compris coupée entre deux morceaux) est
  remise sous forme de référence `secret:NOM`.

Rien ici ne journalise ni ne rend une valeur : les événements portent le nom du secret, l'hôte et l'emplacement.
"""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote_plus

REF_PREFIX = "secret:"
# Même grammaire que `NAME_RE` (nokker/secrets/base.py) : une référence est `secret:` suivi d'un nom valide.
REF_RE = re.compile(r"secret:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})")
# Le proxy n'importe rien du noyau (image légère : bibliothèque standard + cryptography) : grammaire recopiée.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_$-]+(\.[A-Za-z0-9_$-]+)*$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
# En-têtes qui ne portent jamais un secret de l'utilisateur vers l'amont (le proxy les retire ou les réécrit).
HOP_HEADERS = frozenset({"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer",
                         "upgrade", "transfer-encoding", "expect"})
MIN_REDACT_LEN = 4


class Refused(Exception):
    """Requête refusée par la politique : `code` (`secret_unknown`, `host_not_allowed`, `location_not_allowed`),
    `secret` (le NOM concerné, jamais la valeur), message en français destiné au bac à sable."""

    def __init__(self, code: str, secret: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.secret = secret


@dataclass(frozen=True)
class SecretRule:
    """Déclaration d'un secret injectable : son NOM, les hôtes où il peut partir, les emplacements où il est posé.

    `hosts` : noms exacts (`api.example.com`) ou suffixe (`*.example.com`, qui ne couvre pas `example.com` lui-même) ;
    `headers` : noms d'en-têtes (sans casse) ; `query` : paramètres de l'URL ; `fields` : champs du corps JSON (chemin
    pointé `a.b`, un tableau traversé) ou de formulaire (`application/x-www-form-urlencoded`)."""

    name: str
    hosts: tuple[str, ...]
    headers: tuple[str, ...] = ()
    query: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, name: str, d: Mapping[str, Any]) -> SecretRule:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError(f"egress_secrets : nom de secret invalide : {name!r}")
        if not isinstance(d, Mapping):
            raise ValueError(f"egress_secrets.{name} : bloc attendu (hosts, headers, query, fields)")
        unknown = set(d) - {"hosts", "headers", "query", "fields"}
        if unknown:
            raise ValueError(f"egress_secrets.{name} : clés inconnues {sorted(unknown)} (hosts, headers, query, fields)")

        def strings(key: str, rx: re.Pattern[str], lower: bool) -> tuple[str, ...]:
            raw = d.get(key) or []
            if not isinstance(raw, (list, tuple)) or any(not isinstance(x, str) for x in raw):
                raise ValueError(f"egress_secrets.{name}.{key} : liste de chaînes attendue")
            out = tuple(x.strip().lower() if lower else x.strip() for x in raw)
            bad = [x for x in out if not rx.fullmatch(x)]
            if bad:
                raise ValueError(f"egress_secrets.{name}.{key} : valeur invalide {bad[0]!r}")
            return out

        hosts = strings("hosts", _HOST_RE, True)
        if not hosts:
            raise ValueError(f"egress_secrets.{name}.hosts : au moins un hôte (jamais « partout »)")
        rule = cls(name=name, hosts=hosts, headers=strings("headers", _HEADER_RE, True),
                   query=strings("query", _FIELD_RE, False), fields=strings("fields", _FIELD_RE, False))
        if not (rule.headers or rule.query or rule.fields):
            raise ValueError(f"egress_secrets.{name} : au moins un emplacement (headers, query ou fields)")
        if any(h in HOP_HEADERS or h == "host" for h in rule.headers):
            raise ValueError(f"egress_secrets.{name}.headers : en-tête de transport refusé")
        return rule

    def as_dict(self) -> dict[str, Any]:
        return {"hosts": list(self.hosts), "headers": list(self.headers), "query": list(self.query),
                "fields": list(self.fields)}

    def host_allowed(self, host: str) -> bool:
        h = host.strip().lower().rstrip(".")
        for pat in self.hosts:
            if pat.startswith("*."):
                if h.endswith(pat[1:]) and len(h) > len(pat) - 1:
                    return True
            elif h == pat:
                return True
        return False


def parse_rules(raw: Mapping[str, Any] | None) -> tuple[SecretRule, ...]:
    """`{NOM: {hosts, headers, query, fields}}` → règles, triées par nom."""
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise ValueError("egress_secrets.secrets : bloc {NOM: {hosts, headers, query, fields}} attendu")
    return tuple(SecretRule.from_dict(str(n), d) for n, d in sorted(raw.items()))


@dataclass
class Grant:
    """Ce qu'une session a le droit d'injecter : règle + valeur, par nom. Ne vit que dans le proxy (mémoire)."""

    session: str
    tenant: str
    secrets: dict[str, tuple[SecretRule, str]] = field(default_factory=dict)

    def __repr__(self) -> str:  # jamais une valeur
        return f"Grant({self.session!r}, {self.tenant!r}, {sorted(self.secrets)})"

    def names(self) -> list[str]:
        return sorted(self.secrets)


@dataclass
class Request:
    """Une requête HTTP/1.1 vue par le proxy. `body` None = corps non inspecté (trop gros, ou absent et diffusé)."""

    method: str
    target: str  # forme origine : /chemin?requête
    headers: list[tuple[str, str]]
    body: bytes | None = None

    def header(self, name: str) -> str | None:
        n = name.lower()
        return next((v for k, v in self.headers if k.lower() == n), None)


@dataclass(frozen=True)
class Event:
    """Une ligne du journal : jamais de valeur."""

    kind: str  # substitution | refused | redacted
    secret: str
    host: str
    where: str  # header:<nom> | query:<nom> | field:<chemin> | body | url | response | <code de refus>

    def as_dict(self) -> dict[str, str]:
        return {"event": self.kind, "secret": self.secret, "host": self.host, "where": self.where}


def _names(text: str) -> list[str]:
    return [m.group(1) for m in REF_RE.finditer(text)]


def _match_name(raw: str, granted: Mapping[str, Any]) -> str:
    """Le nom accordé visé par une capture (une ponctuation finale `.`/`-` collée à la référence est tolérée)."""
    n = raw
    while n and n not in granted and n[-1] in ".-":
        n = n[:-1]
    return n if n in granted else raw


class _Plan:
    """Collecte les références d'une requête, vérifie la politique, puis substitue."""

    def __init__(self, grant: Grant, host: str) -> None:
        self.grant, self.host = grant, host
        self.events: list[Event] = []
        self.basic: list[tuple[str, str]] = []  # (base64 envoyé, base64 d'origine) : à remettre en référence

    def check(self, raw_name: str, where: str, declared: bool, strict_unknown: bool) -> str | None:
        """Le nom accordé si la référence doit être remplacée ; None si elle passe telle quelle ; `Refused` sinon."""
        name = _match_name(raw_name, self.grant.secrets)
        got = self.grant.secrets.get(name)
        if got is None:
            if strict_unknown:
                raise Refused("secret_unknown", name, f"secret:{name} : secret inconnu pour cette session "
                                                      f"(nom absent de son coffre ou non déclaré)")
            return None
        rule, _ = got
        if not rule.host_allowed(self.host):
            raise Refused("host_not_allowed", name, f"secret:{name} : hôte {self.host} non autorisé pour ce secret "
                                                    f"(autorisés : {', '.join(rule.hosts)})")
        if not declared:
            raise Refused("location_not_allowed", name, f"secret:{name} : emplacement {where} non déclaré pour ce "
                                                        "secret")
        self.events.append(Event("substitution", name, self.host, where))
        return name

def _split_target(target: str) -> tuple[str, str | None]:
    path, sep, query = target.partition("?")
    return path, (query if sep else None)


def _json_paths(obj: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    """(chemin pointé, chaîne) de chaque chaîne d'un document JSON ; les clés comptent aussi (chemin + « # clé »)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            yield p + "#key", str(k)
            yield from _json_paths(v, p)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_paths(v, prefix)
    elif isinstance(obj, str):
        yield prefix, obj


def substitute(grant: Grant, host: str, req: Request) -> tuple[Request, list[Event], list[tuple[bytes, bytes]]]:
    """Applique la politique à `req` pour `host`. Rend (requête réécrite, événements, motifs de caviardage propres à
    cette requête — base64 des en-têtes Basic réécrits). `Refused` si une référence n'a pas le droit de partir."""
    plan = _Plan(grant, host.lower().rstrip("."))
    path, query = _split_target(req.target)
    if REF_RE.search(unquote_plus(path)):
        for n in _names(unquote_plus(path)):
            plan.check(n, "url", False, True)
    if query is not None:
        query = _sub_pairs_per_secret(plan, query, "query")
    headers: list[tuple[str, str]] = []
    for k, v in req.headers:
        lk = k.lower()
        if lk == "authorization" and v[:6].lower() == "basic " and REF_PREFIX not in v:
            try:
                dec = base64.b64decode(v[6:].strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                dec = ""
            if REF_RE.search(dec):
                whole = REF_RE.fullmatch(dec)
                new = _replace_declared(plan, dec, f"header:{k}", lambda r, lk=lk: lk in r.headers, True)
                if whole is not None:
                    # `https://secret:NOM@hôte/…` (git, curl -u) : le client envoie `secret:NOM` comme utilisateur:mot
                    # de passe ; la valeur seule n'est pas un identifiant Basic valide → jeton en mot de passe, sous
                    # l'utilisateur `x-access-token` (accepté par GitHub ; GitLab et Forgejo acceptent tout nom).
                    new = "x-access-token:" + new
                enc = base64.b64encode(new.encode("utf-8")).decode("ascii")
                plan.basic.append((enc, v[6:].strip()))
                headers.append((k, v[:6] + enc))
                continue
        if REF_PREFIX in k:
            for n in _names(k):
                plan.check(n, "header-name", False, True)
        if REF_RE.search(v):
            v = _replace_declared(plan, v, f"header:{k}", lambda r, lk=lk: lk in r.headers, True)
        headers.append((k, v))
    body = req.body
    if body and (REF_PREFIX.encode() in body or b"secret%3a" in body.lower()):
        ctype = (req.header("content-type") or "").split(";")[0].strip().lower()
        if ctype == "application/json" or ctype.endswith("+json"):
            try:
                body = _sub_json_per_secret(plan, body)
            except (ValueError, UnicodeDecodeError):  # JSON illisible : traité comme un corps quelconque
                body = _body_raw(plan, body)
        elif ctype == "application/x-www-form-urlencoded":
            body = _sub_pairs_per_secret(plan, body.decode("latin-1"), "field").encode("latin-1")
        else:
            body = _body_raw(plan, body)
    if body is not None and body != req.body:
        headers = [(k, str(len(body))) if k.lower() == "content-length" else (k, v) for k, v in headers]
    patterns = [(a.encode(), b.encode()) for a, b in plan.basic]
    return Request(req.method, path + (f"?{query}" if query is not None else ""), headers, body), plan.events, patterns


def _replace_declared(plan: _Plan, text: str, where: str, declared_for: Any, strict: bool) -> str:
    """Comme `_Plan.replace`, mais la déclaration dépend du secret visé (règle par secret)."""

    def sub(m: re.Match[str]) -> str:
        name = _match_name(m.group(1), plan.grant.secrets)
        got = plan.grant.secrets.get(name)
        declared = got is not None and declared_for(got[0])
        name2 = plan.check(m.group(1), where, declared, strict)
        if name2 is None:
            return m.group(0)
        return plan.grant.secrets[name2][1] + m.group(1)[len(name2):]

    return REF_RE.sub(sub, text)


def _sub_pairs_per_secret(plan: _Plan, raw: str, kind: str) -> str:
    parts = []
    for item in raw.split("&"):
        k, eq, v = item.partition("=")
        key = unquote_plus(k)
        for n in _names(key):
            plan.check(n, f"{kind}:{key}", False, kind == "query")
        dec = unquote_plus(v)
        if eq and REF_RE.search(dec):
            attr = "query" if kind == "query" else "fields"
            new = _replace_declared(plan, dec, f"{kind}:{key}", lambda r, a=attr, key=key: key in getattr(r, a),
                                    kind == "query")
            v = quote(new, safe="") if new != dec else v
        parts.append(k + eq + v)
    return "&".join(parts)


def _sub_json_per_secret(plan: _Plan, body: bytes) -> bytes:
    text = body.decode("utf-8")
    doc = json.loads(text)
    expected: dict[str, int] = {}
    for path, s in _json_paths(doc):
        for raw in _names(s):
            name = _match_name(raw, plan.grant.secrets)
            got = plan.grant.secrets.get(name)
            declared = got is not None and path in got[0].fields
            name2 = plan.check(raw, f"field:{path}", declared, False)
            if name2 is not None:
                expected[name2] = expected.get(name2, 0) + 1
    for name, count in expected.items():
        rx = re.escape(REF_PREFIX + name) + r"(?![A-Za-z0-9_])"
        if len(re.findall(rx, text)) != count:
            raise Refused("location_not_allowed", name, f"secret:{name} : référence hors des champs déclarés")
        value = json.dumps(plan.grant.secrets[name][1])[1:-1]
        text = re.sub(rx, lambda _m, v=value: v, text)
    return text.encode("utf-8") if expected else body


def _body_raw(plan: _Plan, body: bytes) -> bytes:
    """Corps d'un autre type : une référence à un secret accordé y est refusée (aucun champ à déclarer) ; un nom non
    accordé passe tel quel (texte ordinaire)."""
    for raw in _names(body.decode("latin-1")):
        plan.check(raw, "body", False, False)
    return body


class Redactor:
    """Remet en référence toute valeur accordée qui revient de l'amont. Diffusion par morceaux : une valeur coupée entre
    deux morceaux est quand même vue (on garde en réserve la longueur du plus long motif moins un)."""

    def __init__(self, grant: Grant, extra: Iterable[tuple[bytes, bytes]] = ()) -> None:
        pats: dict[bytes, bytes] = {}
        for name, (_, value) in grant.secrets.items():
            if len(value) < MIN_REDACT_LEN:
                continue
            ref = (REF_PREFIX + name).encode()
            raw = value.encode("utf-8")
            for v in {raw, quote(value, safe="").encode(), json.dumps(value)[1:-1].encode(),
                      base64.b64encode(raw)}:
                pats[v] = ref
        for sent, original in extra:
            pats[sent] = original
        self._pats = sorted(pats.items(), key=lambda kv: len(kv[0]), reverse=True)
        self._keep = max((len(p) for p, _ in self._pats), default=1) - 1
        self._buf = b""
        self.hits: dict[str, int] = {}

    def _apply(self, data: bytes) -> bytes:
        for pat, ref in self._pats:
            if pat in data:
                n = data.count(pat)
                key = ref.decode("latin-1")
                key = key[len(REF_PREFIX):] if key.startswith(REF_PREFIX) else "basic"
                self.hits[key] = self.hits.get(key, 0) + n
                data = data.replace(pat, ref)
        return data

    def whole(self, data: bytes) -> bytes:
        return self._apply(data)

    def feed(self, chunk: bytes) -> bytes:
        if not self._pats:
            return chunk
        self._buf = self._apply(self._buf + chunk)
        if len(self._buf) <= self._keep:
            return b""
        cut = len(self._buf) - self._keep
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return out

    def finish(self) -> bytes:
        out, self._buf = self._apply(self._buf), b""
        return out


__all__ = [
    "HOP_HEADERS",
    "REF_PREFIX",
    "REF_RE",
    "Event",
    "Grant",
    "Redactor",
    "Refused",
    "Request",
    "SecretRule",
    "parse_rules",
    "substitute",
]
