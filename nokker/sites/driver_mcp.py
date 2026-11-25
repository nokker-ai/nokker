"""Sites connectés — pilote réel : le Chrome de `sites-chrome` (même moteur humain que le pod du pool) parlé en
MCP `browser_*` (docs/CONNECTED-SITES.md §3.1).

⚠️ Lot L0 : écrit contre le format de snapshot du pod navigateur (`- rôle "nom" [ref=eN]` puis éventuellement `: valeur`),
exercé par ses tests de format seulement. Il n'a pas encore tourné contre un vrai `sites-chrome` : c'est la première
étape de l'essai réel du §13.3 (sur GO explicite), avant toute page LinkedIn.

`sitesd` est le seul client de ce MCP (réseau `sitesnet`) ; un résultat d'outil du pod ne sort jamais tel quel :
il passe par `MaskedDriver` (valeurs secrètes retirées) puis par la recette (champs déclarés) puis par la clôture.
"""
from __future__ import annotations

import base64
import json as _json
import re
from typing import Any

from nokker.sites.driver import Node

# `[ref=eN]` peut être SUIVI d'autres attributs (`[cursor=pointer]` sur un lien du moteur humain, vu en réel le 06.11 :
# sans eux, tous les liens étaient perdus) ; ils comptent comme ceux d'avant la ref.
_LINE = re.compile(r'^\s*-\s+(?P<role>[a-z]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>[^\n]*?)\[ref=(?P<ref>e\d+)\]'
                   r'(?P<after>(?:\s*\[[^\]]*\])*)(?::\s*(?P<value>.*))?\s*$')
# Arbre du moteur humain (Playwright `_snapshotForAI`) : indentation de 2 espaces par niveau ; lignes sans ref
# (`- paragraph`, `- text: …`, `- /url: …`, `- time [ref]: …`).
_ANY = re.compile(r'^(?P<ind>\s*)-\s+(?P<body>.*?)\s*$')
_CONTAINERS = ("article", "listitem", "comment", "row", "cell", "region", "group")
_UNREAD = re.compile(r"\b(non lu|unread)\b", re.IGNORECASE)
# Fragments d'interface retirés AVANT la clôture (relecture des textes du 06.11) : ce ne sont pas des données du site mais
# des boutons et compteurs que la jointure des textes d'un conteneur collerait au contenu. Ligne entière seulement ;
# « … voir plus » en fin de texte est coupé.
UI_FRAGMENTS = re.compile(
    r"(?:…|\.\.\.)?\s*(?:voir plus|plus|see more|more|afficher plus|j[’']aime|like|commenter|comment|republier|repost"
    r"|envoyer|send|partager|share|r[ée]agir|react)"
    r"|[\d\s.,  ]+(?:k|m)?(?:\s*(?:j[’']aime|r[ée]actions?|commentaires?|comments?|republications?|reposts?|likes?"
    r"|vues?|views?|impressions?))?", re.IGNORECASE)
_MORE_TAIL = re.compile(r"\s*(?:…|\.\.\.)\s*(?:voir plus|plus|see more|more|afficher plus)\s*$", re.IGNORECASE)


def _clean(values: list[str]) -> list[str]:
    out = []
    for v in values:
        v = _MORE_TAIL.sub("", v).strip()
        if v and not UI_FRAGMENTS.fullmatch(v):
            out.append(v)
    return out


