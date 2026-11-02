"""Côté noyau de l'injection des secrets à la sortie (docs/EGRESS-SECRETS.md) : déclarations par secret, ouverture
d'une session au proxy, environnement du bac à sable, journal.

```yaml
code:                      # (ou local.code pour Lokkal)
  egress_secrets:
    enabled: false         # drapeau : éteint, rien ne change (aucun appel au proxy, environnement d'origine)
    proxy_url: http://nokker-egress:3128      # le proxy tel que le voit le bac à sable
    control_url: http://nokker-egress:3129    # son canal de contrôle tel que le voit le noyau
    control_key_env: NOKKER_EGRESS_CONTROL_KEY   # ou control_key_file
    ca_cert: /etc/nokker/egress/ca.crt        # certificat PUBLIC de l'AC du proxy, lisible par le noyau
    secrets:
      GITHUB_TOKEN:
        hosts: [github.com, api.github.com]
        headers: [authorization]
      SEARCH_KEY:
        hosts: [api.search.example]
        query: [key]
        fields: [auth.key]
```

Le noyau ne met dans le bac à sable que : l'URL du proxy avec un jeton de session opaque (aléatoire, propre à la
session, révoqué à la fermeture), le certificat public de l'AC et la liste des NOMS disponibles. Les valeurs partent du
coffre du locataire vers le proxy, par le canal de contrôle, et n'existent que dans la mémoire du proxy.
"""
from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from nokker.egress.policy import SecretRule, parse_rules
from nokker.secrets.base import SecretsError

logger = logging.getLogger("nokker.egress")

ENV_KEY = "NOKKER_EGRESS_CONTROL_KEY"
CA_REL = (".nokker", "egress-ca.pem")
REFS_ENV = "NOKKER_SECRET_REFS"
PROXY_USER = "nokker"
CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS",
               "PIP_CERT")

Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


class EgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class EgressSecretsConfig:
    proxy_url: str
    control_url: str
    ca_cert: str
    rules: tuple[SecretRule, ...] = ()
    control_key_env: str = ENV_KEY
    control_key_file: str = ""
    timeout_s: float = 5.0

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> EgressSecretsConfig | None:
        """None si le bloc est absent ou `enabled` faux. `ValueError` (message clair) si le bloc est invalide."""
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValueError("egress_secrets : bloc attendu")
        if not raw.get("enabled", False):
            return None
        for key in ("proxy_url", "control_url"):
            u = urlsplit(str(raw.get(key) or ""))
            if u.scheme != "http" or not u.hostname or u.username or u.password:
                raise ValueError(f"egress_secrets.{key} : http://hôte:port attendu (sans identifiants)")
        ca = str(raw.get("ca_cert") or "")
        if not os.path.isabs(ca):
            raise ValueError("egress_secrets.ca_cert : chemin absolu du certificat public de l'AC du proxy")
        return cls(proxy_url=str(raw["proxy_url"]).rstrip("/"), control_url=str(raw["control_url"]).rstrip("/"),
                   ca_cert=ca, rules=parse_rules(raw.get("secrets")),
                   control_key_env=str(raw.get("control_key_env") or ENV_KEY),
                   control_key_file=str(raw.get("control_key_file") or ""),
                   timeout_s=float(raw.get("timeout_s", 5.0)))

    def control_key(self) -> str:
        if self.control_key_file:
            try:
                return Path(self.control_key_file).read_text().strip()
            except OSError as e:
                raise EgressError(f"clé du canal de contrôle illisible ({self.control_key_file}) : {e.strerror}") \
                    from None
        key = os.environ.get(self.control_key_env, "").strip()
        if not key:
            raise EgressError(f"clé du canal de contrôle absente ({self.control_key_env})")
        return key


@dataclass(frozen=True)
class SandboxSetup:
    """Ce que reçoit le bac à sable. `proxy_url` porte le jeton de session (pas une valeur de secret)."""

    proxy_url: str
    env: dict[str, str]
    names: tuple[str, ...]

    def __repr__(self) -> str:  # le jeton ne s'affiche pas
        return f"SandboxSetup(names={list(self.names)})"


def _urllib_transport(key: str, timeout: float) -> Transport:
    def call(method: str, url: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        # Pas de proxy pour joindre le proxy : `ProxyHandler({})` ignore HTTP(S)_PROXY de l'environnement du noyau.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EgressError(f"proxy de sortie injoignable ({urlsplit(url).netloc}) : "
                              f"{getattr(e, 'reason', e)}") from None

    return call


def note_text(rules: list[SecretRule]) -> str:
    """Ligne vue par le modèle à l'ouverture d'une session qui a des secrets accordés (texte exact : rapport)."""
    lines = [("[session de code] secrets par référence : écris `secret:NOM` là où irait la valeur ; le proxy de "
              "sortie la pose au départ de la requête, seulement vers les hôtes et aux emplacements déclarés (sinon "
              "refus 403), et la remet en référence dans les réponses. La valeur n'entre jamais dans le bac à sable.")]
    for r in rules:
        where = []
        if r.headers:
            where.append("en-têtes " + ", ".join(r.headers))
        if r.query:
            where.append("paramètres d'URL " + ", ".join(r.query))
        if r.fields:
            where.append("champs du corps " + ", ".join(r.fields))
        lines.append(f"- {r.name} : {', '.join(r.hosts)} ; {' ; '.join(where)}")
    return "\n".join(lines)


