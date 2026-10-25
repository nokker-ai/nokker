"""nokker/tools/mcp_config.py — serveurs MCP ajoutés par l'utilisateur dans Lokkal (lot X16, pack `mcp-builder`).

Le modèle propose, l'humain installe : `mcp_server_add` et `mcp_server_remove` sont DESTRUCTIVE (carte forte), et
le fichier qu'ils écrivent (`<état>/mcp-servers.yaml`, 0600) n'est lu qu'au démarrage du noyau, fusionné APRÈS les
serveurs de `config.yaml` (un nom déjà pris par la config l'emporte). Rien ne change donc avant un redémarrage que
l'utilisateur fait lui-même.

Identifiants : PAR NOM seulement (LOKKAL.md §7.10). `token` doit être une référence `secret:NOM` ; une valeur d'en-tête
est une référence ou un court libellé qui ne ressemble pas à une clé. Une valeur collée est refusée, jamais écrite.
Adresse : https, ou http vers la boucle locale ou un nom de service sans point (comme le coffre OpenBao).

Outils en LECTURE (sans pack) : par défaut tout outil d'un serveur ajouté est une écriture (il demande). L'utilisateur
peut nommer ceux qui ne font que lire (`read_tools`, à l'ajout, par `mcp_server_read_tools` ou par la route locale
`PUT /v1/local/mcp-servers/{nom}/read-tools`) ; au démarrage, `ReadDeclaredSource` les rend READ — jamais un outil
que le noyau, la config ou un pack classe déjà (une déclaration ne baisse rien), et jamais un outil dont le NOM ou la
DESCRIPTION annonce une écriture évidente (`write_words` : send, delete, create, post, push, rm…) sans que ce nom
soit confirmé (`confirm_writes`) sous une carte FORTE : plancher `floor`, qu'aucun mode ni aucune règle ne lève, et
que la route locale ne remplace pas (elle refuse ces noms).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, ToolCall, ToolResult, Verdict

log = logging.getLogger("nokker.tools.mcp_config")

FILE_NAME = "mcp-servers.yaml"
MAX_SERVERS = 32
MAX_HEADERS = 8
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_PLAIN_HEADER = re.compile(r"^[A-Za-z0-9 _.,:/=-]{1,64}$")
_REF = re.compile(r"^secret:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
_TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_READ_TOOLS = 64
READ_RULE = "mcp:read_tools_write_names"

# Verbes d'écriture évidente : un outil dont le nom ou la description en porte un n'est jamais déclaré en lecture sans
# confirmation explicite sous carte forte. Liste volontairement large (un faux positif coûte une carte, un faux
# négatif une écriture sans carte) ; comparés au mot, avec les flexions usuelles (sends, created, posting).
WRITE_VERBS = frozenset({
    "send", "sent", "delete", "del", "remove", "rm", "unlink", "write", "wrote", "written", "create", "update",
    "upsert", "post", "push", "put", "patch", "insert", "upload", "publish", "schedule", "pay", "charge", "refund",
    "transfer", "exec", "execute", "run", "ran", "kill", "drop", "truncate", "set", "move", "mv", "edit", "modify",
    "reply", "forward", "invite", "approve", "reject", "cancel", "submit", "deploy", "restart", "stop", "rename",
    "archive", "trash", "purge", "wipe", "reset", "grant", "revoke", "add", "save", "commit", "merge", "book", "order",
    "buy", "bought", "sell", "sold", "like", "follow", "unfollow", "comment", "share", "assign", "close", "mark",
    "sync", "apply", "enable", "disable", "install", "uninstall", "register", "subscribe", "unsubscribe", "notify",
    "tweet", "reboot", "shutdown", "mutate", "change", "clear", "destroy", "erase", "overwrite", "replace", "chmod",
    "chown", "mkdir", "accept", "decline", "launch", "trigger", "import", "transmit", "dispatch", "broadcast"})
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _stems(word: str) -> set[str]:
    w = word.lower()
    out = {w}
    if w.endswith("ing") and len(w) > 4:
        out |= {w[:-3], w[:-3] + "e", w[:-4]}
    if w.endswith("ed") and len(w) > 3:
        out |= {w[:-2], w[:-1], w[:-3]}
    if w.endswith("es") and len(w) > 3:
        out |= {w[:-2], w[:-1]}
    elif w.endswith("s") and len(w) > 2:
        out.add(w[:-1])
    return out


def write_words(text: str) -> list[str]:
    """Verbes d'écriture évidente trouvés dans un nom d'outil (`deleteIssue`, `git_push`) ou une description."""
    found: list[str] = []
    for word in _WORD.findall(str(text or "")):
        hit = sorted(_stems(word) & WRITE_VERBS)
        if hit and hit[0] not in found:
            found.append(hit[0])
    return found

_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("mcp_server_list", "Liste les serveurs MCP ajoutés dans Lokkal (nom, adresse, NOMS des secrets, manquants).",
     Effect.READ, {"type": "object", "properties": {}}),
    ("mcp_server_add", "Ajoute un serveur MCP (HTTP) à la config de Lokkal, identifiants PAR NOM du coffre "
     "(token: secret:NOM). Carte d'approbation ; actif au prochain redémarrage du noyau.", Effect.DESTRUCTIVE, {
         "type": "object", "required": ["name", "url"], "properties": {
             "name": {"type": "string", "description": "nom court du serveur : minuscules, chiffres, tirets"},
             "url": {"type": "string", "description": "adresse MCP streamable-HTTP (https://…/mcp, ou http:// en "
                                                      "boucle locale)"},
             "token": {"type": "string", "description": "secret:NOM — envoyé en « Authorization: Bearer »"},
             "headers": {"type": "object", "description": "en-têtes : valeurs secret:NOM ou courts libellés"},
             "purpose": {"type": "string", "description": "à quoi sert ce serveur (une phrase)"},
             "read_tools": {"type": "array", "items": {"type": "string"},
                            "description": "facultatif : outils qui ne font QUE lire (sans carte) ; les autres "
                                           "demandent"},
             "confirm_writes": {"type": "array", "items": {"type": "string"},
                                "description": "outils de read_tools au nom d'écriture (send, delete, post…) que "
                                               "l'utilisateur confirme en lecture (carte forte)"}}}),
    ("mcp_server_read_tools", "Déclare les outils en LECTURE d'un serveur MCP ajouté (remplace la liste ; vide = "
     "tout redevient écriture). Carte ; actif au prochain redémarrage du noyau.", Effect.DESTRUCTIVE, {
         "type": "object", "required": ["name", "read_tools"], "properties": {
             "name": {"type": "string"},
             "read_tools": {"type": "array", "items": {"type": "string"}},
             "confirm_writes": {"type": "array", "items": {"type": "string"},
                                "description": "outils au nom d'écriture évidente confirmés en lecture (carte "
                                               "forte)"}}}),
    ("mcp_server_remove", "Retire un serveur MCP ajouté dans Lokkal (jamais un serveur de config.yaml). Carte.",
     Effect.DESTRUCTIVE, {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}),
)
MCP_CONFIG_TOOLS = tuple(name for name, *_ in _SPECS)


class McpConfigError(ValueError):
    pass


def _looks_like_value(text: str) -> bool:
    from nokker.agents.spec import _looks_like_value as looks

    return looks(text)


def check_url(url: str) -> str:
    url = str(url or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password:
        raise McpConfigError("adresse refusée : pas d'identifiant dans l'URL (utilise token: secret:NOM)")
    if parts.query and any(_looks_like_value(v) for v in re.split(r"[&=]", parts.query)):
        raise McpConfigError("adresse refusée : la requête porte ce qui ressemble à une clé (utilise secret:NOM)")
    if parts.scheme == "https" and host:
        return url
    if parts.scheme == "http" and host and (host in _LOOPBACK or "." not in host):
        return url
    raise McpConfigError("adresse refusée : https://…, ou http:// vers la boucle locale ou un service sans point")


def secret_names(entry: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for value in [entry.get("token"), *(entry.get("headers") or {}).values()]:
        m = _REF.match(str(value or ""))
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def check_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Entrée validée et normalisée (jamais de valeur de secret)."""
    name = str(raw.get("name") or "").strip()
    if not _NAME.match(name):
        raise McpConfigError("nom refusé : minuscules, chiffres et tirets, 40 au plus")
    entry: dict[str, Any] = {"name": name, "url": check_url(str(raw.get("url") or ""))}
    token = raw.get("token")
    if token not in (None, ""):
        if not _REF.match(str(token).strip()):
            raise McpConfigError("token refusé : on attend une référence secret:NOM du coffre, jamais une valeur "
                                 "(pose la valeur avec `nokker secret set NOM`)")
        entry["token"] = str(token).strip()
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
        raise McpConfigError(f"headers : un objet de {MAX_HEADERS} en-têtes au plus")
    clean: dict[str, str] = {}
    for key, value in headers.items():
        key, value = str(key).strip(), str(value).strip()
        if not _HEADER_NAME.match(key) or key.lower() in ("authorization", "cookie"):
            raise McpConfigError(f"en-tête refusé : {key[:40]!r} (Authorization passe par token: secret:NOM)")
        if not _REF.match(value) and (not _PLAIN_HEADER.match(value) or _looks_like_value(value)):
            raise McpConfigError(f"en-tête {key} : valeur secret:NOM attendue (une valeur en clair ressemble à une clé)")
        clean[key] = value
    if clean:
        entry["headers"] = clean
    purpose = " ".join(str(raw.get("purpose") or "").split())[:200]
    if purpose:
        entry["purpose"] = purpose
    reads, confirmed = check_read_tools(raw.get("read_tools"),
                                        raw["confirm_writes"] if "confirm_writes" in raw else raw.get("confirmed_writes"))
    if reads:
        entry["read_tools"] = reads
    if confirmed:
        entry["confirmed_writes"] = confirmed
    return entry


