"""Les boîtes mail et agendas d'un compte, vus par le modèle (docs/CONNECTORS-MULTI.md §3, textes validés en relecture).

UNE source par nature regroupe toutes les boîtes accessibles (les miennes, celles partagées avec moi) sous les noms
d'outils d'avant : `mailbox` (mail_search, mail_draft, mail_send) et `calendar` (calendar_*) choisissent la boîte.
Schéma JSON identique pour tous ; seule la description porte la liste, propre à chacun.

Sûreté : la liste des boîtes et les droits sont relus À CHAQUE APPEL (`catalog()`), jamais pris dans la description
mise en cache : un partage retiré ne sert plus, même si le modèle voit encore l'ancienne liste. Le noyau refuse ici,
avant tout accès au fournisseur, une écriture sur une boîte en lecture seule et un déplacement ou une corbeille sur
la boîte d'un autre.

Ids : une seule boîte accessible de cette nature = ids natifs, inchangés. Plusieurs = `<id du connecteur>|<id natif>` ;
un id natif est alors refusé (`id_unqualified`) : les ids IMAP se recouvrent d'une boîte à l'autre.
"""
from __future__ import annotations

import email.utils
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from nokker.connectors.calendar import fence_events, parse_events_result
from nokker.connectors.errors import ConnectorError
from nokker.connectors.store import ID_RE
from nokker.core.ports import ToolContext, ToolSource, ToolSpec
from nokker.core.turn import ToolResult

SEP = "|"
SUBCAL_MODES = ("caldav", "google", "graph")  # agendas à sous-agendas (le reste : un abonnement iCal, « primary »)
MAIL_LIST_TOOLS = ("mail_search", "mail_draft", "mail_send")
CAL_LIST_TOOLS = ("calendar_events", "calendar_create")
MAIL_BY_ID = ("mail_read", "mail_move", "mail_trash")
MAX_MERGED = 25
MAX_EVENTS = 200


@dataclass(frozen=True)
class Box:
    id: str
    kind: str  # mail | calendar
    label: str
    access: str  # owner | read | send | write
    is_default: bool = False
    address: str = ""  # mail : l'adresse de la boîte
    owner_account: str = ""
    owner_tenant: str = ""
    owner_name: str = ""  # vide : la boîte est à moi
    mode: str = ""  # agenda : ical | caldav

    @property
    def mine(self) -> bool:
        return self.access == "owner"

    @property
    def can_write(self) -> bool:
        if self.kind == "calendar" and self.mode == "ical":
            return False
        return self.access in ("owner", "send", "write")

    def form(self) -> str:
        return f"{self.label} · {self.id}"


# ── textes (validés en relecture, 05.11.2026) ──────────────────────────────────────────────────────────────────────

MAILBOX_PARAM = {"type": "string", "description": "facultatif : libellé ou identifiant d'une boîte de la liste ; sans "
                 "lui, une recherche couvre toutes les boîtes et un envoi part de la boîte par défaut"}