@dataclass
class EgressSecrets:
    cfg: EgressSecretsConfig
    transport: Transport | None = None
    _after: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._rules = {r.name: r for r in self.cfg.rules}

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        t = self.transport or _urllib_transport(self.cfg.control_key(), self.cfg.timeout_s)
        return t(method, self.cfg.control_url + path, body)

    def available(self, provider: Any) -> list[SecretRule]:
        """Règles déclarées dont le NOM existe dans le coffre de CE locataire (`provider` : sa portée)."""
        if provider is None or not self._rules:
            return []
        try:
            present = {i.name for i in provider.list()}
        except SecretsError as e:
            logger.warning("injection des secrets : coffre illisible (%s)", e.code)
            return []
        return [r for n, r in sorted(self._rules.items()) if n in present]

    def open_session(self, session: str, tenant: str, provider: Any, host_dir: str, sandbox_dir: str
                     ) -> SandboxSetup | None:
        """Accorde au proxy les secrets déclarés présents dans le coffre du locataire, dépose le certificat de l'AC
        dans l'espace de travail et rend l'environnement du bac à sable. None si rien n'est accordé (environnement
        d'origine). Proxy injoignable : None aussi (aucun secret ne part, la session reste utilisable sans eux)."""
        rules = self.available(provider)
        if not rules:
            return None
        payload: list[dict[str, Any]] = []
        for r in rules:
            try:
                value = provider.get(r.name)
            except SecretsError as e:
                logger.warning("injection des secrets : %s illisible (%s)", r.name, e.code)
                continue
            if value:
                payload.append({"name": r.name, "value": value, **r.as_dict()})
        if not payload:
            return None
        try:
            ca_in_sandbox = self._drop_ca(host_dir, sandbox_dir)
        except (EgressError, OSError) as e:
            logger.warning("injection des secrets : session %s sans secrets (%s)", session, e)
            return None
        token = _secrets.token_urlsafe(32)
        try:
            status, out = self._call("PUT", f"/v1/sessions/{quote(session, safe='')}",
                                     {"token": token, "tenant": tenant, "secrets": payload})
        except EgressError as e:
            logger.warning("injection des secrets : session %s sans secrets (%s)", session, e)
            return None
        finally:
            for p in payload:  # la valeur ne reste pas dans une structure du noyau plus longtemps que l'appel
                p["value"] = ""
        if status != 200:
            logger.warning("injection des secrets : le proxy refuse la session %s (%s, %s)", session, status,
                           out.get("error"))
            return None
        names = tuple(str(n) for n in out.get("secrets") or [p["name"] for p in payload])
        for r in rules:
            if r.name in names:
                logger.info("injection des secrets : session %s (%s) — %s vers %s", session, tenant, r.name,
                            ", ".join(r.hosts))
        u = urlsplit(self.cfg.proxy_url)
        netloc = f"{PROXY_USER}:{token}@{u.netloc}"
        proxy = urlunsplit((u.scheme, netloc, u.path, "", ""))
        env = {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
               "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1", "NODE_USE_ENV_PROXY": "1",
               REFS_ENV: ",".join(names), **{k: ca_in_sandbox for k in CA_ENV_VARS}}
        return SandboxSetup(proxy_url=proxy, env=env, names=names)

    def _drop_ca(self, host_dir: str, sandbox_dir: str) -> str:
        """Copie le certificat PUBLIC de l'AC dans `<espace>/.nokker/egress-ca.pem` (exclu de git) ; rend son chemin
        dans le bac à sable."""
        try:
            pem = Path(self.cfg.ca_cert).read_bytes()
        except OSError as e:
            raise EgressError(f"certificat de l'AC illisible ({self.cfg.ca_cert}) : {e.strerror}") from None
        d = Path(host_dir, CA_REL[0])
        if d.is_symlink():
            raise EgressError("espace de travail : .nokker est un lien symbolique")
        d.mkdir(exist_ok=True)
        target = d / CA_REL[1]
        if target.is_symlink():
            target.unlink()
        tmp = d / (CA_REL[1] + ".tmp")
        tmp.write_bytes(pem)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        exclude = Path(host_dir, ".git", "info", "exclude")
        line = "/" + "/".join(CA_REL)
        if exclude.parent.is_dir() and not exclude.is_symlink():
            cur = exclude.read_text() if exclude.exists() else ""
            if line not in cur.splitlines():
                exclude.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + line + "\n")
        return sandbox_dir.rstrip("/") + "/" + "/".join(CA_REL)

    def close_session(self, session: str) -> None:
        try:
            self._call("DELETE", f"/v1/sessions/{quote(session, safe='')}")
        except EgressError as e:
            logger.warning("injection des secrets : révocation de %s non confirmée (%s)", session, e)

    def journal(self, session: str | None = None) -> list[dict[str, Any]]:
        """Événements du proxy depuis le dernier appel (nom, hôte, emplacement ; jamais une valeur), recopiés dans le
        journal du noyau."""
        q = f"?after={self._after}" + (f"&session={quote(session, safe='')}" if session else "")
        status, out = self._call("GET", "/v1/journal" + q)
        if status != 200:
            return []
        events = list(out.get("events") or [])
        self._after = int(out.get("next") or self._after)
        for e in events:
            logger.info("injection des secrets : %s %s → %s (%s, session %s)", e.get("event"), e.get("secret"),
                        e.get("host"), e.get("where"), e.get("session"))
        return events


__all__ = [
    "CA_ENV_VARS",
    "CA_REL",
    "ENV_KEY",
    "REFS_ENV",
    "EgressError",
    "EgressSecrets",
    "EgressSecretsConfig",
    "SandboxSetup",
    "note_text",
]