def _unquote(v: str) -> str:
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def _entries(text: str) -> list[dict[str, Any]]:
    """Lignes de l'arbre : {level, kind, role, name, ref, rest, value, parent}."""
    out: list[dict[str, Any]] = []
    stack: list[int] = []  # indices des ancêtres, par niveau
    for line in (text or "").splitlines():
        m = _ANY.match(line)
        if not m:
            continue
        level = len(m.group("ind")) // 2
        body = m.group("body")
        e: dict[str, Any] = {"level": level, "role": "", "name": "", "ref": None, "rest": "", "value": ""}
        if body.startswith("/url:"):
            e["kind"], e["value"] = "url", _unquote(body[5:])
        elif body.startswith("text:"):
            e["kind"], e["value"] = "text", _unquote(body[5:])
        else:
            lm = _LINE.match(line)
            if lm:
                e.update(kind="node", role=lm.group("role"), name=(lm.group("name") or "").replace('\\"', '"'),
                         ref=lm.group("ref"), rest=(lm.group("rest") or "") + (lm.group("after") or ""),
                         value=_unquote(lm.group("value") or ""))
            else:  # `- paragraph`, `- paragraph: texte`, `- generic` … sans ref
                rm = re.match(r'^(?P<role>[a-z]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?[^:]*(?::\s*(?P<value>.*))?$', body)
                if not rm:
                    continue
                e.update(kind="node", role=rm.group("role"), name=rm.group("name") or "",
                         value=_unquote(rm.group("value") or ""), rest=body)
        del stack[level:]
        e["parent"] = stack[-1] if stack else None
        out.append(e)
        stack.append(len(out) - 1)
    return out


def _children(entries: list[dict[str, Any]], i: int) -> list[int]:
    return [j for j in range(i + 1, len(entries)) if entries[j]["parent"] == i]


def _descendants(entries: list[dict[str, Any]], i: int) -> list[int]:
    out, lvl = [], entries[i]["level"]
    for j in range(i + 1, len(entries)):
        if entries[j]["level"] <= lvl:
            break
        out.append(j)
    return out


def parse_snapshot(text: str) -> list[Node]:
    """Nœuds à ref, dans l'ordre de la page. En plus de la ligne elle-même (`[k=v]`, `[password]`, `: valeur`) :
    - `href` : le `/url` du nœud, ou (article, élément de liste…) celui du premier lien qu'il contient ;
    - `date` : la valeur du premier `time` contenu ; `unread` : « non lu » / « unread » dans le contenu ;
    - valeur d'un conteneur (article, élément de liste…) : ses textes et paragraphes, hors noms de liens et dates ;
      valeur d'un lien sans valeur, posé dans un paragraphe ou un bloc sans nom : les textes qui le suivent dans ce bloc
      (ligne de résultat « nom — description »)."""
    entries = _entries(text)
    out = []
    for i, e in enumerate(entries):
        if e.get("kind") != "node" or not e["ref"]:
            continue
        attrs: dict[str, str] = {}
        for k, v in re.findall(r"\[(\w+)=([^\]]*)\]", e["rest"]):
            attrs[k] = v
        for flag in re.findall(r"\[(\w+)\]", e["rest"]):  # `[checked]`, `[disabled]`, `[selected]`, `[required]`
            if flag not in ("password",):
                attrs.setdefault(flag, "true")
        if "[password]" in e["rest"] or attrs.get("type") == "password":
            attrs["type"] = "password"
        value = e["value"]
        desc = _descendants(entries, i)
        own_url = next((entries[j]["value"] for j in _children(entries, i) if entries[j].get("kind") == "url"), "")
        if own_url:
            attrs.setdefault("href", own_url)
        if e["role"] in ("combobox", "listbox"):
            # liste déroulante : ses options (enfants `option`), l'option choisie comme valeur (pack Emploi, formulaires)
            opts = [entries[j] for j in desc if entries[j].get("role") == "option"]
            if opts:
                attrs.setdefault("options", "|".join(o["name"] for o in opts if o["name"]))
                chosen = next((o["name"] for o in opts if "[selected]" in o.get("rest", "")), "")
                if chosen and not value:
                    value = chosen
        if e["role"] in _CONTAINERS:
            if "href" not in attrs:
                u = next((entries[j]["value"] for j in desc if entries[j].get("kind") == "url"), "")
                if u:
                    attrs["href"] = u
            t = next((entries[j]["value"] for j in desc if entries[j].get("role") == "time" and entries[j]["value"]), "")
            if t:
                attrs.setdefault("date", t)
            texts = [entries[j]["value"] for j in desc
                     if entries[j]["value"] and entries[j].get("kind") in ("text", "node")
                     and entries[j].get("role") not in ("time", "link")
                     and not (entries[j].get("kind") == "text" and _UNREAD.fullmatch(entries[j]["value"].strip()))]
            if any(_UNREAD.fullmatch(entries[j]["value"].strip() or "-") for j in desc if entries[j].get("kind") == "text"):
                attrs.setdefault("unread", "1")
            if not value and texts:
                value = " ".join(_clean(texts))
        elif e["role"] == "link" and not value and e["parent"] is not None:
            par = entries[e["parent"]]
            if par.get("kind") == "node" and not par["name"] and par["role"] in ("paragraph", "generic", "listitem"):
                sib = [entries[j]["value"] for j in _children(entries, e["parent"])
                       if j > i and entries[j]["value"] and entries[j].get("kind") in ("text", "node")
                       and entries[j].get("role") not in ("link", "time")]
                value = " ".join(_clean(sib))
        out.append(Node(ref=e["ref"], role=e["role"], name=e["name"], value=value, attrs=attrs))
    return out