# Descriptions de la source regroupée (plusieurs boîtes possibles) ; celles des connecteurs seuls (Lokkal, une boîte)
# restent dans mail.py / calendar.py. `{max}` : MAX_RECIPIENTS du connecteur mail.
DESCRIPTIONS = {
    "mail_search": "Cherche des messages dans tes boîtes mail (syntaxe from:, to:, subject:, is:unread, "
                   "after:AAAA/MM/JJ, before:, mots libres) ; renvoie id, date, expéditeur, sujet, extrait, pas le corps.",
    "mail_read": "Lit un message (de la boîte où il se trouve) en texte brut ; les pièces jointes sont listées (nom, "
                 "taille, type), jamais ouvertes.",
    "mail_draft": "Crée un BROUILLON (boîte par défaut ou `mailbox`), jamais d'envoi. to, subject, body en texte brut ; "
                  "in_reply_to = id d'un message pour répondre dans le même fil.",
    "mail_move": "Déplace un message dans sa boîte : destination = inbox, archive, spam (indésirable) ou label:<nom> "
                 "(dossier existant). Réversible (re-déplacer). Ne supprime jamais : pour la corbeille, mail_trash.",
    "mail_trash": "Met un message à la corbeille (récupérable tant que le fournisseur la garde). Action destructive : "
                  "passe par une carte d'approbation. Pour un indésirable, préférer mail_move vers spam.",
    "mail_send": "Envoie un e-mail depuis une de tes boîtes ou une boîte partagée avec envoi. Envoi externe : passe par "
                 "une carte d'approbation. Au plus {max} destinataires, texte brut.",
    "calendar_events": "Événements des `days` prochains jours des agendas de la liste (fuseau du compte). Pour une "
                       "journée entière, `end` est le dernier jour inclus.",
    "calendar_create": "Crée un événement (agenda par défaut ou `calendar`). N'envoie aucune invitation.",
    "calendar_update": "Modifie un événement existant (id renvoyé par calendar_events) ; seuls les champs donnés changent.",
    "calendar_delete": "Supprime un événement (série entière si récurrent). Action destructive.",
}
CALENDAR_PARAM_DESC = ("facultatif : libellé ou identifiant d'un agenda de la liste (son agenda principal), « libellé/"
                       "sous-agenda », ou un sous-agenda de l'agenda par défaut (« primary », « all » ou son nom)")


def _right(b: Box) -> str:
    if b.kind == "mail":
        if b.mine:
            return "à toi, écriture" + (", par défaut" if b.is_default else "")
        return f"partagée par {b.owner_name}, " + ("lecture seule" if b.access == "read" else "lecture et envoi")
    if b.mine:
        base = "à toi, lecture seule, abonnement iCal" if b.mode == "ical" else "à toi, écriture"
        return base + (", par défaut" if b.is_default else "")
    return f"partagé par {b.owner_name}, " + ("lecture seule" if b.access == "read" else "lecture et écriture")


def mail_list_line(boxes: list[Box]) -> str:
    items = " ; ".join(f"{b.form()} — {b.address} ({_right(b)})" for b in boxes)
    head = "Boîte" if len(boxes) == 1 else "Boîtes"
    return (f"{head} : {items}. Lecture sans « mailbox » : toutes les boîtes ; écriture sans « mailbox » : la boîte "
            "par défaut.")


def calendar_list_line(boxes: list[Box]) -> str:
    items = " ; ".join(f"{b.form()} ({_right(b)})" for b in boxes)
    head = "Agenda" if len(boxes) == 1 else "Agendas"
    return (f"{head} : {items}. Une valeur seule de « calendar » désigne d'abord un agenda de cette liste, sinon un "
            "sous-agenda de l'agenda par défaut ; si elle désigne les deux, l'appel est refusé. Lecture sans "
            "« calendar » : l'agenda principal de chaque agenda ; écriture sans « calendar » : l'agenda par défaut. "
            "Chaque événement rendu porte « calendar » sous la forme libellé/sous-agenda.")


def _build_error(b: Box, e: Any) -> str:
    if getattr(e, "code", "") == "account_reconnect":
        what = f"la boîte « {b.label} »" if b.kind == "mail" else f"l'agenda « {b.label} »" if b.kind == "calendar" \
            else f"le compte « {b.label} »"
        addr = f" ({b.address})" if b.address and b.mine else ""
        verb = "doit être reconnectée" if b.kind == "mail" else "doit être reconnecté"
        who = "toi" if b.mine else (b.owner_name or "son propriétaire")  # qui agit : l'appelant, ou le propriétaire
        return f"account_reconnect : {what}{addr} {verb} par {who} dans Connecteurs."
    return f"{getattr(e, 'code', 'protocol_error')} : connecteur « {b.label} » indisponible."


def _is_reconnect(r: ToolResult | None) -> bool:
    return r is not None and r.is_error and str(r.content).startswith("account_reconnect : ")


def _err(name: str, text: str) -> ToolResult:
    return ToolResult(call_id="", name=name, content=text, is_error=True)


def _forms(boxes: list[Box]) -> str:
    return ", ".join(b.form() for b in boxes)


