"""nokker/qa/provenance.py — provenance des valeurs et politique d'arguments critiques (docs/PROVENANCE.md).

Prototype allégé de CaMeL (arXiv 2503.18813) et de Multi-CaMeL (arXiv 2610.05640), derrière le drapeau `provenance`
(défaut éteint : le noyau ne construit rien, aucun verdict de plus, aucun texte de plus vu par le modèle).

Principe. Nokker n'a qu'un modèle, qui voit tout : on ne peut pas faire écrire le plan par un modèle « propre » comme
CaMeL. On garde donc l'autre moitié du papier, celle qui ne dépend pas du modèle : chaque texte qui entre dans le tour
porte une ÉTIQUETTE de source (utilisateur, mail, web, pièce jointe, sous-agent, Crew, connecteur relayé…) avec un niveau
de confiance, et avant une action sensible on cherche d'où vient chaque valeur critique de ses arguments (destinataire
d'un envoi, adresse d'un `web_fetch`, hôte/chemin/commande d'un `terminal`). Une valeur qu'on ne trouve QUE dans une
source non fiable — jamais dans une demande de l'utilisateur ni dans la liste autorisée — rend l'action refusée ou
soumise à une carte FORTE, avec une explication qui nomme la source.

Le registre est en mémoire, par fil RACINE (un sous-agent `<fil>/sa-<id>` partage celui de son parent : c'est la
propagation multi-agents), borné (fils, segments, caractères). Aucune donnée d'un fil n'est lue pour un autre fil ; les
identifiants de fil portent le locataire (`tn_…:`), donc deux locataires ne partagent jamais un registre.

Rien ici n'appelle un modèle. Tout est déterministe et rejouable.
"""
from __future__ import annotations

import enum
import json
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from nokker.core.turn import Decision, Effect, Gate, Verdict

# ─────────────────────────────── étiquettes ───────────────────────────────


class Trust(str, enum.Enum):
    FIABLE = "fiable"  # l'utilisateur, ses consignes, la liste autorisée : une valeur trouvée là est « blanchie »
    INTERNE = "interne"  # produit chez l'utilisateur (espace de travail, mémoire, en-tête de mail) : ni preuve ni teinte
    NON_FIABLE = "non_fiable"  # contenu écrit par un tiers : une valeur trouvée SEULEMENT là est teintée


# source → (description lue dans les explications, confiance). Les descriptions sont vues par le modèle (raison d'un
# refus ou d'une carte) : elles ne contiennent jamais de texte venu de la source elle-même.
SOURCES: dict[str, tuple[str, Trust]] = {
    "utilisateur": ("une demande de l'utilisateur", Trust.FIABLE),
    "liste-autorisée": ("la liste des destinataires autorisés", Trust.FIABLE),
    "délégation": ("la consigne d'un sous-agent, écrite par l'agent parent", Trust.INTERNE),
    "espace-de-travail": ("un fichier ou une commande de l'espace de travail", Trust.INTERNE),
    "mémoire": ("la mémoire", Trust.INTERNE),
    "mail-en-tête": ("l'en-tête d'un mail", Trust.INTERNE),
    "mail": ("le contenu d'un mail", Trust.NON_FIABLE),
    "web": ("une page ou une recherche web", Trust.NON_FIABLE),
    "pièce-jointe": ("une pièce jointe", Trust.NON_FIABLE),
    "agenda": ("un événement d'agenda", Trust.NON_FIABLE),
    "sous-agent": ("le compte rendu d'un sous-agent", Trust.NON_FIABLE),
    "crew": ("un événement reçu de l'extérieur", Trust.NON_FIABLE),
    "connecteur-relayé": ("un connecteur relayé", Trust.NON_FIABLE),
    "mcp": ("un outil MCP tiers", Trust.NON_FIABLE),
    "contexte-reçu": ("un contexte reçu d'un autre fil", Trust.NON_FIABLE),
    "outil": ("le résultat d'un outil", Trust.NON_FIABLE),
}

