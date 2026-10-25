"""nokker/tools/mcp_config.py — serveurs MCP ajoutés par l'utilisateur dans Lokkal (lot X16, pack `mcp-builder`).

Le modèle propose, l'humain installe : `mcp_server_add` et `mcp_server_remove` sont DESTRUCTIVE (carte forte), et
le fichier qu'ils écrivent (`<état>/mcp-servers.yaml`, 0600) n'est lu qu'au démarrage du noyau, fusionné APRÈS les
serveurs de `config.yaml` (un nom déjà pris par la config l'emporte). Rien ne change donc avant un redémarrage que
l'utilisateur fait lui-même.

Identifiants : PAR NOM seulement (LOKKAL.md §7.10). `token` doit être une référence `secret:NOM` ; une valeur d'en-tête
est une référence ou un court libellé qui ne ressemble pas à une clé. Une valeur collée est refusée, jamais écrite.
Adresse : https, ou http vers la boucle locale ou un nom de service sans point (comme le coffre OpenBao).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

log = logging.getLogger("nokker.tools.mcp_config")

FILE_NAME = "mcp-servers.yaml"
MAX_SERVERS = 32
MAX_HEADERS = 8
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_PLAIN_HEADER = re.compile(r"^[A-Za-z0-9 _.,:/=-]{1,64}$")
_REF = re.compile(r"^secret:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}

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
             "purpose": {"type": "string", "description": "à quoi sert ce serveur (une phrase)"}}}),
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
    return entry


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
                    missing = self._missing(item["secrets"])
                    if missing:
                        item["missing_secrets"] = missing
                    items.append(item)
                return out({"servers": items, "configured": sorted(self.configured()) if self.configured else [],
                            "note": "les serveurs ajoutés sont actifs après un redémarrage du noyau"})
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
                missing = self._missing(names)
                if missing:
                    result["missing_secrets"] = missing
                    result["next"] = "pose chaque valeur toi-même : nokker secret set " + " ; nokker secret set ".join(
                        missing)
                return out(result)
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


__all__ = ["FILE_NAME", "MCP_CONFIG_TOOLS", "McpConfigError", "McpConfigToolSource", "check_entry", "check_url",
           "load_user_servers", "merge_servers", "secret_names"]