class McpBrowserDriver:
    """`driver.Driver` sur le MCP de `sites-chrome` : une session par connecteur, profil monté depuis l'archive."""

    def __init__(self, caller: Any, profile: bytes | None = None) -> None:
        self.caller = caller  # objet avec call(name, args) -> (texte, is_error) ; `_McpCaller` en production
        # archive tar.gz du profil (déchiffrée par ProfileStore) : posée par `sites_profile_import` avant le premier
        # geste, dans le tmpfs de l'enfant de `sites-chrome` ; None = profil vierge
        self.profile = profile
        self._imported = profile is None
        self._url = "about:blank"
        self._secret: set[str] = set()
        self._nodes: list[Node] | None = None

    def _call(self, name: str, args: dict[str, Any]) -> str:
        if not self._imported:
            self._imported = True
            _t, err = self.caller.call("sites_profile_import", {"archive": base64.b64encode(self.profile or b"").decode()})
            if err:
                raise RuntimeError("navigateur : profil du site non posé")
        text, is_error = self.caller.call(name, args)
        if is_error:
            raise RuntimeError(f"navigateur : {name} en échec")  # jamais le texte (il peut contenir une valeur)
        return text

    def _refresh_nodes(self) -> None:
        snap = self._call("browser_snapshot", {})
        # en-tête du moteur humain : « - page : <titre> — <url> » ; « - Page URL: <url> » (format Playwright MCP) accepté
        m = re.search(r"(?m)^- page : .*? — (\S+)\s*$", snap) or re.search(r"(?m)^- Page URL: (\S+)", snap)
        if m:
            self._url = m.group(1)
        self._nodes = parse_snapshot(snap)

    def goto(self, url: str) -> None:
        self._secret.clear()
        self._call("browser_navigate", {"url": url, "snapshot": False})
        self._refresh_nodes()

    def url(self) -> str:
        return self._url

    def nodes(self) -> list[Node]:
        if self._nodes is None:
            self._refresh_nodes()
        return list(self._nodes or [])

    def text(self) -> str:
        return self._call("browser_read_text", {"maxChars": 20000})

    def click(self, ref: str) -> None:
        self._call("browser_click", {"ref": ref, "snapshot": False})
        self._refresh_nodes()

    def type(self, ref: str, text: str) -> None:
        self._call("browser_type", {"ref": ref, "text": text, "snapshot": False})
        self._refresh_nodes()

    def secret_fields(self) -> set[str]:
        return set(self._secret)

    # ── gestes de formulaire (pack Emploi) ──
    def select(self, ref: str, value: str) -> None:
        self._call("browser_select_option", {"ref": ref, "values": [value], "snapshot": False})
        self._refresh_nodes()

    def check(self, ref: str, on: bool) -> None:
        node = next((n for n in self.nodes() if n.ref == ref), None)
        if node is not None and (node.attrs.get("checked") == "true") == bool(on):
            return
        self._call("browser_click", {"ref": ref, "snapshot": False})
        self._refresh_nodes()

    def upload(self, ref: str, filename: str, data: bytes, mime: str = "application/pdf") -> None:
        """Pièce jointe SANS fichier sur le disque de `sites-chrome` (aucun fichier local n'y est permis) : le fichier
        est construit dans la page et posé sur le champ (`DataTransfer`), puis l'événement `change` est émis."""
        b64 = base64.b64encode(data).decode("ascii")
        fn = ("(el) => { const b = atob(" + _json.dumps(b64) + "); const u = new Uint8Array(b.length); "
              "for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); const dt = new DataTransfer(); "
              "dt.items.add(new File([u], " + _json.dumps(filename) + ", {type: " + _json.dumps(mime) + "})); "
              "el.files = dt.files; el.dispatchEvent(new Event('input', {bubbles: true})); "
              "el.dispatchEvent(new Event('change', {bubbles: true})); return el.files.length ? el.files[0].name : ''; }")
        out = self._call("browser_evaluate", {"ref": ref, "function": fn})
        if filename not in (out or ""):  # relu dans la page : le champ porte bien CE fichier
            raise RuntimeError("navigateur : pièce jointe non posée")
        self._refresh_nodes()

    def mark_secret(self, ref: str) -> None:
        self._secret.add(ref)
        # le champ reste couvert même repassé en texte (« Afficher ») : style posé avant toute capture
        self._call("browser_evaluate", {"ref": ref, "function": "(el) => { el.setAttribute('data-nk-secret','1'); "
                                                                "el.style.webkitTextSecurity='disc'; }"})

    def screenshot(self) -> bytes:
        self._call("browser_evaluate", {"function": BLUR_SCRIPT})
        out = self._call("browser_take_screenshot", {})
        return out.encode("utf-8") if isinstance(out, str) else base64.b64decode(out)

    def refresh(self) -> None:
        self._call("browser_navigate", {"url": self._url, "snapshot": False}) if self._url.startswith("https://") \
            else None
        self._refresh_nodes()

    def export_profile(self) -> bytes:
        # archive tar.gz rendue en base64 par `sites-chrome` (Chrome fermé, cookies écrits, sans caches)
        return base64.b64decode(self._call("sites_profile_export", {}).strip(), validate=True)

    def close(self) -> None:
        try:
            self._call("browser_close", {})
        except Exception:  # noqa: BLE001
            pass