MAIL_TOOLS = frozenset({"mail_search", "mail_read"})
WEB_TOOLS = frozenset({"web_search", "web_fetch"})
DELEGATE_TOOLS = frozenset({"delegate", "delegate_status"})
MEMORY_TOOLS = frozenset({"memory_search", "memory_get", "recall_context"})
WORKSPACE_TOOLS = frozenset({"read_file", "search_files", "write_file", "patch", "terminal", "process_start",
                             "process_list", "process_logs", "process_stop", "todo", "ssh_run", "checkpoint_create",
                             "checkpoint_list", "checkpoint_restore"})
# Champs d'en-tête d'un résultat de mail (JSON du connecteur, nokker/connectors/mail.py) : les adresses que
# l'utilisateur désigne par « réponds-lui », pas un texte où un tiers glisse une consigne.
MAIL_HEADER_KEYS = frozenset({"from", "to", "cc", "reply_to", "sender"})
# Une commande qui va chercher sur le réseau rend du web, pas de l'espace de travail.
_NETWORK_CMD = re.compile(r"(?i)\b(?:curl|wget|http|https?://|lynx|links|aria2c)\b")

SEND_TOOLS = frozenset({"mail_send"})
FETCH_TOOLS = frozenset({"web_fetch", "browser_navigate"})
EXEC_TOOLS = frozenset({"terminal", "process_start", "ssh_run"})
SEARCH_TOOLS = frozenset({"web_search"})
# Sources dont une valeur ne doit pas sortir dans la requête d'une recherche web : tout ce qui est lu, sauf ce que
# l'utilisateur a écrit, la liste autorisée, la consigne d'un parent et le web lui-même (déjà public).
_PUBLIC_SOURCES = frozenset({"utilisateur", "liste-autorisée", "délégation", "web"})
RECIPIENT_KEYS = ("to", "cc", "bcc", "recipient", "recipients", "email", "address", "destinataire", "destinataires",
                  "attendees")

# Une décision par famille : « carte » (HITL, carte forte) ou « refus » (DENY). Défauts du prototype.
DEFAULT_DECISIONS = {"envoi": "carte", "fetch": "refus", "exec": "carte", "recherche": "carte"}

# ─────────────────────────────── atomes ───────────────────────────────

_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+")
_URL = re.compile(r"\b(?:https?|ftp)://[^\s\"'<>`()\[\]{}|\\]+")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_DOMAIN = re.compile(r"(?<![\w@.\-/])(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?![\w\-])")
_PATH = re.compile(r"(?<![\w.~/:\-])(?:~/|/)[\w.\-]+(?:/[\w.\-]+)*/?")
_TOKEN = re.compile(r"[a-z0-9]{3,}")
_BLOCK = re.compile(r'<untrusted-data kind="([^"]*)" id="([^"]+)">\n?(.*?)\n?</untrusted-data id="\2">', re.DOTALL)
_FILE_BLOCK = re.compile(r"\[Fichier : [^\n]*\]\n(.*?)\n\[/Fichier\]", re.DOTALL)
_DIR_BLOCK = re.compile(r"\[Dossier : [^\n]*\]\n(.*?)\n\[/Dossier\]", re.DOTALL)
# Extensions de fichier courantes : `main.py` ou `notes.md` ne sont pas des hôtes.
_FILE_EXT = frozenset(["py", "js", "ts", "tsx", "jsx", "md", "txt", "json", "yaml", "yml", "sh", "toml", "cfg", "ini", "lock", "log", "html", "htm", "css", "csv", "xml", "rs", "go", "java", "c", "h", "cpp", "hpp", "rb", "php", "sql", "env", "pdf", "png", "jpg", "jpeg", "gif", "svg", "zip", "gz", "tar", "tgz", "bz2", "xz", "whl", "mjs", "cjs", "vue", "lua", "pl", "r", "kt", "swift", "scala", "bak", "tmp", "out", "pem", "key", "crt", "conf", "service"])

# Mots de structure d'une adresse web (chemin, paramètres) : ils ne portent aucune donnée de l'utilisateur.
URL_WORDS = frozenset(["search", "query", "recherche", "suche", "page", "pages", "index", "html", "htm", "php", "aspx", "jsp", "lang", "api", "results", "result", "find", "products", "product", "item", "items", "category", "categories", "tag", "tags", "www", "utm", "source", "medium", "sort", "order", "filter", "view", "list"])

