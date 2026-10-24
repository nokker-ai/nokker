"""Premier lancement de Nokker Lokkal (docs/LOKKAL.md §7.1) : modèles, coffre local, état de l'onboarding.

- **Coffre local** (`LocalVault`) : clés BYOK et clé du Router, chiffrées au repos (Fernet) dans un fichier 0600, avec une
  clé que l'installeur génère dans le `.env` du compose (`NOKKER_LOCAL_SECRET_KEY`). Sans cette clé, le coffre refuse
  d'écrire : jamais une clé d'API en clair sur le disque. Les clés sont désignées par leur nom (`router`,
  `byok:<nom>`) et ne sortent jamais par l'API.
- **Modèles** : Router (compte lié), BYOK (tout point d'accès compatible OpenAI) ou modèle local (Ollama détecté sur
  l'hôte). Choisir le modèle par défaut rebranche les paliers du noyau en direct (`apply`), sans redémarrage.
- **État** (`setup.json`, à côté du coffre) : ce qui n'est pas secret — points d'accès, modèle par défaut, fait ou non.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SECRET_KEY_ENV = "NOKKER_LOCAL_SECRET_KEY"
ROUTER_URL = "https://router.sokkan.ch/v1"
OLLAMA_CANDIDATES = ("http://host.docker.internal:11434", "http://127.0.0.1:11434")


class VaultError(RuntimeError):
    """Coffre inutilisable : pas de clé de chiffrement, ou fichier illisible avec cette clé."""


@dataclass
class LocalVault:
    path: Path
    key_env: str = SECRET_KEY_ENV

    def _fernet(self) -> Any:
        from cryptography.fernet import Fernet

        key = os.environ.get(self.key_env, "")
        if not key:
            raise VaultError(f"{self.key_env} absente : le coffre local ne range rien en clair")
        return Fernet(key.encode())

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        from cryptography.fernet import InvalidToken

        try:
            return json.loads(self._fernet().decrypt(self.path.read_bytes()))
        except InvalidToken as e:
            raise VaultError("coffre local illisible avec la clé actuelle") from e

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def put(self, name: str, secret: str) -> None:
        data = self._load()
        data[name] = secret
        self._write(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._write(data)

    def names(self) -> list[str]:
        try:
            return sorted(self._load())
        except VaultError:
            return []

    def _write(self, data: dict[str, str]) -> None:
        blob = self._fernet().encrypt(json.dumps(data).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, self.path)


def test_endpoint(base_url: str, api_key: str | None, timeout_s: float = 8.0) -> dict[str, Any]:
    """`GET <base_url>/models` : le point d'accès répond-il, et avec quels modèles ? La clé n'est jamais renvoyée."""
    t0 = time.monotonic()
    try:
        r = httpx.get(base_url.rstrip("/") + "/models", timeout=timeout_s,
                      headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    except httpx.HTTPError as e:
        return {"ok": False, "models": [], "latency_ms": None, "error": type(e).__name__}
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"ok": False, "models": [], "latency_ms": ms, "error": f"HTTP {r.status_code}"}
    try:
        models = sorted(str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id"))
    except (ValueError, AttributeError):
        models = []
    return {"ok": True, "models": models, "latency_ms": ms}


def detect_local_models(candidates: tuple[str, ...] = OLLAMA_CANDIDATES) -> list[dict[str, Any]]:
    out = []
    for base in candidates:
        try:
            r = httpx.get(base + "/api/tags", timeout=1.5)
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            names = sorted(str(m.get("name")) for m in (r.json().get("models") or []) if m.get("name"))
            out.append({"runtime": "ollama", "url": base, "models": names})
            break
    return out


@dataclass
class LocalSetup:
    state_path: Path
    vault: LocalVault
    detect: Any = detect_local_models
    test: Any = test_endpoint
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_path.is_file():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.state.setdefault("done", False)
        self.state.setdefault("byok", [])
        self.state.setdefault("default", None)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def view(self) -> dict[str, Any]:
        names = set(self.vault.names())
        return {"done": bool(self.state["done"]),
                "models": {"router": {"linked": "router" in names, "account": self.state.get("account"),
                                      "model": self.state.get("router_model") or "qwen/qwen3.5-397b-a17b"},
                           "byok": [{"name": b["name"], "base_url": b["base_url"], "model": b.get("model"),
                                     "ok": b.get("ok"), "has_key": f"byok:{b['name']}" in names}
                                    for b in self.state["byok"]],
                           "local": self.detect()},
                "default": self.state["default"],
                "connectors": [],
                "channels": [{"kind": "telegram", "state": self.state.get("telegram", "absent")}]}

    def test_model(self, body: dict[str, Any]) -> dict[str, Any]:
        base = str(body.get("base_url") or "")
        if not base.startswith(("http://", "https://")):
            return {"ok": False, "models": [], "latency_ms": None, "error": "base_url http(s) requise"}
        return self.test(base, body.get("api_key") or None)

    def put_models(self, body: dict[str, Any]) -> dict[str, Any]:
        """Enregistre les BYOK (clé au coffre, jamais dans l'état) et le modèle par défaut."""
        if "byok" in body:
            kept = []
            for b in body.get("byok") or []:
                name, base = str(b.get("name") or "").strip(), str(b.get("base_url") or "").strip()
                if not name or not base.startswith(("http://", "https://")):
                    raise ValueError("byok : name et base_url http(s) requis")
                if b.get("api_key"):
                    self.vault.put(f"byok:{name}", str(b["api_key"]))
                kept.append({"name": name, "base_url": base, "model": b.get("model"), "ok": b.get("ok")})
            for old in self.state["byok"]:  # une entrée retirée de la liste emporte sa clé du coffre
                if old["name"] not in {k["name"] for k in kept}:
                    self.vault.delete(f"byok:{old['name']}")
            self.state["byok"] = kept
        if "default" in body:
            d = str(body["default"])
            if not (d == "router" or d.startswith(("byok:", "local:"))):
                raise ValueError("default : router | byok:<nom> | local:<url>|<modèle>")
            self.state["default"] = d
        self._save()
        return self.view()

    def done(self) -> dict[str, Any]:
        self.state["done"] = True
        self._save()
        return {"done": True}

    def provider_for_default(self) -> tuple[str, str, str] | None:
        """(base_url, clé, modèle) du modèle par défaut choisi, ou None s'il n'y en a pas encore."""
        d = self.state.get("default")
        if not d:
            return None
        if d == "router":
            key = self.vault.get("router")
            base = self.state.get("inference_base") or ROUTER_URL
            return (base, key, self.state.get("router_model") or "qwen/qwen3.5-397b-a17b") if key else None
        if d.startswith("byok:"):
            name = d[5:]
            b = next((x for x in self.state["byok"] if x["name"] == name), None)
            return (b["base_url"], self.vault.get(d) or "", b.get("model") or "") if b else None
        url, _, model = d[6:].partition("|")  # local:<url>|<modèle>
        return (url.rstrip("/") + "/v1", "", model)




@dataclass
class LocalLink:
    """Lien de Lokkal avec un compte nokker.ai, par flux d'appareil (docs/LOKKAL.md §7.2), côté machine.

    Côté nokker.ai (à écrire dans le noyau de la bêta, train ≥ 2.11) : `POST /v1/link/device` → `{device_code,
    user_code, verification_uri, expires_in, interval}` ; `POST /v1/link/token {device_code}` → `{state:
    pending|approved|expired|denied, api_key?, account?}` (la clé du Router, limitée à l'inférence, n'est rendue
    qu'une fois) ; `POST /v1/link/revoke` (Bearer = cette clé). Ici : la clé va au coffre local, jamais à l'API."""

    setup: LocalSetup
    base_url: str = "https://app.nokker.ai"
    client: Any = None  # httpx.Client injectable (tests)
    _pending: dict[str, Any] = field(default_factory=dict)

    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=10.0)

    def start(self, machine: str) -> dict[str, Any]:
        r = self._http().post(self.base_url.rstrip("/") + "/v1/link/device", json={"client": "lokkal",
                                                                                     "machine": machine})
        r.raise_for_status()
        d = r.json()
        self._pending = {"device_code": d["device_code"], "expires_at": time.time() + int(d.get("expires_in", 600))}
        return {k: d[k] for k in ("user_code", "verification_uri", "expires_in", "interval") if k in d}

    def status(self) -> dict[str, Any]:
        if "router" in self.setup.vault.names():
            return {"state": "approved", "account": self.setup.state.get("account")}
        if not self._pending:
            return {"state": "unlinked"}  # aucun lien demandé (« expired » = un flux commencé puis échu)
        if time.time() > self._pending["expires_at"]:
            self._pending = {}
            return {"state": "expired"}
        r = self._http().post(self.base_url.rstrip("/") + "/v1/link/token",
                              json={"device_code": self._pending["device_code"]})
        r.raise_for_status()
        d = r.json()
        state = str(d.get("state") or "pending")
        if state == "approved" and d.get("api_key"):
            self.setup.vault.put("router", str(d["api_key"]))
            if d.get("machine_token"):  # passerelle (§7.5) : le tunnel s'authentifie avec, le relais publie relay_host
                self.setup.vault.put("machine_token", str(d["machine_token"]))
                self.setup.state["relay_host"] = d.get("relay_host")
            if d.get("inference_base"):  # §7.2 révisé : l'inférence passe par le noyau de nokker.ai
                self.setup.state["inference_base"] = d["inference_base"]
            self.setup.state["machine_id"] = d.get("machine_id")
            try:  # clés publiques de nokker.ai, épinglées au lien : vérifient les jetons reçus par la passerelle
                jw = self._http().get(self.base_url.rstrip("/") + "/v1/.well-known/jwks.json")
                if jw.status_code == 200:
                    self.setup.state["jwks"] = jw.json()
            except httpx.HTTPError:
                pass
            self.setup.state["account"] = d.get("account")
            if not self.setup.state.get("default"):
                self.setup.state["default"] = "router"
            self.setup._save()
            self._pending = {}
            return {"state": "approved", "account": d.get("account")}
        if state in ("expired", "denied"):
            self._pending = {}
        return {"state": state}

    def unlink(self) -> dict[str, Any]:
        key = self.setup.vault.get("router")
        if key:
            try:  # révocation au mieux : la clé est effacée d'ici quoi qu'il arrive
                self._http().post(self.base_url.rstrip("/") + "/v1/link/revoke",
                                  headers={"Authorization": f"Bearer {key}"})
            except httpx.HTTPError:
                pass
            self.setup.vault.delete("router")
        self.setup.vault.delete("machine_token")
        self.setup.state["account"] = None
        self.setup.state["relay_host"] = None
        if self.setup.state.get("default") == "router":
            self.setup.state["default"] = None
        self.setup._save()
        return {"state": "unlinked"}


__all__ = ["LocalLink", "LocalSetup", "LocalVault", "VaultError", "detect_local_models", "test_endpoint"]