# Champs floutés avant toute capture du noyau (mêmes champs que sites-chrome, docs/BROWSER-LIVE.md §6) : mot de passe,
# carte (`autocomplete=cc-*`), code à usage unique (`one-time-code`), champs marqués par le coffre (`data-nk-secret`), et
# champs de code 2FA reconnus par leur libellé (label, aria-label, placeholder, name, id).
BLUR_SELECTORS = ("input[type=password]", "input[autocomplete^='cc-']", "input[autocomplete='one-time-code']",
                  "[data-nk-secret]")
OTP_LABEL_RE = (r"one[- ]?time|\botp\b|\b2fa\b|\bmfa\b|totp|authenticator|verification code|"
                r"code de v[ée]rification|code (?:sms|re[çc]u)|sms[- ]?code|bestätigungscode|sicherheitscode|"
                r"codice di verifica|codice sms|mtan|passcode|security code|\bpin\b")
BLUR_SCRIPT = (
    "() => { const rx = new RegExp(" + _json.dumps(OTP_LABEL_RE) + ", 'i'); "
    "const hot = new Set(document.querySelectorAll(" + _json.dumps(", ".join(BLUR_SELECTORS)) + ")); "
    "document.querySelectorAll('input, textarea').forEach(e => { "
    "const lab = [e.getAttribute('aria-label'), e.getAttribute('placeholder'), e.getAttribute('name'), e.id, "
    "...Array.from(e.labels || []).map(l => l.textContent)].filter(Boolean).join(' '); "
    "if (rx.test(lab)) hot.add(e); }); "
    "hot.forEach(e => { e.style.filter = 'blur(8px)'; }); return hot.size; }")

__all__ = ["BLUR_SCRIPT", "BLUR_SELECTORS", "McpBrowserDriver", "parse_snapshot"]