ATOM_WORDS = {"email": "l'adresse", "host": "l'hôte", "url": "l'adresse web", "ip": "l'adresse IP",
              "path": "le chemin", "command": "la commande entière", "value": "la valeur"}


def norm(text: str) -> str:
    """Forme de comparaison : NFKC, casse repliée, accents retirés (« électrique » = « electrique »)."""
    t = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text or "").casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_url(u: str) -> str:
    return u.rstrip(".,;:!?'\")]}>")


def host_of(url: str) -> str:
    rest = url.split("://", 1)[-1]
    host = re.split(r"[/?#]", rest, maxsplit=1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    return host.removeprefix("www.")


def atoms_of(text: str) -> dict[str, set[str]]:
    """Valeurs « critiques » d'un texte déjà normalisé (`norm`) : adresses mail, URL, hôtes, IP, chemins absolus."""
    out: dict[str, set[str]] = {"email": set(), "url": set(), "host": set(), "ip": set(), "path": set()}
    for m in _URL.finditer(text):
        u = _strip_url(m.group(0))
        out["url"].add(u)
        h = host_of(u)
        if h:
            (out["ip"] if _IPV4.fullmatch(h) else out["host"]).add(h)
    rest = _URL.sub(" ", text)
    for m in _EMAIL.finditer(rest):
        e = m.group(0).strip(".")
        out["email"].add(e)
        out["host"].add(e.rsplit("@", 1)[1])
    rest = _EMAIL.sub(" ", rest)
    out["ip"].update(_IPV4.findall(rest))
    for m in _DOMAIN.finditer(rest):
        d = m.group(0).strip(".")
        if d.rsplit(".", 1)[-1] not in _FILE_EXT and not _IPV4.fullmatch(d):
            out["host"].add(d.removeprefix("www."))
    for m in _PATH.finditer(rest):
        p = m.group(0).rstrip("/")
        if len(p) >= 4 and p.count("/") >= 1:
            out["path"].add(p)
    return out


# ─────────────────────────────── registre ───────────────────────────────


@dataclass
class Segment:
    source: str
    trust: Trust
    origin: str  # « une page ou une recherche web (web_fetch, tour 3) » : jamais de texte de la source
    thread_id: str
    seq: int
    text: str  # normalisé et réduit (espaces)
    atoms: dict[str, set[str]]
    via: tuple[str, ...] = ()  # sous-agent : les sources non fiables qu'il a lues (propagation)


@dataclass
class _Root:
    segments: list[Segment] = field(default_factory=list)
    seq: int = 0
    marks: dict[tuple[str, str], int] = field(default_factory=dict)  # (fil, id d'appel) → seq avant l'appel
    turns_seen: set[tuple[str, int]] = field(default_factory=set)


def root_of(thread_id: str) -> str:
    return (thread_id or "").split("/sa-", 1)[0]


@dataclass
class Finding:
    arg: str
    kind: str  # email | host | url | ip | path | command | value
    value: str
    origins: list[Segment]  # vide = origine inconnue (mode strict)
    extra: int = 0  # web_fetch : éléments de l'adresse absents de la source de l'hôte


@dataclass
class ProvenanceGuard:
    """Étiquetage des textes du tour, propagation par fil racine, et politique sur 3 familles d'outils sensibles.

    Branché sur `Kernel.provenance` (None = éteint). Appels du noyau : `begin_turn(turn)` au début du tour,
    `check(call, spec, turn) -> list[Verdict]` avant chaque appel d'outil (à mettre AVANT les verdicts des portes :
    la règle nommée sur la carte est la sienne), `record(turn, call, spec, result)` après chaque résultat exécuté."""

    strict: bool = False  # origine inconnue d'un destinataire / hôte = traitée comme non fiable
    workspace_trusted: bool = True  # False : espace de travail et mémoire comptent comme non fiables
    decisions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DECISIONS))
    allow_addresses: frozenset[str] = frozenset()
    allow_domains: frozenset[str] = frozenset()
    max_threads: int = 256
    max_segments: int = 400
    max_chars: int = 200_000
    _roots: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ── registre ──

    def _root(self, thread_id: str) -> _Root:
        key = root_of(thread_id)
        r = self._roots.get(key)
        if r is None:
            r = self._roots[key] = _Root()
            while len(self._roots) > self.max_threads:
                self._roots.popitem(last=False)
        else:
            self._roots.move_to_end(key)
        return r

    def _trust(self, source: str) -> Trust:
        t = SOURCES.get(source, SOURCES["outil"])[1]
        if t is Trust.INTERNE and not self.workspace_trusted and source in ("espace-de-travail", "mémoire"):
            return Trust.NON_FIABLE
        return t

    def _add(self, thread_id: str, source: str, origin: str, text: str, via: tuple[str, ...] = (),
             trust: Trust | None = None) -> None:
        if not text or not text.strip():
            return
        t = _collapse(norm(text[: self.max_chars]))
        with self._lock:
            r = self._root(thread_id)
            r.seq += 1
            r.segments.append(Segment(source=source, trust=trust or self._trust(source), origin=origin,
                                      thread_id=thread_id, seq=r.seq, text=t, atoms=atoms_of(t), via=via))
            if len(r.segments) > self.max_segments:
                # Les demandes de l'utilisateur ne sortent jamais du registre (elles blanchissent) ; le reste, du plus
                # ancien au plus récent.
                drop = next((i for i, s in enumerate(r.segments) if s.trust is not Trust.FIABLE), None)
                if drop is not None:
                    r.segments.pop(drop)

    def begin_turn(self, turn: Any) -> None:
        """Étiquette ce qui entre avec le tour : le texte de l'utilisateur (sans ses blocs `<untrusted-data>`), les
        blocs eux-mêmes, les pièces jointes, le contexte reçu d'un autre fil. Sur un fil de sous-agent, la consigne est
        écrite par le modèle parent : `délégation`, interne (ni preuve ni teinte : la teinte vient du registre commun)."""
        tid = str(getattr(turn, "thread_id", "") or "")
        idx = int(getattr(turn, "index", 0) or 0)
        with self._lock:
            r = self._root(tid)
            if (tid, idx) in r.turns_seen:
                return
            r.turns_seen.add((tid, idx))
        text = str(getattr(getattr(turn, "input", None), "text", "") or "")
        at = f"tour {idx}"
        child = "/sa-" in tid
        for kind, _id, body in _BLOCK.findall(text):
            src = "web" if kind == "url" else "crew"
            self._add(tid, src, f"{SOURCES[src][0]} ({kind}, {at})", body)
        own = _BLOCK.sub(" ", text)
        if child or getattr(turn, "channel", "") == "subagent":
            self._add(tid, "délégation", f"{SOURCES['délégation'][0]} ({at})", own)
        else:
            self._add(tid, "utilisateur", f"{SOURCES['utilisateur'][0]} ({at})", own)
        attached = str(getattr(turn, "attached", "") or "")
        if attached:
            for kind, _id, body in _BLOCK.findall(attached):
                self._add(tid, "web", f"{SOURCES['web'][0]} (pièce jointe url, {at})", body)
            rest = _BLOCK.sub(" ", attached)
            for body in _DIR_BLOCK.findall(rest):
                self._add(tid, "espace-de-travail", f"{SOURCES['espace-de-travail'][0]} (dossier joint, {at})", body)
            for body in _FILE_BLOCK.findall(rest):
                self._add(tid, "pièce-jointe", f"{SOURCES['pièce-jointe'][0]} (fichier, {at})", body)
        for i, block in enumerate(getattr(turn, "injected", None) or []):
            self._add(tid, "contexte-reçu", f"{SOURCES['contexte-reçu'][0]} ({at})", str(block))

    def source_of(self, name: str, spec: Any, arguments: dict[str, Any]) -> str:
        if name in MAIL_TOOLS:
            return "mail"
        if name in WEB_TOOLS or name.startswith("browser"):
            return "web"
        if name in DELEGATE_TOOLS:
            return "sous-agent"
        if name.startswith("calendar_"):
            return "agenda"
        if name in MEMORY_TOOLS:
            return "mémoire"
        if getattr(spec, "locality", None) == "cloud":
            return "connecteur-relayé"
        if name in WORKSPACE_TOOLS:
            if name in ("terminal", "process_start") and _NETWORK_CMD.search(str(arguments.get("command", ""))):
                return "web"
            return "espace-de-travail"
        if str(getattr(spec, "source", "") or "").startswith("mcp:"):
            return "mcp"
        return "outil"

    def record(self, turn: Any, call: Any, spec: Any, result: Any) -> None:
        """Étiquette un résultat d'outil exécuté (le résultat CAVIARDÉ, complet, avant réservoir)."""
        tid = str(getattr(turn, "thread_id", "") or "")
        idx = int(getattr(turn, "index", 0) or 0)
        name = str(getattr(call, "name", "") or "")
        content = str(getattr(result, "content", "") or "")
        args = getattr(call, "arguments", None) or {}
        source = self.source_of(name, spec, args)
        at = f"{name}, tour {idx}" + (", sous-agent" if "/sa-" in tid else "")
        if source == "mail":
            head, body = _split_mail(content)
            self._add(tid, "mail-en-tête", f"{SOURCES['mail-en-tête'][0]} ({at})", head)
            self._add(tid, "mail", f"{SOURCES['mail'][0]} ({at})", body)
            return
        if source == "sous-agent":
            via = self._child_sources(tid, str(getattr(call, "id", "") or ""))
            # Multi-CaMeL : la teinte d'un compte rendu est celle de ce que l'enfant a lu. Un enfant qui n'a lu que
            # l'espace de travail rend un compte rendu interne ; un enfant qui a lu le web rend un compte rendu teinté.
            trust = Trust.NON_FIABLE if via else Trust.INTERNE
            read = f", qui avait lu : {', '.join(SOURCES[v][0] for v in via)}" if via else ""
            self._add(tid, "sous-agent", f"{SOURCES['sous-agent'][0]} ({at}{read})", content, via=via, trust=trust)
            return
        self._add(tid, source, f"{SOURCES[source][0]} ({at})", content)

    def _child_sources(self, thread_id: str, call_id: str) -> tuple[str, ...]:
        prefix = thread_id + "/sa-"
        with self._lock:
            r = self._root(thread_id)
            since = r.marks.pop((thread_id, call_id), 0)
            seen: list[str] = []
            for s in r.segments:
                if s.thread_id.startswith(prefix) and s.seq > since and s.trust is Trust.NON_FIABLE:
                    for src in (s.via or (s.source,)):
                        if src not in seen:
                            seen.append(src)
        return tuple(seen)

    # ── politique ──

    def family(self, name: str, spec: Any, arguments: dict[str, Any]) -> str | None:
        if name in SEND_TOOLS or getattr(spec, "effect", None) is Effect.SEND_EXTERNAL:
            return "envoi"
        if name in FETCH_TOOLS:
            return "fetch"
        if name in EXEC_TOOLS:
            return "exec"
        if name in SEARCH_TOOLS:
            return "recherche"
        return None

    def check(self, call: Any, spec: Any, turn: Any) -> list[Verdict]:
        tid = str(getattr(turn, "thread_id", "") or "")
        name = str(getattr(call, "name", "") or "")
        args = getattr(call, "arguments", None) or {}
        with self._lock:
            r = self._root(tid)
            if name in DELEGATE_TOOLS:  # repère : ce que les enfants liront à partir d'ici teinte leur compte rendu
                r.marks[(tid, str(getattr(call, "id", "") or ""))] = r.seq
                while len(r.marks) > 64:
                    r.marks.pop(next(iter(r.marks)))
            segments = list(r.segments)
        fam = self.family(name, spec, args)
        if fam is None:
            return []
        if fam == "envoi":
            findings = self._check_send(args, segments)
        elif fam == "fetch":
            findings = self._check_fetch(args, segments)
        elif fam == "recherche":
            findings = self._check_search(args, segments)
        else:
            findings = self._check_exec(args, segments)
        if not findings:
            return []
        mode = self.decisions.get(fam, DEFAULT_DECISIONS[fam])
        reason = explain(fam, findings, mode)
        if mode == "refus":
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=f"provenance:{fam}",
                            reason=reason, subject=getattr(call, "id", None), code="provenance")]
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=f"provenance:{fam}", reason=reason,
                        subject=getattr(call, "id", None), strength="strong")]

    # Où une valeur apparaît : (blanchie ?, segments non fiables, vue ailleurs ?)
    def _locate(self, kind: str, value: str, segments: list[Segment]) -> tuple[bool, list[Segment], bool]:
        if kind == "email" and (value in self.allow_addresses or value.rsplit("@", 1)[-1] in self.allow_domains):
            return True, [], True
        if kind == "host" and any(value == d or value.endswith("." + d) for d in self.allow_domains):
            return True, [], True
        hits = [s for s in segments if _contains(s, kind, value)]
        trusted = any(s.trust is Trust.FIABLE for s in hits)
        return trusted, [s for s in hits if s.trust is Trust.NON_FIABLE], bool(hits)

    def _verdict_atom(self, arg: str, kind: str, value: str, segments: list[Segment], strict_kind: bool) \
            -> Finding | None:
        trusted, bad, seen = self._locate(kind, value, segments)
        if trusted:
            return None
        if bad:
            return Finding(arg=arg, kind=kind, value=value, origins=bad)
        if self.strict and strict_kind and not seen:
            return Finding(arg=arg, kind=kind, value=value, origins=[])
        return None

    def _check_send(self, args: dict[str, Any], segments: list[Segment]) -> list[Finding]:
        out: list[Finding] = []
        for key in RECIPIENT_KEYS:
            raw = args.get(key)
            values = raw if isinstance(raw, list) else [raw] if raw else []
            for v in values:
                text = norm(str(v))
                emails = sorted(set(_EMAIL.findall(text)))
                if not emails and len(text.strip()) >= 4:
                    f = self._verdict_atom(key, "value", text.strip(), segments, True)
                    if f:
                        out.append(f)
                for e in emails:
                    f = self._verdict_atom(key, "email", e.strip("."), segments, True)
                    if f:
                        out.append(f)
        return out

    def _check_fetch(self, args: dict[str, Any], segments: list[Segment]) -> list[Finding]:
        raw = args.get("urls", args.get("url"))
        urls = raw if isinstance(raw, list) else [raw] if raw else []
        out: list[Finding] = []
        for u in urls:
            url = _strip_url(norm(str(u)).strip())
            host = host_of(url)
            if not host:
                continue
            trusted, bad, seen = self._locate("host", host, segments)
            if trusted:
                continue
            if any(url in s.atoms["url"] for s in segments):
                continue  # lien recopié tel quel : il ne porte aucune donnée qui ne soit déjà dans sa source
            if bad and not any(s.trust is Trust.INTERNE for s in segments if _contains(s, "host", host)):
                # Ce que le modèle a AJOUTÉ à l'adresse fournie par la source (le plus long lien de la source qui en
                # est le début ; à défaut, tout ce qui suit l'hôte) : seuls les mots de l'utilisateur y sont admis.
                # Une donnée lue ailleurs (un solde, un agenda, un fichier) collée à un lien d'un tiers est une fuite.
                bases = [u for s in bad for u in s.atoms["url"] if host_of(u) == host and url.startswith(u)]
                added = url[len(max(bases, key=len)):] if bases else url.split(host, 1)[-1]
                said = " ".join(s.text for s in segments if s.trust is Trust.FIABLE)
                extra = [t for t in _TOKEN.findall(added) if t not in said and t not in URL_WORDS]
                if extra:
                    out.append(Finding(arg="urls", kind="url", value=url, origins=bad, extra=len(extra)))
            elif self.strict and not seen:
                out.append(Finding(arg="urls", kind="url", value=url, origins=[]))
        return out

    def _check_search(self, args: dict[str, Any], segments: list[Segment]) -> list[Finding]:
        """La requête d'une recherche web sort de la machine : un mot qu'on ne trouve QUE dans une source lue (mail,
        mémoire, fichier, résultat d'outil), jamais dans une demande de l'utilisateur, est une valeur qui s'en va.
        Limite : recopie littérale seulement (paraphrase, encodage, fragment passent)."""
        query = norm(str(args.get("query", "") or ""))
        words = [t for t in dict.fromkeys(_TOKEN.findall(query)) if t not in URL_WORDS]
        said: set[str] = set()
        for s in segments:
            if s.trust is Trust.FIABLE:
                said.update(_TOKEN.findall(s.text))
        words = [t for t in words if t not in said]
        if not words:
            return []
        bad: list[Segment] = []
        taint: list[str] = []
        for s in segments:
            if s.source in _PUBLIC_SOURCES or s.trust is Trust.FIABLE:
                continue
            have = set(_TOKEN.findall(s.text))
            hit = [t for t in words if t in have]
            if hit:
                bad.append(s)
                taint.extend(t for t in hit if t not in taint)
        if not bad:
            return []
        return [Finding(arg="query", kind="value", value=" ".join(taint), origins=bad)]

    def _check_exec(self, args: dict[str, Any], segments: list[Segment]) -> list[Finding]:
        out: list[Finding] = []
        command = norm(str(args.get("command", "") or ""))
        found = atoms_of(command)
        host_arg = norm(str(args.get("host", "") or "")).strip()
        if host_arg:  # ssh_run : la machine visée
            f = self._verdict_atom("host", "ip" if _IPV4.fullmatch(host_arg) else "host", host_arg, segments, True)
            if f:
                out.append(f)
        for kind in ("url", "host", "ip", "email", "path"):
            for v in sorted(found[kind]):
                if kind == "url":
                    continue  # couvert par son hôte (un lien recopié d'une page reste un hôte de cette page)
                f = self._verdict_atom("command", kind, v, segments, kind in ("host", "ip"))
                if f:
                    out.append(f)
        whole = _collapse(command)
        if len(whole) >= 12 and not any(f.arg == "command" for f in out):
            hits = [s for s in segments if whole in s.text]
            if hits and not any(s.trust is Trust.FIABLE for s in hits):
                bad = [s for s in hits if s.trust is Trust.NON_FIABLE]
                if bad:  # affichée telle que le modèle l'a écrite (la forme normalisée change la casse)
                    raw = _collapse(str(args.get("command", "") or ""))
                    out.append(Finding(arg="command", kind="command", value=raw, origins=bad))
        return out


