"""Connecteur mail IMAP/SMTP (T3, contrat C2) : la boîte du testeur, par mot de passe d'application. Mêmes noms et effets
que `nokker/tools/effects.py::DEFAULT_EFFECTS` et que docs/TOOLS-MAIL.md, sans argument `user` (c'est SA boîte).

Une connexion par appel d'outil (ouverte, utilisée, fermée) : pas d'état partagé entre tours ni entre locataires. Hôtes
et ports passent par `net.check_endpoint` (SSRF) et la connexion est épinglée à l'IP vérifiée. Aucun secret dans un
message d'erreur ni dans un résultat d'outil. `mail_send` est SEND_EXTERNAL : la carte d'approbation du noyau passe avant."""
from __future__ import annotations

import email
import email.policy
import email.utils
import html
import imaplib
import json
import re
import smtplib
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

from nokker.connectors import imap_utf7
from nokker.connectors.errors import ConnectorError, scrub
from nokker.connectors.net import ALLOWED_PORTS, IMPLICIT_TLS, TIMEOUT_S, check_endpoint, open_pinned, tls_context
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.previews.ssrf import ResolvedHop
from nokker.tools.effects import is_trash_destination, mail_move_effect

PRESETS: dict[str, dict[str, Any]] = {
    "gmail": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 465},
    "icloud": {"imap_host": "imap.mail.me.com", "imap_port": 993, "smtp_host": "smtp.mail.me.com", "smtp_port": 587},
    "infomaniak": {"imap_host": "mail.infomaniak.com", "imap_port": 993, "smtp_host": "mail.infomaniak.com", "smtp_port": 465},
    "fastmail": {"imap_host": "imap.fastmail.com", "imap_port": 993, "smtp_host": "smtp.fastmail.com", "smtp_port": 465},
}
MAX_RESULTS = 25
MAX_BODY_CHARS = 20_000
MAX_FETCH_BYTES = 2_000_000
MAX_RECIPIENTS = 10
WATCH_FOLDER = "INBOX"  # relève du déclencheur mail: (v1 : la boîte de réception seulement)
_ADDR_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}$")
_ROLE_FLAGS = {"drafts": "\\Drafts", "trash": "\\Trash", "spam": "\\Junk", "sent": "\\Sent"}
_ROLE_NAMES = {
    "drafts": ("Drafts", "Brouillons", "Draft"),
    "trash": ("Trash", "Corbeille", "Deleted Messages", "Deleted Items", "Éléments supprimés"),
    "spam": ("Junk", "Spam", "Indésirables", "Junk E-mail", "Junk Email"),
    "sent": ("Sent", "Envoyés", "Sent Messages", "Sent Items", "Éléments envoyés"),
    "archive": ("Archive", "Archives", "All Mail", "Tous les messages"),
}
_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Réglages complets et validés (préréglage résolu). Lève `bad_settings` / `port_not_allowed` ; aucun accès réseau."""
    if not isinstance(settings, dict):
        raise ConnectorError("bad_settings", "réglages illisibles")
    preset = str(settings.get("preset") or "custom")
    if preset != "custom" and preset not in PRESETS:
        raise ConnectorError("bad_settings", f"préréglage inconnu : {preset}")
    base = dict(PRESETS.get(preset, {}))
    out = {**base, **{k: v for k, v in settings.items() if v not in (None, "")}}
    address = str(out.get("address", "")).strip()
    if not _ADDR_RE.match(address):
        raise ConnectorError("bad_settings", "adresse e-mail invalide")
    norm: dict[str, Any] = {"preset": preset, "address": address, "username": str(out.get("username") or address)}
    for key in ("imap_host", "smtp_host"):
        if not out.get(key):
            raise ConnectorError("bad_settings", f"{key} requis")
        norm[key] = str(out[key]).strip().lower()
    for key, proto in (("imap_port", "imap"), ("smtp_port", "smtp")):
        try:
            norm[key] = int(out.get(key) or 0)
        except (TypeError, ValueError):
            raise ConnectorError("bad_settings", f"{key} invalide") from None
        if norm[key] not in ALLOWED_PORTS[proto]:
            raise ConnectorError("port_not_allowed", f"port {norm[key]} refusé pour {proto}")
    folders = out.get("folders")
    if isinstance(folders, dict):
        norm["folders"] = {k: str(v) for k, v in folders.items() if k in (*_ROLE_FLAGS, "archive") and v}
    return norm


# ─────────────────────────────── transport épinglé ───────────────────────────────


class _PinnedIMAP(imaplib.IMAP4):
    def __init__(self, hop: ResolvedHop, tls: bool, timeout_s: float) -> None:
        self._hop, self._tls = hop, tls
        super().__init__(hop.hostname, hop.port, timeout_s)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        return open_pinned(self._hop, tls=self._tls, timeout_s=timeout or TIMEOUT_S)


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, hop: ResolvedHop, tls: bool, timeout_s: float) -> None:
        self._hop, self._tls = hop, tls
        super().__init__(local_hostname="nokker.ai", timeout=timeout_s)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return open_pinned(self._hop, tls=self._tls, timeout_s=timeout)


def _auth_or_protocol(exc: Exception, secret: str) -> ConnectorError:
    text = scrub(str(exc), secret)
    low = text.lower()
    if isinstance(exc, smtplib.SMTPAuthenticationError) or "authenticationfailed" in low or "invalid credentials" in low \
            or "login failed" in low or "authentication failed" in low or "[auth" in low:
        return ConnectorError("auth_failed", "identifiants refusés (mot de passe d'application ?)")
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, OSError)):
        return ConnectorError("unreachable", f"serveur injoignable ({exc.__class__.__name__})")
    return ConnectorError("protocol_error", text[:200] or exc.__class__.__name__)


def connect_imap(s: dict[str, Any], secret: str, *, resolver=socket.getaddrinfo) -> Any:
    hop = check_endpoint(s["imap_host"], s["imap_port"], "imap", resolver=resolver)
    tls = ("imap", hop.port) in IMPLICIT_TLS
    try:
        m = _PinnedIMAP(hop, tls, TIMEOUT_S)
        if not tls:
            m.starttls(tls_context())
        m.login(s["username"], secret)
    except ConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001 — imaplib/ssl/socket lèvent des types variés
        raise _auth_or_protocol(exc, secret) from None
    return m


def connect_smtp(s: dict[str, Any], secret: str, *, resolver=socket.getaddrinfo) -> Any:
    hop = check_endpoint(s["smtp_host"], s["smtp_port"], "smtp", resolver=resolver)
    tls = ("smtp", hop.port) in IMPLICIT_TLS
    try:
        m = _PinnedSMTP(hop, tls, TIMEOUT_S)
        m.connect(hop.hostname, hop.port)
        if not tls:
            m.starttls(context=tls_context())
        m.login(s["username"], secret)
    except ConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _auth_or_protocol(exc, secret) from None
    return m


# ─────────────────────────────── aides IMAP ───────────────────────────────


def _q(name: str) -> str:
    """Argument IMAP entre guillemets (échappement \\ et ")."""
    if re.search(r"[\r\n\x00]", name):
        raise ConnectorError("bad_settings", "caractère interdit")
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ok(res: tuple[str, list[Any]], what: str) -> list[Any]:
    typ, data = res
    if typ != "OK":
        raise ConnectorError("protocol_error", f"{what} refusé par le serveur")
    return data


def _parse_list(lines: list[Any]) -> list[tuple[set[str], str]]:
    out: list[tuple[set[str], str]] = []
    for raw in lines:
        if not isinstance(raw, bytes):
            continue
        m = _LIST_RE.match(raw.decode("utf-8", "replace"))
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        out.append(({f.lower() for f in m.group("flags").split()}, imap_utf7.decode(name)))
    return out


def _body_text(msg: email.message.EmailMessage) -> tuple[str, list[dict[str, Any]]]:
    atts: list[dict[str, Any]] = []
    plain = htm = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        ctype = part.get_content_type()
        if fname or part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True) or b""
            atts.append({"name": fname or "(sans nom)", "type": ctype, "size": len(payload)})
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            continue
        if ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "text/html" and htm is None:
            htm = content
    return (plain if plain is not None else _html_to_text(htm or "")), atts


