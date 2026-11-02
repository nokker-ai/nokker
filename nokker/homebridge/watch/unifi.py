"""UniFi Network — API officielle « UniFi Network API » (intégration locale), vérifiée sur la spécification OpenAPI
publiée par Ubiquiti (https://developer.ui.com/network/v11.0.97/, `openapi.json`, consultée le 01.11.2026) :

- base locale : `https://<console>/proxy/network/integration` ; en-tête `X-API-KEY` (clé créée dans la console,
  Paramètres → Control Plane → Integrations) ;
- `GET /v1/sites` → `{data: [{id, internalReference, name}]}` ;
- `GET /v1/sites/{siteId}/clients?offset=&limit=` (limit ≤ 200) → `{data: [{type: WIRED|WIRELESS|VPN|TELEPORT, id,
  name, connectedAt, ipAddress, macAddress, uplinkDeviceId, access: {type: DEFAULT|GUEST}}], totalCount}` ;
- `GET|PUT /v1/sites/{siteId}/firewall/policies/{id}` (PUT : corps complet « Create or update firewall policy »,
  champs requis `action, destination, enabled, ipProtocolScope, loggingEnabled, name, source`).

L'API officielle n'a PAS d'action « bloquer un client » (seules actions de client : AUTHORIZE_GUEST_ACCESS,
UNAUTHORIZE_GUEST_ACCESS). La pause d'un appareil (contrôle parental) passe donc par une **règle de pare-feu créée
par l'utilisateur dans la console** (source = l'adresse MAC de l'appareil, action BLOCK, désactivée), déclarée par son
identifiant dans `home.yaml` (`pause: {console_salon: <uuid de la règle>}`). La passerelle ne fait qu'une chose :
basculer `enabled` de CETTE règle (GET puis PUT du même corps). Elle ne crée, ne modifie ni ne supprime aucune autre
règle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from nokker.homebridge.watch.net import LanHttp, UpstreamError, WatchEnv

_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
PAGE = 200
MAX_CLIENTS = 1000
# Champs d'une règle de pare-feu repris tels quels au PUT (schéma « Create or update firewall policy »).
_POLICY_FIELDS = ("action", "connectionStateFilter", "description", "destination", "enabled", "ipProtocolScope",
                  "ipsecFilter", "loggingEnabled", "name", "reportFlowEnabled", "schedule", "source")


def _text(v: Any, limit: int = 80) -> str:
    s = "" if v is None else str(v)
    return " ".join("".join(c if c.isprintable() else " " for c in s).split())[:limit]


def mac_norm(v: Any) -> str:
    s = re.sub(r"[^0-9a-f]", "", str(v or "").lower())
    return ":".join(s[i:i + 2] for i in range(0, 12, 2)) if len(s) == 12 else ""


@dataclass
class UniFi:
    config: WatchEnv
    url: str
    key_ref: str
    site: str = "default"
    pause: dict[str, str] | None = None
    http: LanHttp | None = None

    @classmethod
    def from_config(cls, config: WatchEnv, raw: dict[str, Any]) -> UniFi:
        url = str(raw.get("url") or "").rstrip("/")
        if not url or not raw.get("api_key"):
            raise ValueError("watch.unifi : url et api_key (secret:NOM) obligatoires")
        config.check_url(url)
        pause: dict[str, str] = {}
        for name, pid in (raw.get("pause") or {}).items():
            if not _NAME.match(str(name)) or not _UUID.match(str(pid)):
                raise ValueError(f"watch.unifi.pause : nom ({str(name)[:48]!r}) ou identifiant de règle invalide")
            pause[str(name)] = str(pid)
        verify = raw.get("ca_file") or bool(raw.get("verify_tls", True))
        return cls(config=config, url=url, key_ref=str(raw["api_key"]), site=str(raw.get("site") or "default"),
                   pause=pause, http=LanHttp(config, verify=verify))

    def _api(self, method: str, path: str, **kw: Any) -> Any:
        headers = {"X-API-KEY": self.config.secret(self.key_ref), "Accept": "application/json"}
        return self.http.json(method, f"{self.url}/proxy/network/integration{path}", headers=headers, **kw)

    def _site_id(self) -> str:
        sites = (self._api("GET", "/v1/sites", params={"limit": 200}) or {}).get("data") or []
        for s in sites:
            if self.site in (s.get("internalReference"), s.get("name"), s.get("id")):
                return str(s.get("id"))
        raise UpstreamError(f"site UniFi introuvable : {self.site}", "not_found")

    # ── lecture ──
    def clients(self) -> list[dict[str, Any]]:
        sid = self._site_id()
        out: list[dict[str, Any]] = []
        offset = 0
        while offset < MAX_CLIENTS:
            page = self._api("GET", f"/v1/sites/{sid}/clients", params={"offset": offset, "limit": PAGE}) or {}
            data = page.get("data") or []
            for c in data:
                out.append({"id": str(c.get("id") or ""), "name": _text(c.get("name")), "mac": mac_norm(c.get("macAddress")),
                            "ip": _text(c.get("ipAddress"), 45), "type": _text(c.get("type"), 12),
                            "connected_at": _text(c.get("connectedAt"), 40),
                            "guest": ((c.get("access") or {}).get("type") == "GUEST")})
            offset += len(data)
            if not data or offset >= int(page.get("totalCount") or 0):
                break
        return out

    def pause_targets(self) -> list[str]:
        return sorted((self.pause or {}).keys())

    # ── action nommée : basculer la règle de pause déclarée ──
    def set_pause(self, name: str, paused: bool) -> dict[str, Any]:
        pid = (self.pause or {}).get(name)
        if pid is None:
            raise UpstreamError(f"appareil sans règle de pause déclarée : {name[:48]}", "unknown_target")
        sid = self._site_id()
        cur = self._api("GET", f"/v1/sites/{sid}/firewall/policies/{pid}") or {}
        if str((cur.get("action") or {}).get("type")) not in ("BLOCK", "REJECT"):
            raise UpstreamError("la règle déclarée ne bloque pas (action BLOCK ou REJECT attendue)", "bad_policy")
        if bool(cur.get("enabled")) == paused:
            return {"changed": False, "paused": paused}
        body = {k: cur[k] for k in _POLICY_FIELDS if k in cur}
        body["enabled"] = paused
        self._api("PUT", f"/v1/sites/{sid}/firewall/policies/{pid}", json_body=body)
        return {"changed": True, "paused": paused}


__all__ = ["UniFi", "mac_norm"]