def _contains(s: Segment, kind: str, value: str) -> bool:
    if kind == "host":
        return any(value == h or value.endswith("." + h) for h in s.atoms["host"])
    if kind in ("email", "ip", "url"):
        return value in s.atoms[kind]
    if kind == "path":
        return value in s.atoms["path"] or value in s.text
    return value in s.text  # value, command


def _split_mail(content: str) -> tuple[str, str]:
    """(en-têtes, reste) d'un résultat JSON du connecteur mail ; un texte non JSON est tout entier du contenu."""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return "", content
    head: list[str] = []
    body: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in MAIL_HEADER_KEYS and isinstance(v, (str, list)):
                    head.append(v if isinstance(v, str) else " ".join(map(str, v)))
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            body.append(str(x))

    walk(data)
    return "\n".join(head), "\n".join(body)


# ─────────────────────────────── explications (texte vu par le modèle et sur la carte) ───────────────────────────────

# Sujet de la règle, avec son genre pour l'accord de la fin de phrase.
RULE_SUBJECT = {
    "envoi": ("Un destinataire d'origine non fiable", "m"),
    "fetch": ("Une adresse web composée vers un hôte d'origine non fiable", "f"),
    "exec": ("Une commande dont un élément vient d'une source non fiable", "f"),
    "recherche": ("Une requête de recherche web contenant une valeur lue ailleurs", "f"),
}
RULE_SUBJECT_STRICT = {
    "envoi": ("Un destinataire d'origine inconnue", "m"),
    "fetch": ("Une adresse web d'origine inconnue", "f"),
    "exec": ("Une commande vers un hôte d'origine inconnue", "f"),
    "recherche": ("Une requête de recherche web d'origine inconnue", "f"),
}
TAIL = {"carte": " passe par une carte forte.", "refus": {"m": " est refusé.", "f": " est refusée."}}
MAX_FINDINGS = 3