def _names_list(raw: Any, what: str) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_READ_TOOLS:
        raise McpConfigError(f"{what} : une liste de {MAX_READ_TOOLS} noms d'outils au plus")
    out: list[str] = []
    for x in raw:
        name = str(x).strip()
        if not _TOOL.match(name):
            raise McpConfigError(f"{what} : nom d'outil invalide {name[:40]!r}")
        if name not in out:
            out.append(name)
    return out


def check_read_tools(read_tools: Any, confirm_writes: Any = None) -> tuple[list[str], list[str]]:
    """(outils déclarés en lecture, noms d'écriture évidente confirmés). Un nom d'écriture évidente doit être confirmé
    (`confirm_writes`, qui ne nomme que ceux-là) : sinon refusé, rien n'est écrit."""
    reads = _names_list(read_tools, "read_tools")
    confirm = _names_list(confirm_writes, "confirm_writes")
    writes = [t for t in reads if write_words(t)]
    missing = [t for t in writes if t not in confirm]
    if missing:
        raise McpConfigError(
            f"read_tools : {', '.join(missing)} porte(nt) un nom d'écriture évidente ({', '.join(write_words(missing[0]))}"
            ") ; si l'outil ne fait vraiment que lire, nomme-le aussi dans confirm_writes : l'utilisateur le confirme "
            "sur une carte forte")
    extra = [t for t in confirm if t not in writes]
    if extra:
        raise McpConfigError(f"confirm_writes : seulement les outils de read_tools au nom d'écriture évidente "
                             f"({', '.join(extra)} n'en est pas un)")
    return reads, [t for t in writes if t in confirm]