class _Strip(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)


def _html_to_text(src: str) -> str:
    p = _Strip()
    try:
        p.feed(src)
    except Exception:  # noqa: BLE001 — HTML cassé : on garde ce qu'on a
        pass
    return re.sub(r"\n{3,}", "\n\n", html.unescape("".join(p.out))).strip()


def _clean(s: str | None, n: int = 300) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def _header_text(msg: email.message.Message, name: str) -> str:
    try:
        return str(msg.get(name, "") or "")
    except Exception:  # noqa: BLE001 — en-tête malformé
        return ""


_SEARCH_KEYS = {"from": "FROM", "to": "TO", "cc": "CC", "subject": "SUBJECT"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_date(v: str) -> str | None:
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", v)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{d:02d}-{_MONTHS[mo - 1]}-{y}"


def translate_query(query: str) -> tuple[list[str], list[str]]:
    """Syntaxe façon Gmail (from:, to:, subject:, is:unread, after:, before:, mots libres) → critères IMAP SEARCH.
    Rend (critères, notes sur ce qui a été ignoré). Les mots libres cherchent dans tout le message."""
    crit: list[str] = []
    notes: list[str] = []
    for tok in re.findall(r'-?\w+:"[^"]*"|-?"[^"]*"|\S+', query):
        neg = tok.startswith("-")
        t = tok[1:] if neg else tok
        key, sep, val = t.partition(":")
        val = val.strip('"') if sep else ""
        one: list[str] | None
        if sep and key.lower() in _SEARCH_KEYS and val:
            one = [_SEARCH_KEYS[key.lower()], _q(val)]
        elif sep and key.lower() == "is" and val in ("unread", "read", "starred"):
            one = [{"unread": "UNSEEN", "read": "SEEN", "starred": "FLAGGED"}[val]]
        elif sep and key.lower() in ("after", "before") and _imap_date(val):
            one = ["SINCE" if key.lower() == "after" else "BEFORE", _imap_date(val)]  # type: ignore[list-item]
        elif sep and key.lower() in ("has", "in", "label", "category", "larger", "smaller", "older_than", "newer_than"):
            notes.append(f"critère « {key}: » non géré hors Gmail, ignoré")
            continue
        else:
            one = ["TEXT", _q(t.strip('"'))]
        crit.extend(["NOT", *one] if neg else one)
    return crit, notes


# ─────────────────────────────── connecteur ───────────────────────────────

_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="mail_search",
        description="Cherche des messages dans ta boîte mail (syntaxe from:, to:, subject:, is:unread, after:AAAA/MM/JJ, "
                    "before:, mots libres) ; renvoie id, date, expéditeur, sujet, extrait, pas le corps.",
        parameters={"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "critères de recherche ; vide = derniers messages"},
            "max": {"type": "integer", "description": f"nombre de résultats (1 à {MAX_RESULTS}, défaut 10)"},
            "folder": {"type": "string", "description": "inbox (défaut), archive, sent, drafts, spam, trash, ou un nom de dossier"}}},
        effect=Effect.READ, source="connector:mail"),
    ToolSpec(
        name="mail_read",
        description="Lit un message de ta boîte en texte brut ; les pièces jointes sont listées (nom, taille, type), jamais ouvertes.",
        parameters={"type": "object", "required": ["message_id"], "properties": {
            "message_id": {"type": "string", "description": "id renvoyé par mail_search"}}},
        effect=Effect.READ, source="connector:mail"),
    ToolSpec(
        name="mail_draft",
        description="Crée un BROUILLON dans ta boîte (jamais d'envoi). to, subject, body en texte brut ; "
                    "in_reply_to = id d'un message pour répondre dans le même fil.",
        parameters={"type": "object", "required": ["to", "subject", "body"], "properties": {
            "to": {"type": "string", "description": "destinataire(s), séparés par des virgules"},
            "subject": {"type": "string"}, "body": {"type": "string"},
            "in_reply_to": {"type": "string", "description": "facultatif : id du message auquel on répond"}}},
        effect=Effect.WRITE, source="connector:mail"),
    ToolSpec(
        name="mail_move",
        description="Déplace un message de ta boîte : destination = inbox, archive, spam (indésirable) ou label:<nom> "
                    "(dossier existant). Réversible (re-déplacer). Ne supprime jamais : pour la corbeille, mail_trash.",
        parameters={"type": "object", "required": ["message_id", "destination"], "properties": {
            "message_id": {"type": "string", "description": "id renvoyé par mail_search / mail_read"},
            "destination": {"type": "string", "description": "inbox | archive | spam | label:<nom>"},
            "reason": {"type": "string", "description": "facultatif : pourquoi"}}},
        # Vers la corbeille (`label:Trash`, `label:[Gmail]/Corbeille`…) : DESTRUCTIVE, comme `mail_trash` (carte
        # forte) ; ailleurs : écriture réversible. Même règle chez nokker.ai quand l'outil est relayé (X20b).
        effect=Effect.WRITE, effect_of=mail_move_effect, source="connector:mail"),
    ToolSpec(
        name="mail_trash",
        description="Met un message de ta boîte à la corbeille (récupérable tant que le fournisseur la garde). Action "
                    "destructive : passe par une carte d'approbation. Pour un indésirable, préférer mail_move vers spam.",
        parameters={"type": "object", "required": ["message_id"], "properties": {
            "message_id": {"type": "string"}, "reason": {"type": "string"}}},
        effect=Effect.DESTRUCTIVE, source="connector:mail"),
    ToolSpec(
        name="mail_send",
        description="Envoie un e-mail depuis ta boîte, en ton nom. Envoi externe : passe par une carte d'approbation. "
                    f"Au plus {MAX_RECIPIENTS} destinataires, texte brut.",
        parameters={"type": "object", "required": ["to", "subject", "body"], "properties": {
            "to": {"type": "string", "description": "destinataire(s), séparés par des virgules"},
            "cc": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
            "in_reply_to": {"type": "string", "description": "facultatif : id du message auquel on répond"}}},
        effect=Effect.SEND_EXTERNAL, source="connector:mail"),
]