def _clip(v: str, n: int = 80) -> str:
    """Valeur d'origine non fiable recopiée dans la raison : une ligne, sans balise ni crochet (`qa/untrusted.inline`),
    bornée."""
    from nokker.qa.untrusted import inline

    return inline(v, n)


def _origins(segs: list[Segment], de: bool = False) -> str:
    seen: list[str] = []
    for s in segs:
        if s.origin not in seen:
            seen.append(s.origin)
    shown = [_clip(_de(o) if de else o, 120) for o in seen[:2]]
    return " et ".join(shown) + (f" (et {len(seen) - 2} autre(s))" if len(seen) > 2 else "")


def _de(phrase: str) -> str:
    """« de » + groupe nominal, avec l'élision et la contraction du français (« du contenu », « d'une page »)."""
    for art, out in (("le ", "du "), ("les ", "des "), ("un ", "d'un "), ("une ", "d'une "), ("l'", "de l'"),
                     ("la ", "de la ")):
        if phrase.startswith(art):
            return out + phrase[len(art):]
    return "de " + phrase


def explain(family: str, findings: list[Finding], mode: str) -> str:
    parts: list[str] = []
    for f in findings[:MAX_FINDINGS]:
        what = ATOM_WORDS.get(f.kind, "la valeur")
        if not f.origins:
            parts.append(f"{what} « {_clip(f.value)} » (argument {_clip(f.arg, 40)}) n'apparaît ni dans une demande de "
                         "l'utilisateur ni dans une source lue : origine inconnue")
        elif f.kind == "url" and family == "fetch":
            parts.append(f"{what} « {_clip(f.value)} » vise un hôte connu seulement par {_origins(f.origins)}, source "
                         f"non fiable, et y ajoute {f.extra} élément(s) qui ne viennent pas des demandes de "
                         "l'utilisateur")
        else:
            parts.append(f"{what} « {_clip(f.value)} » (argument {_clip(f.arg, 40)}) vient {_origins(f.origins, de=True)}, source non "
                         "fiable, et n'apparaît dans aucune demande de l'utilisateur")
    if len(findings) > MAX_FINDINGS:
        parts.append(f"et {len(findings) - MAX_FINDINGS} autre(s) valeur(s)")
    unknown = all(not f.origins for f in findings)
    subject, gender = (RULE_SUBJECT_STRICT if unknown else RULE_SUBJECT)[family]
    tail = TAIL["carte"] if mode == "carte" else TAIL["refus"][gender]
    body = " ; ".join(parts)
    return f"provenance : {body}. {subject}{tail}"


def build_provenance(settings: dict[str, Any] | None, invariants: Any = None) -> ProvenanceGuard | None:
    """Le garde depuis la config (`provenance: {enabled, strict, workspace_trusted, decisions}`). Éteint (défaut) :
    None, le noyau ne fait rien de plus. La liste autorisée des invariants (`Allowlist`) blanchit ses adresses."""
    s = dict(settings or {})
    if not s.get("enabled"):
        return None
    decisions = dict(DEFAULT_DECISIONS)
    for k, v in dict(s.get("decisions") or {}).items():
        if k in decisions and v in ("carte", "refus"):
            decisions[k] = v
    allow = getattr(invariants, "allowlist", None)
    return ProvenanceGuard(strict=bool(s.get("strict", False)),
                           workspace_trusted=bool(s.get("workspace_trusted", True)), decisions=decisions,
                           allow_addresses=frozenset(getattr(allow, "addresses", ()) or ()),
                           allow_domains=frozenset(getattr(allow, "domains", ()) or ()))


__all__ = [
    "DEFAULT_DECISIONS",
    "SOURCES",
    "Finding",
    "ProvenanceGuard",
    "Segment",
    "Trust",
    "atoms_of",
    "build_provenance",
    "explain",
    "root_of",
]