def load_user_servers(path: Path) -> list[dict[str, Any]]:
    """Entrées valides de `mcp-servers.yaml` ; une entrée illisible est sautée (journalisée), jamais devinée."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    except (OSError, yaml.YAMLError) as e:
        log.warning("%s illisible, ignoré : %s", path, e)
        return []
    out: list[dict[str, Any]] = []
    for raw in (data.get("servers") or [])[:MAX_SERVERS] if isinstance(data, dict) else []:
        try:
            out.append(check_entry(raw if isinstance(raw, dict) else {}))
        except McpConfigError as e:
            log.warning("serveur MCP ignoré dans %s : %s", path, e)
    return out


def merge_servers(configured: list[dict[str, Any]], added: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serveurs de la config, puis ceux de l'utilisateur dont le nom n'est pas déjà pris."""
    taken = {str(s.get("name") or "") for s in configured}
    extra = []
    for s in added:
        if s["name"] in taken:
            log.warning("serveur MCP %s : nom déjà pris par config.yaml, entrée ajoutée ignorée", s["name"])
            continue
        taken.add(s["name"])
        extra.append({k: v for k, v in s.items() if k != "purpose"})
    return [*configured, *extra]


def set_read_tools(path: Path, name: str, read_tools: Any, confirm_writes: Any = None) -> dict[str, Any]:
    """Remplace les outils déclarés en lecture du serveur ajouté `name` (liste vide : tout redevient écriture). Rend
    l'entrée écrite ; `McpConfigError` si le serveur est inconnu ou la liste refusée (rien n'est écrit)."""
    servers = load_user_servers(path)
    target = str(name or "").strip()
    found = next((s for s in servers if s["name"] == target), None)
    if found is None:
        raise McpConfigError(f"serveur inconnu : {target!r} (seuls les serveurs ajoutés depuis une session se "
                             "règlent d'ici ; ceux de config.yaml se règlent dans config.yaml)")
    reads, confirmed = check_read_tools(read_tools, confirm_writes)
    entry = {k: v for k, v in found.items() if k not in ("read_tools", "confirmed_writes")}
    if reads:
        entry["read_tools"] = reads
    if confirmed:
        entry["confirmed_writes"] = confirmed
    _write(path, [entry if s["name"] == target else s for s in servers])
    return entry


