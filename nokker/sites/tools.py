"""Sites connectés — outils vus par le modèle (docs/CONNECTED-SITES.md §8 ; textes exacts §8.6, relus et figés).

Lots L0/L1 : `sites_connect`, `sites_list`, `sites_read`, `sites_search`. Aucun geste d'écriture n'est au registre
(`sites.write` éteint, lot L2). Sans site accessible, seul `sites_connect` existe (pour en ajouter un).
Aucun schéma ne porte de mot de passe ni de code (relecture des textes, 05.11).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.sites.fence import Fencer, fence
from nokker.sites.governor import GovernorError
from nokker.sites.model import Connector, State, signal_text
from nokker.sites.service import CONSENT_VERSION, SiteError, SitesService, host_ok
from nokker.sites.store import StoreError

logger = logging.getLogger(__name__)

TOOLS = ("sites_connect", "sites_list", "sites_read", "sites_search")

DESCRIPTIONS = {
    "sites_list": "Sans kind : liste les sites connectés (libellé, site, compte, état, dernière connexion réussie), en "
                  "lecture seule, sans rien relancer. Avec kind (notifications, messages, invitations ou posts, selon "
                  "le site) : éléments récents d'un site connecté, du plus récent au plus ancien ; chaque élément "
                  "porte une ref pour sites_read.",
    "sites_read": "Lit un élément d'un site connecté par sa ref (fil de messages, profil, post avec ses commentaires), "
                  "dans la session de la personne. url sert seulement à lire une page du site qui n'a pas de ref ; "
                  "pour une page d'un autre site, web_fetch.",
    "sites_search": "Cherche sur un site connecté (kind : people, posts ou companies, selon le site) ; au plus 10 "
                    "résultats, chacun avec une ref.",
    "sites_connect": "Ajoute un compte d'un site (site connu ou URL, identifiant, libellé facultatif) et lance la "
                     "connexion, ou relance celle d'un connecteur existant (site seul). Ne prend jamais de mot de passe "
                     "ni de code : le résultat donne un lien de saisie sécurisée à transmettre tel quel ; la connexion "
                     "n'est confirmée que par un résultat « connected ». Pour savoir quels sites sont connectés, "
                     "sites_list sans kind, jamais sites_connect.",
}

_SITE = {"type": "string", "description": "libellé ou identifiant d'un site de la liste"}
PARAMS = {
    "sites_list": {"type": "object", "properties": {
        "site": _SITE,
        "kind": {"type": "string", "enum": ["notifications", "messages", "invitations", "posts"]},
        "max": {"type": "integer", "minimum": 1, "maximum": 20}}},
    "sites_read": {"type": "object", "properties": {
        "site": _SITE, "ref": {"type": "string"}, "url": {"type": "string"}}},
    "sites_search": {"type": "object", "properties": {
        "site": _SITE, "query": {"type": "string"},
        "kind": {"type": "string", "enum": ["people", "posts", "companies"]},
        "max": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query", "kind"]},
    "sites_connect": {"type": "object", "properties": {
        "site": {"type": "string", "description": "site connu (« LinkedIn »), URL, ou libellé / identifiant d'un "
                                                  "connecteur existant"},
        "username": {"type": "string", "description": "e-mail ou nom d'utilisateur sur le site (nouveau compte)"},
        "label": {"type": "string", "description": "facultatif : nom du connecteur (par défaut, le nom du site)"}},
        "required": ["site"]},
}

CONNECT_MESSAGES = {
    "connecting": "Connexion à « {label} » en cours ; rappelle sites_connect avec site=« {label} » pour suivre.",
    "needs_consent": "« {label} » : l'avertissement sur les conditions d'utilisation du site doit d'abord être accepté "
                     "par la personne ; transmets ce lien tel quel : {link}.",
    "needs_password": "« {label} » demande un mot de passe : une demande de saisie sécurisée est partie {where} ; "
                      "transmets ce lien tel quel : {link}. Ne demande pas le mot de passe dans la conversation.",
    "needs_code": "« {label} » demande un code de vérification : une demande est partie {where} ; transmets ce "
                  "lien tel quel : {link}. Le code expire dans 3 minutes.",
    "needs_link": "« {label} » a envoyé un lien de connexion par e-mail ; le lien est récupéré dans la boîte reliée, "
                  "sinon une demande est partie {where} : {link}.",
    "needs_app_approval": "« {label} » demande une validation dans son application : une demande est partie {where} "
                          "(« valide sur ton téléphone puis appuie sur Fait »).",
    "needs_you": "« {label} » demande une vérification de sécurité du site ou affiche un écran non reconnu ; la personne a reçu "
                 "le lien pour la faire {where}.",
    "connected": "« {label} » est connecté (compte : {account}).",
    "suspended": "« {label} » : identifiant refusé par le site ; la personne doit le remplacer dans Connecteurs → Sites "
                 "connectés. Aucune nouvelle tentative.",
    "paused": "« {label} » est en pause ({reason}) ; rien n'a été fait sur le site.",
    "failed": "Connexion à « {label} » abandonnée ({reason}) ; rien n'a été fait sur le site.",
}
# Modèle exact de l'erreur `site_paused` (dans l'empreinte des textes).
SITE_PAUSED = "site_paused : « {label} » est en pause jusqu'au {until} ({signal}) ; rien n'a été fait."
# Canal des demandes, d'après la config du compte (un compte n'a pas forcément Telegram).
WHERE_TELEGRAM = "sur Telegram"
WHERE_APP = "dans l'application Nokker"

_COUNTERS = {"pages": "pages vues", "searches": "recherches", "profiles": "profils ouverts", "messages": "messages",
             "logins": "reconnexions automatiques"}
_FIELDS = {"text": "texte", "with": "avec", "last": "dernier message", "from": "de", "name": "nom",
           "headline": "titre", "author": "auteur"}
# Étiquettes vues par le modèle, en français (relecture des textes du 06.11) ; les valeurs de `kind` restent celles du
# schéma. Repli FERMÉ : un code inconnu ne sort jamais brut (« éléments » / « élément » / « champ », journalisé).
LIST_TITLE = {"notifications": "notifications", "messages": "messages", "invitations": "invitations",
              "posts": "publications"}
SEARCH_TITLE = {"people": "recherche de personnes", "posts": "recherche de publications",
                "companies": "recherche d'entreprises"}
READ_TITLE = {"thread": "fil de messages", "profile": "profil", "post": "publication", "page": "page"}
ITEM_LABEL = {"notif": "notification", "msg": "message", "in": "profil", "post": "publication",
              "comment": "commentaire", "page": "page"}


def _label(table: dict[str, str], key: Any, fallback: str) -> str:
    out = table.get(str(key))
    if out is None:
        logger.warning("sites : étiquette inconnue %r (repli « %s »)", key, fallback)
        return fallback
    return out
_STATE_WORD = {State.CONNECTED: "connecté", State.EXPIRED: "connecté", State.SUSPENDED: "suspendu",
               State.NEEDS_PASSWORD: "vérification en attente", State.NEEDS_CODE: "vérification en attente",
               State.NEEDS_LINK: "vérification en attente", State.NEEDS_APP_APPROVAL: "vérification en attente",
               State.NEEDS_YOU: "vérification en attente",
               State.NEEDS_CONSENT: "conditions d'utilisation à accepter"}
_NOT_CONNECTED = (State.ABSENT, State.CONNECTING, State.FAILED, State.DISCONNECTED)


UNKNOWN_OWNER = "son propriétaire"


def owner_names(households: Any) -> Callable[[str], str]:
    """Nom du propriétaire d'un site partagé, à LA MÊME SOURCE que 068 (`connectors/boxes.py` : `Box.owner_name` =
    `Member.name` du foyer, résolu par le service des connecteurs) ; sans foyer lisible : « son propriétaire »."""

    def name(account: str) -> str:
        if households is None:
            return UNKNOWN_OWNER
        try:
            hh = households.of(account)
            m = hh.member(account) if hh is not None else None
        except Exception:  # noqa: BLE001 — foyer illisible : jamais l'identifiant brut
            m = None
        return m.name if m is not None else UNKNOWN_OWNER

    return name


def state_word(c: Connector, now: float) -> str:
    if c.state == State.PAUSED or (c.paused_until and now < c.paused_until):
        return f"en pause jusqu'au {_dm(c.paused_until)}" if c.paused_until else "en pause"
    if c.state in _NOT_CONNECTED:
        return "non connecté"
    return _STATE_WORD.get(c.state, "non connecté")


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _dm(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def error_text(code: str, *, names: Callable[[str], str] | None = None, **x: Any) -> str:
    """Textes exacts du §8.6 (`code : texte`). `names` : compte propriétaire → nom affiché (même source que 068)."""
    names = names or (lambda account: UNKNOWN_OWNER)
    lab = x.get("label", "")
    if code == "argument_invalide":
        case = x.get("case")
        if case == "site_required":
            return f"argument_invalide : plusieurs sites accessibles ; précise `site` ({', '.join(x['labels'])})."
        if case == "kind":
            return (f"argument_invalide : kind={x['value']} n'existe pas pour « {lab} » ; valeurs : "
                    f"{', '.join(x['valid'])}.")
        if case == "ref_and_url":
            return "argument_invalide : sites_read prend `ref` ou `url`, pas les deux."
        if case == "no_secret":
            return (f"argument_invalide : {x['tool']} ne prend jamais de mot de passe ni de code ; la saisie passe par "
                    "le lien sécurisé.")
        if case == "missing":
            return f"argument_invalide : {x['tool']} demande `{x['arg']}`."
        return f"argument_invalide : {x.get('why', 'argument manquant ou invalide')}."
    if code == "url_hors_site":
        hosts = ", ".join(h for h in x.get("hosts", ()) if not h.startswith("www."))
        return f"url_hors_site : {x.get('url')} n'est pas une page de « {lab} » (hôtes : {hosts}) ; utilise web_fetch."
    if code == "site_unknown":
        valid = ", ".join(f"{c.label} · {c.id}" for c in x.get("valid", []))
        return f"site_unknown : site inconnu « {x.get('given', '')} ». Sites valides : {valid or 'aucun'}."
    if code == "site_ambiguous":
        hits = x["hits"]
        parts = ", ".join(f"{c.label} · {c.id} ({'à toi' if a == 'owner' else 'partagé par ' + names(c.account)})"
                          for c, a in hits)
        return (f"site_ambiguous : « {x['given']} » désigne plusieurs sites : {parts} ; précise : "
                f"{' ou '.join(c.id for c, _ in hits)}.")
    if code == "site_reconnect":
        return (f"site_reconnect : la session « {lab} » a expiré et la reconnexion automatique n'a pas abouti (pas "
                "d'identifiant enregistré, ou code non donné à temps) ; la personne a été prévenue ; rien n'a été fait.")
    if code == "site_suspended":
        return (f"site_suspended : « {lab} » est suspendu (identifiant refusé par le site) ; la personne doit remplacer "
                "son identifiant dans Connecteurs → Sites connectés ; rien n'a été fait.")
    if code == "site_needs_you":
        return (f"site_needs_you : « {lab} » demande une vérification de sécurité du site ou affiche un écran non "
                "reconnu ; rien n'a été fait ; la personne a reçu le lien pour la faire.")
    if code == "site_paused":
        return SITE_PAUSED.format(label=lab, until=_dm(x["until"]), signal=signal_text(x.get("signal", "")))
    if code == "quota":
        return (f"quota : plafond atteint pour « {lab} » ({_COUNTERS.get(x['counter'], x['counter'])} : {x['cap']} par "
                f"{x['per']}) ; reprise possible à {_hm(x['retry_at'])} ; rien n'a été fait.")
    if code == "quiet_hours":
        return (f"quiet_hours : « {lab} » n'est pas utilisé entre 23 h et 7 h par un agent de fond ; rien n'a été "
                "fait.")
    if code == "site_read_only":
        return (f"site_read_only : « {lab} » est partagé par {names(x.get('owner_account', ''))} en lecture seule ; "
                "écrire est refusé.")
    if code == "unsupported":
        return f"unsupported : « {lab} » n'a pas de recette : seule la lecture par url est possible."
    if code == "ref_unknown":
        return "ref_unknown : référence inconnue ou expirée ; relance sites_list ou sites_search."
    if code == "recipe_broken":
        return (f"recipe_broken : le geste « {x.get('gesture')} » de « {lab} » ne reconnaît plus la page (étape : "
                f"{x.get('step')}) ; rien n'a été envoyé ; il est désactivé jusqu'à une mise à jour.")
    if code == "busy":
        return (f"busy : « {lab} » est déjà en cours d'utilisation ; rien n'a été fait ; propose de réessayer plus "
                "tard.")
    if code == "machine_offline":
        return f"machine_offline : la machine qui porte « {lab} » est éteinte ou injoignable ; rien n'a été fait."
    if code == "site_connected":
        return f"site_connected : « {lab} » est un site connecté ; pour ses pages, utilise sites_read site={lab}."
    return f"{code} : rien n'a été fait."


class SitesToolSource:
    """Source `sites_*` du noyau (locality `local` ; exposée aussi au modèle de nokker.ai par le relais, §8.5)."""

    def __init__(self, service: SitesService, who: Callable[[], str], *, requests: Any = None,
                 households: Any = None,
                 where: Callable[[str], str] | None = None) -> None:
        self.service = service
        self.who = who
        self.requests = requests or service.requests
        self.names = owner_names(households)
        self.where = where or (lambda account: WHERE_APP)

    # ── descriptions (§8.6) ──
    def sites_line(self, account: str) -> str:
        vis = self.service.visible(account)
        if not vis:
            return ""
        parts = []
        for c, a in vis:
            who = c.account_name or c.username
            if a != "owner":
                who = fence(c.site, "account", who, check=False)[0]
            rights = "à toi" if a == "owner" else (
                f"partagé par {self.names(c.account)}, " + ("lecture seule" if a == "read" else "lecture et envoi"))
            state = state_word(c, self.service.clock())
            parts.append(f"{c.label} · {c.id} — {who} ({rights}, {state})")
        head = "Site : " if len(parts) == 1 else "Sites : "
        line = head + " ; ".join(parts) + "."
        if len(parts) > 1:
            line += " Avec plusieurs sites, « site » est obligatoire ; aucune lecture ne couvre tous les sites à la fois."
        gestures = []
        for c, a in vis:
            g = self.service.recipes[c.site].gestures_line(read_only=(a == "read"))
            body = " ; ".join(f"{verb} {'|'.join(kinds)}" for verb, kinds in g.items())
            ro = " (lecture seule)" if a == "read" else ""
            gestures.append(f"Gestes {c.label}{ro} : {body}.")
        return line + " " + " ".join(gestures)

    def specs(self) -> list[ToolSpec]:
        account = self.who()
        line = self.sites_line(account)
        out = [ToolSpec(name="sites_connect", description=DESCRIPTIONS["sites_connect"],
                        parameters=PARAMS["sites_connect"], effect=Effect.WRITE, source="builtin", locality="local")]
        if not line:
            # aucun site : `sites_list` (sans kind) reste offert, pour que « quels sites sont connectés ? » ait une
            # réponse en lecture au lieu d'un `sites_connect` (répétition du 07.11, acte 4d)
            out.append(ToolSpec(name="sites_list", description=DESCRIPTIONS["sites_list"],
                                parameters=PARAMS["sites_list"], effect=Effect.READ, source="builtin",
                                locality="local", fresh=True))
            return out
        for name in ("sites_list", "sites_read", "sites_search"):
            desc = DESCRIPTIONS[name] + (" " + line if name != "sites_read" else "")
            out.append(ToolSpec(name=name, description=desc, parameters=PARAMS[name], effect=Effect.READ,
                                source="builtin", locality="local", fresh=True))
        return out

    # ── appels ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        args = dict(arguments or {})
        if any(k in args for k in ("password", "code", "mot_de_passe", "passwd", "otp")):
            # jamais un secret par un argument (contrat des textes) : refusé en clair, rien n'est transmis au site
            return ToolResult(call_id="", name=name, is_error=True, reason="argument_invalide",
                              content=error_text("argument_invalide", case="no_secret", tool=name))
        account = self.who()
        from nokker.browserlive.sites import note_thread

        note_thread(account, ctx.thread_id, ctx.sender)  # fil de la bulle `browser` d'un flux lancé d'ici
        live = (ctx.channel or "") not in ("crew", "agent", "background")
        try:
            if name == "sites_connect":
                return self._ok(name, json.dumps(self._connect(account, args, ctx), ensure_ascii=False))
            if name == "sites_list":
                return self._ok(name, self._list(account, args, live))
            if name == "sites_read":
                return self._ok(name, self._read(account, args, live))
            if name == "sites_search":
                return self._ok(name, self._search(account, args, live))
        except SiteError as e:
            return ToolResult(call_id="", name=name, content=error_text(e.code, names=self.names, **e.extra), is_error=True,
                              reason=e.code)
        except GovernorError as e:
            return ToolResult(call_id="", name=name, content=error_text(e.code, names=self.names, **e.extra), is_error=True,
                              reason=e.code)
        return ToolResult(call_id="", name=name, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), is_error=True)

    @staticmethod
    def _ok(name: str, content: str) -> ToolResult:
        return ToolResult(call_id="", name=name, content=content)

    def _kind(self, conn: Connector, kind: Any, valid: tuple[str, ...], tool: str) -> str:
        if not kind:
            raise SiteError("argument_invalide", case="missing", tool=tool, arg="kind")
        if kind not in valid:
            raise SiteError("argument_invalide", case="kind", value=kind, valid=valid, label=conn.label)
        return str(kind)

    @staticmethod
    def _max(value: Any, default: int, cap: int) -> int:
        try:
            return max(1, min(cap, int(value)))
        except (TypeError, ValueError):
            return default

    def _render(self, conn: Connector, title: str, items: list[dict]) -> str:
        f = Fencer(conn.site)
        lines = [f"Site « {conn.label} » — {title} : {len(items)} élément(s)."]
        for i, it in enumerate(items, 1):
            meta = []
            if it.get("ref"):
                meta.append(f"ref {it['ref']}")
            m = it.get("meta") or {}
            if m.get("date"):
                meta.append(str(m["date"]))
            if m.get("degree"):
                meta.append(f"degré {m['degree']}")
            if m.get("unread"):
                meta.append("non lu")
            lines.append(f"{i}. " + (" · ".join(meta) if meta else _label(ITEM_LABEL, it.get("kind"), "élément")))
            for k, v in (it.get("fields") or {}).items():
                lines.append(f"   {_label(_FIELDS, k, 'champ')} : {f(k, str(v))}")
        return f.wrap("\n".join(lines))

    def _accounts(self, account: str) -> str:
        """Sites visibles du compte, en lecture seule : aucun appel au navigateur, aucun identifiant secret."""
        vis = self.service.visible(account)
        if not vis:
            return "Aucun site connecté."
        now = self.service.clock()
        lines = [f"{len(vis)} site(s) :"]
        for c, a in vis:
            who = c.account_name or c.username
            if a != "owner":
                who = fence(c.site, "account", who, check=False)[0]
            rights = "à toi" if a == "owner" else (
                f"partagé par {self.names(c.account)}, " + ("lecture seule" if a == "read" else "lecture et envoi"))
            last = f", dernière connexion réussie {_dm(c.last_ok_at)}" if c.last_ok_at else ""
            site = f" ({c.site_name})" if c.label.casefold() != c.site_name.casefold() else ""  # pas « LinkedIn (LinkedIn) »
            lines.append(f"- {c.label}{site} · {c.id} — {who} ({rights}) : {state_word(c, now)}{last}.")
        return "\n".join(lines)

    def _list(self, account: str, args: dict, live: bool) -> str:
        if not args.get("kind"):
            return self._accounts(account)
        conn, _ = self.service.resolve(account, args.get("site"))
        recipe = self.service.recipe(conn)
        kind = self._kind(conn, args.get("kind"), recipe.list_kinds, "sites_list")
        items = self.service.list_items(conn, kind, self._max(args.get("max"), 10, 20), live=live)
        return self._render(conn, _label(LIST_TITLE, kind, "éléments"), items)

    def _search(self, account: str, args: dict, live: bool) -> str:
        conn, _ = self.service.resolve(account, args.get("site"))
        recipe = self.service.recipe(conn)
        query = str(args.get("query") or "").strip()
        if not query:
            raise SiteError("argument_invalide", case="missing", tool="sites_search", arg="query")
        kind = self._kind(conn, args.get("kind"), recipe.search_kinds, "sites_search")
        items = self.service.search(conn, kind, query, self._max(args.get("max"), 10, 10), live=live)
        return self._render(conn, _label(SEARCH_TITLE, kind, "éléments"), items)

    def _read(self, account: str, args: dict, live: bool) -> str:
        ref, url = args.get("ref"), args.get("url")
        if ref and url:
            raise SiteError("argument_invalide", case="ref_and_url")
        if not ref and not url:
            raise SiteError("argument_invalide", case="missing", tool="sites_read", arg="ref` ou `url")
        if ref:
            conn, _, _ = self.service.ref_target(str(ref))
            if self.service.access(account, conn) is None:
                raise SiteError("ref_unknown")
            out = self.service.read(conn, ref=str(ref), live=live)
        else:
            conn, _ = self.service.resolve(account, args.get("site"))
            recipe = self.service.recipe(conn)
            if not host_ok(str(url), recipe.hosts):
                raise SiteError("url_hors_site", url=url, label=conn.label, hosts=recipe.hosts)
            out = self.service.read(conn, url=str(url), live=live)
        title = _label(READ_TITLE, out["what"], "éléments")
        return self._render(conn, title, out["items"])

    def _connect(self, account: str, args: dict, ctx: ToolContext) -> dict:
        site = str(args.get("site") or "").strip()
        username = str(args.get("username") or "").strip()
        if not site:
            raise SiteError("argument_invalide", case="missing", tool="sites_connect", arg="site")
        conn = None
        if not username:
            try:
                conn, access = self.service.resolve(account, site)
            except SiteError:
                conn = None
            if conn is not None and access != "owner":
                raise SiteError("site_read_only", label=conn.label, owner_account=conn.account)
        if conn is None:
            recipe = self.service.find_site(site)
            if recipe is None:
                raise SiteError("unsupported", label=site)
            if not username:
                raise SiteError("argument_invalide", case="missing", tool="sites_connect", arg="username")
            if not self.service.store.has_consent(account, recipe.site, CONSENT_VERSION):
                rid = f"consent-{recipe.site}"
                return {"site": recipe.name, "status": "needs_consent", "secure_link": self.service.secure_link(rid),
                        "message": CONNECT_MESSAGES["needs_consent"].format(label=recipe.name,
                                                                            link=self.service.secure_link(rid))}
            try:
                conn = self.service.add(account, recipe.site, username, args.get("label") or None)
            except StoreError as e:
                raise SiteError("argument_invalide", why=str(e)) from None
        flow = self.service.connect(conn, wait_s=3.0)
        conn = self.service.store.get(conn.id) or conn
        status = flow.status
        out: dict[str, Any] = {"site": conn.label, "id": conn.id, "status": status}
        link = ""
        if status in ("needs_password", "needs_code", "needs_link") and flow.request_id:
            req = self.requests.get(flow.request_id)
            link = self.service.secure_link(flow.request_id)
            out["secure_link"] = link
            if req is not None:
                out["expires_in"] = max(0, int(req.expires_at - self.service.clock()))
                self.requests.arm(ctx.thread_id, ctx.sender, req)  # le RÉSULTAT arme la capture (§4.6)
        if status in ("needs_password", "needs_code", "needs_link") and not link:
            status = out["status"] = "connecting"  # demande pas encore ouverte : jamais un message sans son lien
        if status not in CONNECT_MESSAGES:  # état inattendu : on dit « en cours », jamais « connecté »
            status = out["status"] = "connecting"
        out["message"] = CONNECT_MESSAGES[status].format(label=conn.label, link=link,
                                                         account=conn.account_name or conn.username,
                                                         reason=signal_text(flow.reason), where=self.where(account))
        return out


__all__ = ["CONNECT_MESSAGES", "DESCRIPTIONS", "PARAMS", "SitesToolSource", "TOOLS", "error_text"]