def ordered(boxes: list[Box]) -> list[Box]:
    """Le défaut d'abord, puis les miennes, puis les partagées (ordre d'arrivée)."""
    return sorted(boxes, key=lambda b: (not b.is_default, not b.mine))


class Ambiguous(Exception):
    """Un libellé porté par plusieurs boîtes (la mienne et une partagée, ou deux partagées) : le texte d'erreur exact."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def _owner_note(b: Box) -> str:
    if b.mine:
        return "à toi"
    return f"{'partagée' if b.kind == 'mail' else 'partagé'} par {b.owner_name}"


def find(boxes: list[Box], value: str) -> Box | None:
    """La boîte d'un libellé ou d'un id ; None si aucune ; `Ambiguous` si le libellé en désigne plusieurs (jamais de
    choix silencieux : on propose les ids)."""
    v = (value or "").strip()
    if not v:
        return None
    if ID_RE.match(v.lower()):
        return next((b for b in boxes if b.id == v.lower()), None)
    hits = [b for b in boxes if b.label.lower() == v.lower()]
    if len(hits) > 1:
        mail = hits[0].kind == "mail"
        code, what = ("mailbox_ambiguous", "plusieurs boîtes") if mail else ("calendar_ambiguous", "plusieurs agendas")
        listed = ", ".join(f"{b.form()} ({_owner_note(b)})" for b in hits)
        raise Ambiguous(f"{code} : « {v} » désigne {what} : {listed} ; précise : {' ou '.join(b.id for b in hits)}.")
    return hits[0] if hits else None


def split_id(value: str) -> tuple[str | None, str]:
    v = str(value or "")
    head, sep, rest = v.partition(SEP)
    if sep and ID_RE.match(head):
        return head, rest
    return None, v


def resolve_send(boxes: list[Box], args: dict[str, Any]) -> tuple[Box | None, str | None]:
    """Boîte d'un `mail_send` / `mail_draft` : celle du message répondu, sinon `mailbox`, sinon le défaut.
    (boîte, None) ou (None, texte d'erreur exact). Partagé avec la carte d'envoi (`MailSenderGates`)."""
    boxes = ordered(boxes)
    multi = len(boxes) > 1
    chosen = None
    if args.get("mailbox"):
        try:
            chosen = find(boxes, str(args["mailbox"]))
        except Ambiguous as e:
            return None, e.text
        if chosen is None:
            return None, f"mailbox_unknown : boîte inconnue « {args['mailbox']} ». Boîtes valides : {_forms(boxes)}."
    if args.get("in_reply_to"):
        cid, _ = split_id(str(args["in_reply_to"]))
        if multi and cid is None:
            return None, (f"id_unqualified : id sans boîte « {args['in_reply_to']} » alors que plusieurs boîtes sont "
                          "accessibles ; relance mail_search et utilise l'id qu'il rend (forme mb_…|…).")
        if cid is not None:
            src = next((b for b in boxes if b.id == cid), None)
            if src is None:
                return None, f"mailbox_unknown : boîte inconnue « {cid} ». Boîtes valides : {_forms(boxes)}."
            if chosen is not None and chosen.id != src.id:
                return None, f"mailbox_mismatch : le message vient de la boîte « {src.label} », pas de « {chosen.label} »."
            chosen = src
    chosen = chosen or (boxes[0] if boxes else None)
    if chosen is None:
        return None, "mailbox_unknown : aucune boîte reliée."
    if not chosen.can_write:
        return None, (f"mailbox_read_only : la boîte « {chosen.label} » est partagée par {chosen.owner_name} en lecture "
                      "seule ; écrire ou envoyer depuis elle est refusé.")
    return chosen, None


def sender_line(b: Box) -> str:
    """Ligne de la carte d'envoi : l'expéditeur réel, calculé par le noyau."""
    who = f", au nom de {b.owner_name}" if not b.mine else ""
    return f"Depuis : {b.label} — {b.address}{who}"


@dataclass
class MailSenderGates:
    """Portes posées AUTOUR de celles du noyau : la carte d'un `mail_send` porte TOUJOURS l'expéditeur réel (boîte,
    adresse, « au nom de » pour une boîte partagée), calculé ici par la même règle que l'envoi (`resolve_send`).
    `boxes()` : les boîtes mail du locataire courant ; None ou erreur = la carte reste celle du noyau."""

    inner: Any
    boxes: Callable[[], list[Box] | None]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def action(self, call: Any, spec: Any, turn: Any) -> list[Any]:
        verdicts = self.inner.action(call, spec, turn)
        if call.name != "mail_send" or str(getattr(spec, "source", "")) != "connector:mail":
            return verdicts
        try:
            boxes = [b for b in (self.boxes() or []) if b.kind == "mail"]
            b, err = resolve_send(boxes, call.arguments or {}) if boxes else (None, "aucune")
        except Exception:  # noqa: BLE001 — la carte reste celle du noyau ; l'envoi refera le contrôle
            return verdicts
        if b is None:
            return verdicts
        from nokker.core.turn import Decision

        line = sender_line(b)
        return [replace(v, reason=f"{v.reason}\n{line}" if v.reason else line)
                if v.decision == Decision.HITL else v for v in verdicts]


class BoxSource:
    """Source regroupée d'une nature. `catalog()` rend les boîtes accessibles MAINTENANT ; `build(box)` le connecteur
    de la boîte (secret lu dans le locataire de son propriétaire). `base(boxes)` : les specs du connecteur d'origine."""

    def __init__(self, kind: str, catalog: Callable[[], list[Box]], build: Callable[[Box], ToolSource],
                 base: Callable[[list[Box]], list[ToolSpec]]) -> None:
        self.kind, self._catalog, self._build, self._base = kind, catalog, build, base
        self._local = threading.local()  # connecteurs construits pendant l'appel en cours (un fil = un appel)

    def _boxes(self) -> list[Box]:
        return ordered([b for b in self._catalog() if b.kind == self.kind])

    def _conn(self, b: Box) -> ToolSource:
        # construit une fois PAR APPEL (vidé au début de `call`) : un secret changé ou un partage retiré compte tout de suite
        built = getattr(self._local, "built", None)
        if built is None:
            built = self._local.built = {}
        if b.id not in built:
            built[b.id] = self._build(b)
        return built[b.id]

    # ── specs ──
    def specs(self) -> list[ToolSpec]:
        boxes = self._boxes()
        if not boxes:
            return []
        out = []
        for sp in self._base(boxes):
            if self.kind == "mail":
                out.append(self._mail_spec(sp, boxes))
            else:
                out.append(self._cal_spec(sp, boxes))
        return out

    def _mail_spec(self, sp: ToolSpec, boxes: list[Box]) -> ToolSpec:
        params = sp.parameters
        from nokker.connectors.mail import MAX_RECIPIENTS

        desc = DESCRIPTIONS.get(sp.name, sp.description).format(max=MAX_RECIPIENTS)
        if sp.name in MAIL_LIST_TOOLS:
            params = {**params, "properties": {**params.get("properties", {}), "mailbox": MAILBOX_PARAM}}
            desc = f"{desc} {mail_list_line(boxes)}"
        return replace(sp, description=desc, parameters=params)

    def _cal_spec(self, sp: ToolSpec, boxes: list[Box]) -> ToolSpec:
        props = dict(sp.parameters.get("properties", {}))
        props["calendar"] = {"type": "string", "description": CALENDAR_PARAM_DESC}
        desc = DESCRIPTIONS.get(sp.name, sp.description)
        if sp.name in CAL_LIST_TOOLS:
            desc = f"{desc} {calendar_list_line(boxes)}"
        return replace(sp, description=desc, parameters={**sp.parameters, "properties": props})

    # ── appels ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self._local.built = {}
        boxes = self._boxes()
        if not boxes:
            return _err(name, "mailbox_unknown : aucune boîte reliée." if self.kind == "mail"
                        else "calendar_unknown : aucun agenda relié.")
        a = dict(arguments or {})
        try:
            if self.kind == "mail":
                return self._mail(name, a, ctx, boxes)
            return self._calendar(name, a, ctx, boxes)
        except Exception as exc:  # noqa: BLE001 — jamais d'exception hors de call()
            return _err(name, f"erreur interne : {exc.__class__.__name__}")

    def _inner(self, b: Box, name: str, a: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any] | None, ToolResult]:
        try:
            conn = self._conn(b)
        except ConnectorError as e:  # compte OAuth à reconnecter, connecteur retiré, coffre scellé : sans valeur
            return None, _err(name, _build_error(b, e))
        r = conn.call(name, a, ctx)
        if r.is_error:
            return None, r
        try:  # `calendar_events` arrive CLÔTURÉ (DONNÉE non fiable) : relu ici, re-clôturé à la sortie (`_fenced`)
            return parse_events_result(r.content), r
        except ValueError:
            return None, r

    @staticmethod
    def _ok(name: str, out: dict[str, Any]) -> ToolResult:
        return ToolResult(call_id="", name=name, content=json.dumps(out, ensure_ascii=False))

    @staticmethod
    def _q(b: Box, native: str, multi: bool) -> str:
        return f"{b.id}{SEP}{native}" if multi else native

    def _by_id(self, boxes: list[Box], raw: str, what: str) -> tuple[Box | None, str, str | None]:
        cid, native = split_id(raw)
        multi = len(boxes) > 1
        if cid is None:
            if multi:
                tool = "mail_search" if self.kind == "mail" else "calendar_events"
                form = "mb_…|…" if self.kind == "mail" else "ca_…|…"
                return None, "", (f"id_unqualified : id sans {what} « {raw} » alors que plusieurs {what}s sont "
                                  f"accessibles ; relance {tool} et utilise l'id qu'il rend (forme {form}).")
            return boxes[0], native, None
        b = next((x for x in boxes if x.id == cid), None)
        if b is None:
            code = "mailbox_unknown" if self.kind == "mail" else "calendar_unknown"
            noun = "boîte inconnue" if self.kind == "mail" else "agenda inconnu"
            valid = "Boîtes valides" if self.kind == "mail" else "Agendas valides"
            return None, "", f"{code} : {noun} « {cid} ». {valid} : {_forms(boxes)}."
        return b, native, None

    # mail
    def _mail(self, name: str, a: dict[str, Any], ctx: ToolContext, boxes: list[Box]) -> ToolResult:
        multi = len(boxes) > 1
        if name == "mail_search":
            want = a.pop("mailbox", None)
            targets = boxes
            if want:
                try:
                    b = find(boxes, str(want))
                except Ambiguous as e:
                    return _err(name, e.text)
                if b is None:
                    return _err(name, f"mailbox_unknown : boîte inconnue « {want} ». Boîtes valides : {_forms(boxes)}.")
                targets = [b]
            merged, unavailable, reconnect, extra, last_err, first_rc = [], [], [], {}, None, None
            for b in targets:
                out, r = self._inner(b, name, a, ctx)
                if out is None:
                    if _is_reconnect(r):
                        reconnect.append(b.label)
                        first_rc = first_rc or r
                    else:
                        unavailable.append(b.label)
                    last_err = r
                    continue
                for m in out.get("messages", []):
                    merged.append({**m, "id": self._q(b, str(m.get("id", "")), multi), "mailbox": b.label})
                extra = {k: v for k, v in out.items() if k not in ("messages", "count")} or extra
            if not merged and len(unavailable) + len(reconnect) == len(targets):
                if first_rc is not None:  # tout a échoué, dont une boîte à reconnecter : dire qui doit agir
                    return first_rc
                return last_err if len(targets) == 1 else _err(name, "boîtes injoignables : " + ", ".join(unavailable))
            if len(targets) > 1:
                merged.sort(key=_mail_date, reverse=True)
                try:
                    limit = max(1, min(int(a.get("max") or 10), MAX_MERGED))
                except (TypeError, ValueError):
                    limit = 10
                merged = merged[:limit]
            res = {**extra, "count": len(merged), "messages": merged}
            if unavailable:
                res["unavailable"] = unavailable
            if reconnect:
                res["reconnect"] = reconnect
            return self._ok(name, res)
        if name in MAIL_BY_ID:
            b, native, err = self._by_id(boxes, str(a.get("message_id") or ""), "boîte")
            if err:
                return _err(name, err)
            if name in ("mail_move", "mail_trash") and not b.mine:
                return _err(name, f"mailbox_not_yours : la boîte « {b.label} » appartient à {b.owner_name} ; déplacer "
                                  "ou mettre à la corbeille ses messages est refusé.")
            out, r = self._inner(b, name, {**a, "message_id": native}, ctx)
            if out is None:
                return r
            if "id" in out:
                out["id"] = self._q(b, str(out["id"]), multi)
            return self._ok(name, {**out, "mailbox": b.label})
        if name in ("mail_draft", "mail_send"):
            b, err = resolve_send(boxes, a)
            if err:
                return _err(name, err)
            a.pop("mailbox", None)
            if a.get("in_reply_to"):
                a["in_reply_to"] = split_id(str(a["in_reply_to"]))[1]
            out, r = self._inner(b, name, a, ctx)
            if out is None:
                return r
            return self._ok(name, {**out, "mailbox": b.label})
        return _err(name, f"outil inconnu : {name}")

    # calendar
    def _cal_target(self, boxes: list[Box], value: str) -> tuple[Box | None, str | None, str | None]:
        """(agenda, sous-agenda ou None = principal, erreur). Grammaire : <libellé|id>, <libellé|id>/<sous-agenda>, ou
        un sous-agenda du défaut (« primary », « all », un nom). Valeur des deux côtés : `calendar_ambiguous`."""
        v = (value or "").strip()
        default = boxes[0]
        if not v:
            return default, None, None
        try:
            if "/" in v:
                head, _, sub = v.partition("/")
                b = find(boxes, head)
                if b is not None:
                    return b, sub.strip() or None, None
            b = find(boxes, v)
        except Ambiguous as e:
            return None, None, e.text
        if b is not None:
            if v.lower() not in ("primary", "all") and default.mode in SUBCAL_MODES and self._has_sub(default, v):
                return None, None, (f"calendar_ambiguous : « {v} » désigne à la fois l'agenda {b.form()} et le sous-agenda "
                                    f"« {v} » de {default.form()} ; précise : {b.id} ou {default.label}/{v}.")
            return b, None, None
        if len(boxes) == 1 or v.lower() in ("primary", "all") or default.mode in SUBCAL_MODES:
            return default, v, None
        return None, None, f"calendar_unknown : agenda inconnu « {v} ». Agendas valides : {_forms(boxes)}."

    def _has_sub(self, b: Box, name: str) -> bool:
        try:
            cals = self._conn(b)._select("all")  # type: ignore[attr-defined]  # noqa: SLF001
        except Exception:  # noqa: BLE001 — agenda injoignable : pas d'ambiguïté démontrée
            return False
        return any(c.name.lower() == name.lower() for c in cals)

    def _subs(self, b: Box, sub: str | None) -> list[str]:
        if b.mode not in SUBCAL_MODES:
            return ["primary"]
        conn = self._conn(b)
        return [c.name for c in conn._select(sub)]  # type: ignore[attr-defined]  # noqa: SLF001

    def _calendar(self, name: str, a: dict[str, Any], ctx: ToolContext, boxes: list[Box]) -> ToolResult:
        multi = len(boxes) > 1
        if name == "calendar_events":
            raw = str(a.pop("calendar", "") or "")
            if raw:
                b, sub, err = self._cal_target(boxes, raw)
                if err:
                    return _err(name, err)
                targets = [(b, sub)]
            else:
                targets = [(b, None) for b in boxes]
            events, unavailable, reconnect, truncated, last_err, first_rc = [], [], [], 0, None, None
            for b, sub in targets:
                try:
                    subs = self._subs(b, sub)
                except ValueError as exc:
                    return _err(name, f"calendar_unknown : {exc}. Agendas valides : {_forms(boxes)}.")
                except Exception as exc:  # noqa: BLE001 — injoignable, à reconnecter : les autres agendas continuent
                    last_err = _err(name, _build_error(b, exc))
                    if _is_reconnect(last_err):
                        reconnect.append(b.label)
                        first_rc = first_rc or last_err
                    else:
                        unavailable.append(b.label)
                    continue
                for s in subs:
                    args = {**a, **({"calendar": s} if b.mode in SUBCAL_MODES else {})}
                    out, last = self._inner(b, name, args, ctx)
                    if out is None:
                        if _is_reconnect(last):
                            reconnect.append(b.label)
                            first_rc = first_rc or last
                        else:
                            unavailable.append(b.label)
                        last_err = last
                        break
                    truncated += int(out.get("truncated") or 0)
                    for e in out.get("events", []):
                        events.append({**e, "id": self._q(b, str(e.get("id", "")), multi), "calendar": f"{b.label}/{s}"})
            failed = len(set(unavailable)) + len(set(reconnect))
            if not events and failed == len(targets) and first_rc is not None:
                return first_rc  # tout a échoué, dont un agenda à reconnecter : dire qui doit agir
            if not events and last_err is not None and len(targets) == 1:
                return last_err  # un seul agenda visé : son erreur telle quelle (comme le mail)
            events.sort(key=lambda e: (str(e.get("start", "")), str(e.get("summary", ""))))
            res: dict[str, Any] = {"count": min(len(events), MAX_EVENTS), "events": events[:MAX_EVENTS]}
            if len(events) > MAX_EVENTS or truncated:
                res["truncated"] = max(len(events), truncated)
            if unavailable:
                res["unavailable"] = sorted(set(unavailable))
            if reconnect:
                res["reconnect"] = sorted(set(reconnect))
            # titres, lieux, descriptions viennent de l'extérieur : la sortie fusionnée reste clôturée comme l'amont
            return ToolResult(call_id="", name=name, content=fence_events(res))
        if name == "calendar_create":
            b, sub, err = self._cal_target(boxes, str(a.pop("calendar", "") or ""))
            if err:
                return _err(name, err)
        elif name in ("calendar_update", "calendar_delete"):
            b, native, err = self._by_id(boxes, str(a.get("event_id") or ""), "agenda")
            if err:
                return _err(name, err)
            a["event_id"] = native
            sub = None
            if a.get("calendar"):
                cb, sub, cerr = self._cal_target(boxes, str(a.pop("calendar")))
                if cerr:
                    return _err(name, cerr)
                if cb.id != b.id:
                    return _err(name, f"calendar_mismatch : l'événement vient de l'agenda « {b.label} », pas de "
                                      f"« {cb.label} ».")
        else:
            return _err(name, f"outil inconnu : {name}")
        if not b.can_write:
            if b.mine:
                return _err(name, f"calendar_read_only : l'agenda « {b.label} » est un abonnement iCal en lecture seule.")
            return _err(name, f"calendar_read_only : l'agenda « {b.label} » est partagé par {b.owner_name} en lecture "
                              "seule ; le modifier est refusé.")
        if sub:
            a["calendar"] = sub
        out, r = self._inner(b, name, a, ctx)
        if out is None:
            return r
        if "id" in out:
            out["id"] = self._q(b, str(out["id"]), multi)
        if "calendar" in out or name == "calendar_create":
            out["calendar"] = f"{b.label}/{out.get('calendar') or sub or 'primary'}"
        return self._ok(name, out)


def _mail_date(m: dict[str, Any]) -> float:
    try:
        d = email.utils.parsedate_to_datetime(str(m.get("date") or ""))
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC).timestamp()


__all__ = ["Ambiguous", "Box", "BoxSource", "MailSenderGates", "CAL_LIST_TOOLS", "MAIL_LIST_TOOLS", "SEP", "calendar_list_line", "find",
           "mail_list_line", "ordered", "resolve_send", "sender_line", "split_id"]