def _write(path: Path, servers: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mcp-servers-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# Serveurs MCP ajoutés depuis une session (mcp_server_add). Identifiants par NOM du coffre.\n")
            yaml.safe_dump({"servers": servers}, f, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class McpConfigToolSource:
    """`ToolSource` : `mcp_server_list` / `mcp_server_add` / `mcp_server_remove` sur `path`. `known_secrets` rend les
    NOMS présents au coffre (jamais de valeur) pour signaler ce qui manque ; None = inconnu."""

    path: Path
    known_secrets: Callable[[], list[str]] | None = None
    configured: Callable[[], list[str]] | None = None  # noms des serveurs de config.yaml
    core: bool = False

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:mcp_config")
                for n, d, e, p in _SPECS]

    def _missing(self, names: list[str]) -> list[str] | None:
        if self.known_secrets is None:
            return None
        try:
            have = set(self.known_secrets())
        except Exception:  # noqa: BLE001 — coffre scellé ou absent : on ne sait pas
            return None
        return [n for n in names if n not in have]

    def floor(self, call: ToolCall, spec: Any, turn: Any) -> Verdict | None:
        """Plancher (`PermissionGates.floors`) : déclarer en LECTURE un outil au nom d'écriture évidente demande une
        carte FORTE qu'aucun mode, aucune règle `allow` ni aucun GO écrit ne lève ; sans `confirm_writes` qui le nomme,
        refusé tout de suite (pas de carte vouée à l'échec). Rien d'imposé pour une déclaration ordinaire."""
        if call.name not in ("mcp_server_add", "mcp_server_read_tools"):
            return None
        a = call.arguments or {}
        reads = a.get("read_tools")
        if not isinstance(reads, list):
            return None
        writes = [str(t) for t in reads if write_words(str(t))]
        if not writes:
            return None
        confirm = {str(t) for t in a.get("confirm_writes") or []} if isinstance(a.get("confirm_writes"), list) else set()
        server = str(a.get("name") or "")[:40]
        if not set(writes) <= confirm:
            return Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=READ_RULE, subject=call.id,
                           reason=f"{', '.join(writes)} : nom d'écriture évidente ; à déclarer en lecture seulement si "
                                  "l'utilisateur le confirme (confirm_writes, carte forte)")
        return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=READ_RULE, subject=call.id,
                       strength="strong",
                       reason=f"serveur MCP {server} : traiter comme LECTURE (sans carte) des outils dont le nom "
                              f"annonce une écriture : {', '.join(writes)}. Ne confirme que si tu sais qu'ils ne "
                              "modifient rien.")

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        a = arguments or {}
        try:
            servers = load_user_servers(self.path)
            if name == "mcp_server_list":
                items = []
                for s in servers:
                    item = {"name": s["name"], "url": s["url"], "secrets": secret_names(s)}
                    if s.get("purpose"):
                        item["purpose"] = s["purpose"]
                    for key in ("read_tools", "confirmed_writes"):
                        if s.get(key):
                            item[key] = list(s[key])
                    missing = self._missing(item["secrets"])
                    if missing:
                        item["missing_secrets"] = missing
                    items.append(item)
                return out({"servers": items, "configured": sorted(self.configured()) if self.configured else [],
                            "note": "les serveurs ajoutés sont actifs après un redémarrage du noyau ; leurs outils "
                                    "sont des écritures (ils demandent), sauf ceux de read_tools dont la description "
                                    "n'annonce pas d'écriture"})
            if name == "mcp_server_add":
                entry = check_entry(a)
                taken = set(self.configured()) if self.configured else set()
                if entry["name"] in taken or any(s["name"] == entry["name"] for s in servers):
                    return out(f"refusé : un serveur MCP s'appelle déjà {entry['name']!r} (retire-le d'abord)", True)
                if len(servers) >= MAX_SERVERS:
                    return out(f"refusé : {MAX_SERVERS} serveurs ajoutés au plus", True)
                _write(self.path, [*servers, entry])
                names = secret_names(entry)
                result: dict[str, Any] = {"added": entry["name"], "url": entry["url"], "secrets": names,
                                          "active": "au prochain redémarrage du noyau"}
                for key in ("read_tools", "confirmed_writes"):
                    if entry.get(key):
                        result[key] = list(entry[key])
                missing = self._missing(names)
                if missing:
                    result["missing_secrets"] = missing
                    result["next"] = "pose chaque valeur toi-même : nokker secret set " + " ; nokker secret set ".join(
                        missing)
                return out(result)
            if name == "mcp_server_read_tools":
                target = str(a.get("name") or "").strip()
                if self.configured and target in set(self.configured()) \
                        and not any(s["name"] == target for s in servers):
                    return out(f"refusé : {target!r} vient de config.yaml ; ses outils en lecture se déclarent dans "
                               "config.yaml (read_tools), pas d'ici", True)
                entry = set_read_tools(self.path, target, a.get("read_tools"), a.get("confirm_writes"))
                return out({"server": target, "read_tools": list(entry.get("read_tools") or []),
                            "confirmed_writes": list(entry.get("confirmed_writes") or []),
                            "active": "au prochain redémarrage du noyau"})
            if name == "mcp_server_remove":
                target = str(a.get("name") or "").strip()
                kept = [s for s in servers if s["name"] != target]
                if len(kept) == len(servers):
                    return out(f"rien à retirer : {target!r} n'est pas un serveur ajouté (config.yaml ne se modifie "
                               "pas d'ici)", True)
                _write(self.path, kept)
                return out({"removed": target, "active": "au prochain redémarrage du noyau"})
        except McpConfigError as e:
            return out(f"refusé : {e}", True)
        except OSError as e:
            return out(f"écriture impossible : {type(e).__name__}", True)
        return out(f"outil inconnu : {name}", True)