class MailConnector:
    """`ToolSource` mail d'un compte. `imap_factory()` / `smtp_factory()` rendent une session déjà authentifiée (tests)."""

    def __init__(self, settings: dict[str, Any], secret: str, *, imap_factory: Callable[[], Any] | None = None,
                 smtp_factory: Callable[[], Any] | None = None, locality: str | None = None) -> None:
        self.s = normalize_settings(settings)
        self._secret = secret
        self.locality = locality  # docs/LOKKAL.md §6.7 : « cloud » chez nokker.ai, « local » dans Lokkal
        self._imap_factory = imap_factory or (lambda: connect_imap(self.s, secret))
        self._smtp_factory = smtp_factory or (lambda: connect_smtp(self.s, secret))

    def specs(self) -> list[ToolSpec]:
        if self.locality is None:
            return list(_SPECS)
        from dataclasses import replace

        return [replace(sp, locality=self.locality) for sp in _SPECS]

    @contextmanager
    def _imap(self) -> Iterator[Any]:
        m = self._imap_factory()
        try:
            yield m
        except ConnectorError:
            raise
        except (imaplib.IMAP4.error, OSError, ValueError) as exc:
            raise _auth_or_protocol(exc, self._secret) from None
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    # ── dossiers ──

    def _folders(self, m: Any) -> dict[str, str]:
        """role → nom de dossier : réglage explicite > attribut RFC 6154 > noms usuels ; `inbox` toujours INBOX."""
        listed = _parse_list(_ok(m.list(), "LIST"))
        names = {n for _, n in listed}
        roles: dict[str, str] = {"inbox": "INBOX"}
        explicit = self.s.get("folders", {})
        for role in ("drafts", "trash", "spam", "sent", "archive"):
            found = explicit.get(role)
            if not found and role != "archive":
                found = next((n for fl, n in listed if _ROLE_FLAGS[role].lower() in fl), None)
            if not found and role == "archive":
                found = next((n for fl, n in listed if "\\archive" in fl), None) or \
                    next((n for fl, n in listed if "\\all" in fl), None)
            if not found:
                found = next((c for c in _ROLE_NAMES[role] if c in names), None)
            if found:
                roles[role] = found
        roles["_all"] = "\x00".join(sorted(names))
        return roles

    @staticmethod
    def _folder(roles: dict[str, str], wanted: str) -> str:
        wanted = (wanted or "inbox").strip()
        if wanted.lower() in roles and not wanted.startswith("_"):
            return roles[wanted.lower()]
        if wanted.lower().startswith("label:"):
            wanted = wanted[6:]
        if wanted in roles["_all"].split("\x00") or wanted.upper() == "INBOX":
            return "INBOX" if wanted.upper() == "INBOX" else wanted
        raise ConnectorError("bad_settings", f"dossier introuvable : {wanted[:60]}")

    def _select(self, m: Any, folder: str, *, readonly: bool) -> int:
        _ok(m.select(_q(imap_utf7.encode(folder)), readonly), f"SELECT {folder}")
        typ, data = m.response("UIDVALIDITY")
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    @staticmethod
    def _mid(folder: str, validity: int, uid: str) -> str:
        return f"{folder}:{validity}:{uid}"

    @staticmethod
    def _split_id(message_id: str) -> tuple[str, int, str]:
        try:
            folder, validity, uid = str(message_id).rsplit(":", 2)
            if not folder or not uid.isdigit():
                raise ValueError
            return folder, int(validity), uid
        except ValueError:
            raise ConnectorError("bad_settings", "id de message invalide (utiliser un id de mail_search)") from None

    def _open_message(self, m: Any, message_id: str, *, readonly: bool) -> tuple[str, str]:
        folder, validity, uid = self._split_id(message_id)
        actual = self._select(m, folder, readonly=readonly)
        if validity and actual and validity != actual:
            raise ConnectorError("bad_settings", "id périmé : le dossier a été reconstruit, relancer mail_search")
        return folder, uid

    # ── outils ──

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        fn = getattr(self, f"_t_{name}", None) if name in {s.name for s in _SPECS} else None
        if fn is None:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        try:
            out = fn(arguments or {})
        except ConnectorError as exc:
            return ToolResult(call_id="", name=name, content=f"{exc.code} : {scrub(exc.message, self._secret)}", is_error=True)
        except Exception as exc:  # noqa: BLE001 — jamais d'exception hors de call(), jamais de secret dans le texte
            return ToolResult(call_id="", name=name, content=f"erreur interne : {scrub(str(exc), self._secret)[:200]}",
                              is_error=True)
        return ToolResult(call_id="", name=name, content=json.dumps(out, ensure_ascii=False))

    def _t_mail_search(self, a: dict[str, Any]) -> dict[str, Any]:
        query = str(a.get("query") or "").strip()
        try:
            limit = max(1, min(int(a.get("max") or 10), MAX_RESULTS))
        except (TypeError, ValueError):
            limit = 10
        with self._imap() as m:
            roles = self._folders(m)
            folder = self._folder(roles, str(a.get("folder") or "inbox"))
            validity = self._select(m, folder, readonly=True)
            notes: list[str] = []
            m._encoding = "utf-8"  # noqa: SLF001 — critères non ASCII (SEARCH CHARSET UTF-8)
            if query and self.s["preset"] == "gmail":
                typ, data = m.uid("SEARCH", "X-GM-RAW", _q(query))
            else:
                crit, notes = translate_query(query)
                typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", *(crit or ["ALL"]))
            uids = (data[0] or b"").split() if typ == "OK" else []
            uids = [u.decode() for u in uids][-limit:][::-1]
            msgs = [self._summary(m, folder, validity, u) for u in uids]
        res: dict[str, Any] = {"folder": folder, "query": query, "count": len(msgs), "messages": [x for x in msgs if x]}
        if notes:
            res["notes"] = notes
        return res

    def _summary(self, m: Any, folder: str, validity: int, uid: str) -> dict[str, Any] | None:
        typ, data = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] "
                                          "BODY.PEEK[TEXT]<0.2000>)")
        if typ != "OK" or not data:
            return None
        meta = " ".join(p[0].decode("utf-8", "replace") if isinstance(p, tuple) else (p.decode("utf-8", "replace") if isinstance(p, bytes) else "")
                        for p in data)
        blobs = [p[1] for p in data if isinstance(p, tuple)]
        if not blobs:
            return None
        head = email.message_from_bytes(blobs[0], policy=email.policy.default)
        snippet = ""
        if len(blobs) > 1:
            snippet = _clean(_html_to_text(blobs[1].decode("utf-8", "replace")) if b"<html" in blobs[1].lower()
                             or b"<div" in blobs[1].lower() else blobs[1].decode("utf-8", "replace"), 200)
        flags = re.search(r"FLAGS \(([^)]*)\)", meta)
        return {"id": self._mid(folder, validity, uid), "date": _header_text(head, "Date"),
                "from": _clean(_header_text(head, "From")), "subject": _clean(_header_text(head, "Subject")),
                "snippet": snippet, "unread": "\\Seen" not in (flags.group(1) if flags else "")}

    def _t_mail_read(self, a: dict[str, Any]) -> dict[str, Any]:
        with self._imap() as m:
            folder, uid = self._open_message(m, str(a.get("message_id") or ""), readonly=True)
            typ, data = m.uid("FETCH", uid, f"(BODY.PEEK[]<0.{MAX_FETCH_BYTES}>)")
            raw = next((p[1] for p in (data or []) if isinstance(p, tuple)), None)
            if typ != "OK" or raw is None:
                raise ConnectorError("bad_settings", "message introuvable")
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        body, atts = _body_text(msg)
        truncated = len(body) > MAX_BODY_CHARS or len(raw) >= MAX_FETCH_BYTES
        return {"id": a["message_id"], "from": _clean(_header_text(msg, "From")), "to": _clean(_header_text(msg, "To")),
                "cc": _clean(_header_text(msg, "Cc")), "date": _header_text(msg, "Date"),
                "subject": _clean(_header_text(msg, "Subject")), "body": body[:MAX_BODY_CHARS],
                "attachments": atts, "truncated": truncated}

    def _compose(self, a: dict[str, Any], m: Any | None) -> EmailMessage:
        to = self._addresses(a.get("to"), "to")
        cc = self._addresses(a.get("cc"), "cc") if a.get("cc") else []
        if len(to) + len(cc) > MAX_RECIPIENTS:
            raise ConnectorError("bad_settings", f"au plus {MAX_RECIPIENTS} destinataires")
        subject = str(a.get("subject") or "")
        if re.search(r"[\r\n]", subject):
            raise ConnectorError("bad_settings", "retour à la ligne interdit dans le sujet")
        msg = EmailMessage()
        msg["From"] = self.s["address"]
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.s["address"].split("@", 1)[1])
        msg.set_content(str(a.get("body") or ""))
        if a.get("in_reply_to") and m is not None:
            folder, uid = self._open_message(m, str(a["in_reply_to"]), readonly=True)
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)])")
            raw = next((p[1] for p in (data or []) if isinstance(p, tuple)), b"")
            h = email.message_from_bytes(raw, policy=email.policy.default)
            mid = _clean(_header_text(h, "Message-ID"), 500)
            if mid and not re.search(r"[\r\n]", mid):
                msg["In-Reply-To"] = mid
                msg["References"] = (_clean(_header_text(h, "References"), 1500) + " " + mid).strip()
        return msg

    @staticmethod
    def _addresses(value: Any, field: str) -> list[str]:
        parsed = [a for _, a in email.utils.getaddresses([str(value or "")]) if a]
        if not parsed or any(not _ADDR_RE.match(a) for a in parsed):
            raise ConnectorError("bad_settings", f"adresse invalide dans « {field} »")
        return parsed

    def _t_mail_draft(self, a: dict[str, Any]) -> dict[str, Any]:
        with self._imap() as m:
            msg = self._compose(a, m if a.get("in_reply_to") else None)
            roles = self._folders(m)
            if "drafts" not in roles:
                raise ConnectorError("unsupported", "dossier Brouillons introuvable sur ce compte")
            _ok(m.append(_q(imap_utf7.encode(roles["drafts"])), "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()),
                         msg.as_bytes()), "APPEND")
        return {"drafted": True, "folder": roles["drafts"], "to": msg["To"], "subject": msg["Subject"]}

    def _t_mail_send(self, a: dict[str, Any]) -> dict[str, Any]:
        reply = bool(a.get("in_reply_to"))
        sent_copy = None
        if reply:
            with self._imap() as m:
                msg = self._compose(a, m)
        else:
            msg = self._compose(a, None)
        recipients = [x for _, x in email.utils.getaddresses([msg["To"] or "", msg.get("Cc") or ""]) if x]
        smtp = self._smtp_factory()
        try:
            refused = smtp.send_message(msg, from_addr=self.s["address"], to_addrs=recipients)
        except smtplib.SMTPRecipientsRefused as exc:
            raise ConnectorError("protocol_error", "tous les destinataires ont été refusés") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise _auth_or_protocol(exc, self._secret) from None
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
        if self.s["preset"] != "gmail":  # Gmail range lui-même l'envoi dans « Envoyés »
            try:
                with self._imap() as m:
                    roles = self._folders(m)
                    if "sent" in roles:
                        m.append(_q(imap_utf7.encode(roles["sent"])), "(\\Seen)", imaplib.Time2Internaldate(time.time()),
                                 msg.as_bytes())
                        sent_copy = roles["sent"]
            except ConnectorError:
                sent_copy = None
        return {"sent": True, "to": msg["To"], "subject": msg["Subject"], "refused": sorted(refused or {}),
                "copy_in": sent_copy}

    def _t_mail_move(self, a: dict[str, Any]) -> dict[str, Any]:
        dest = str(a.get("destination") or "").strip()
        if not dest:
            raise ConnectorError("bad_settings", "destination requise")
        with self._imap() as m:
            roles = self._folders(m)
            if dest.lower() not in ("inbox", "archive", "spam") and not dest.lower().startswith("label:"):
                raise ConnectorError("bad_settings", "destination : inbox | archive | spam | label:<nom>")
            target = self._folder(roles, dest)
            if target == roles.get("trash") and not is_trash_destination(dest):
                # Corbeille sous un nom que la table des effets ne reconnaît pas (dossier marqué \Trash par le
                # serveur) : l'appel a été classé « rangement », il ne doit pas supprimer. La corbeille a son outil.
                raise ConnectorError("unsupported", "ce dossier est la corbeille du compte : utiliser mail_trash "
                                                    "(action destructive, carte d'approbation)")
            src, uid = self._open_message(m, str(a.get("message_id") or ""), readonly=False)
            if src == target:
                return {"moved": False, "reason": "déjà dans ce dossier", "folder": target}
            return self._relocate(m, uid, src, target)

    def _relocate(self, m: Any, uid: str, src: str, target: str) -> dict[str, Any]:
        caps = {c.decode().upper() if isinstance(c, bytes) else str(c).upper() for c in (m.capabilities or ())}
        q = _q(imap_utf7.encode(target))
        if "MOVE" in caps:
            _ok(m.uid("MOVE", uid, q), "MOVE")
        else:
            _ok(m.uid("COPY", uid, q), "COPY")
            _ok(m.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"), "STORE")
            if "UIDPLUS" in caps:
                _ok(m.uid("EXPUNGE", uid), "EXPUNGE")
            else:
                _ok(m.expunge(), "EXPUNGE")
        return {"moved": True, "from": src, "folder": target}

    def _t_mail_trash(self, a: dict[str, Any]) -> dict[str, Any]:
        with self._imap() as m:
            roles = self._folders(m)
            if "trash" not in roles:
                raise ConnectorError("unsupported", "dossier Corbeille introuvable sur ce compte")
            src, uid = self._open_message(m, str(a.get("message_id") or ""), readonly=False)
            if src == roles["trash"]:
                return {"moved": False, "reason": "déjà dans la corbeille"}
            return self._relocate(m, uid, src, roles["trash"])

    # ── relève de fond (déclencheur mail:<filtre>, nokker/agents/mailwatch.py) ──

    def watch(self, criteria: list[str], *, last_uid: int | None, validity: int | None, fetch: int) -> dict[str, Any]:
        """Une relève de la boîte de réception, en LECTURE SEULE (EXAMINE, BODY.PEEK : rien n'est marqué lu) :
        `{validity, top, baseline: True}` quand il n'y a pas encore de curseur ou que l'UIDVALIDITY a changé (le dossier
        a été reconstruit : on repart du plus grand UID, sans rien rejouer) ; sinon `{validity, top, uids, messages}` —
        les UID > `last_uid` qui passent `criteria` (critères IMAP SEARCH déjà construits, croissants), et les
        métadonnées (id, expéditeur, sujet, date ; jamais le corps) des `fetch` plus récents. Lève `ConnectorError`
        (sans secret)."""
        with self._imap() as m:
            actual = self._select(m, WATCH_FOLDER, readonly=True)
            top = self._top_uid(m)
            if last_uid is None or validity is None or actual != validity:
                return {"validity": actual, "top": top, "baseline": True}
            m._encoding = "utf-8"  # noqa: SLF001 — critères non ASCII (SEARCH CHARSET UTF-8)
            typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", "UID", f"{last_uid + 1}:*", *criteria)
            if typ != "OK":
                raise ConnectorError("protocol_error", "SEARCH refusé par le serveur")
            # « n:* » désigne toujours au moins le dernier message (RFC 3501) : on ne garde que ce qui est après le curseur.
            uids = sorted({int(u) for u in (data[0] or b"").split() if u.isdigit() and int(u) > last_uid})
            listed = uids[-fetch:] if fetch > 0 else []
            messages = [x for x in (self._watch_meta(m, WATCH_FOLDER, actual, str(u)) for u in listed) if x]
        return {"validity": actual, "top": max([top, *uids]), "uids": uids, "messages": messages}

    @staticmethod
    def _top_uid(m: Any) -> int:
        """Plus grand UID du dossier sélectionné : UIDNEXT − 1 (réponse du SELECT), sinon le plus grand UID trouvé."""
        typ, data = m.response("UIDNEXT")
        try:
            return max(0, int(data[0]) - 1)
        except (TypeError, ValueError, IndexError):
            pass
        typ, data = m.uid("SEARCH", "ALL")
        uids = [int(u) for u in (data[0] or b"").split() if u.isdigit()] if typ == "OK" and data else []
        return max(uids, default=0)

    def _watch_meta(self, m: Any, folder: str, validity: int, uid: str) -> dict[str, Any] | None:
        typ, data = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        raw = next((p[1] for p in (data or []) if isinstance(p, tuple)), None) if typ == "OK" else None
        if raw is None:
            return None
        h = email.message_from_bytes(raw, policy=email.policy.default)
        return {"id": self._mid(folder, validity, uid), "from": _clean(_header_text(h, "From"), 200),
                "subject": _clean(_header_text(h, "Subject"), 200), "date": _clean(_header_text(h, "Date"), 80)}

    # ── test de connexion ──

    def test(self) -> str:
        """Ouvre IMAP puis SMTP (authentification seulement, rien n'est envoyé). Lève `ConnectorError`."""
        with self._imap() as m:
            _ok(m.select("INBOX", True), "SELECT INBOX")
            typ, data = m.response("EXISTS")
            count = int(data[0]) if data and data[0] else 0
        smtp = self._smtp_factory()
        try:
            smtp.noop()
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
        return f"IMAP et SMTP ok ({count} messages dans la boîte de réception)"


__all__ = ["MailConnector", "PRESETS", "WATCH_FOLDER", "connect_imap", "connect_smtp", "normalize_settings",
           "translate_query"]