@dataclass
class ReadDeclaredSource:
    """Enveloppe d'un serveur MCP qui déclare des outils en lecture (`read_tools`) : leur effet devient READ, à trois
    conditions — l'effet résolu par la table est l'écriture PAR DÉFAUT (outil inconnu du noyau, sans surcharge de la
    config ni effet relevé par un pack, sans classement par arguments) ; et ni son nom ni la description que donne le
    serveur n'annoncent une écriture, sauf nom confirmé (`confirmed_writes`, carte forte). Une déclaration ne baisse
    donc jamais un effet connu ; la description du serveur (non fiable) ne sert qu'à REFUSER la levée."""

    inner: Any
    read_tools: list[str] = field(default_factory=list)
    confirmed_writes: list[str] = field(default_factory=list)
    effects: Any = None  # EffectsTable ; None = table par défaut

    def __post_init__(self) -> None:
        from nokker.tools.effects import EffectsTable

        if self.effects is None:
            self.effects = EffectsTable()

    def _liftable(self, spec: ToolSpec) -> bool:
        from nokker.tools.effects import ARGUMENT_EFFECTS, DEFAULT_EFFECTS

        if spec.name not in self.read_tools or spec.effect is not Effect.WRITE:
            return False
        if spec.name in self.effects.overrides or spec.name in DEFAULT_EFFECTS or spec.name in ARGUMENT_EFFECTS:
            return False
        if (write_words(spec.name) or write_words(spec.description)) and spec.name not in self.confirmed_writes:
            log.warning("outil MCP %s déclaré en lecture, mais son nom ou sa description annonce une écriture : il "
                        "reste une écriture (confirm_writes pour le confirmer)", spec.name)
            return False
        return True

    def specs(self) -> list[ToolSpec]:
        import dataclasses

        out = []
        for spec in self.inner.specs():
            spec = self.effects.annotate(spec)
            out.append(dataclasses.replace(spec, effect=Effect.READ, effect_of=None) if self._liftable(spec) else spec)
        return out

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        return self.inner.call(name, arguments, ctx)

    def turn_done(self, thread_id: str) -> None:
        hook = getattr(self.inner, "turn_done", None)
        if callable(hook):
            hook(thread_id)

    def __getattr__(self, name: str) -> Any:  # refresh, close… : ceux de la source enveloppée
        if name.startswith("__") or name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)


__all__ = ["FILE_NAME", "MCP_CONFIG_TOOLS", "McpConfigError", "McpConfigToolSource", "READ_RULE", "ReadDeclaredSource",
           "WRITE_VERBS", "check_entry", "check_read_tools", "check_url", "load_user_servers", "merge_servers",
           "secret_names", "set_read_tools", "write_words"]
