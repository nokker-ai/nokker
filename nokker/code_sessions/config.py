"""Bloc `code` de la config du noyau (docs/LOKKAL.md §7.28) : lu et validé AU DÉMARRAGE, une erreur arrête le noyau.

```yaml
code:
  enabled: true
  backend: kubernetes            # docker | kubernetes ; local refusé (réservé à Lokkal, local.code)
  workspace_root: /workspaces
  layout: tenant/user/session    # racine = <workspace_root>/<tenant_id>/<user>/<session_id>
  max_sessions_per_user: 3
  idle_ttl_s: 1800
  project_skills: true           # skills du dépôt (.nokker/skills), après confiance de l'utilisateur
  max_subagents: 3               # sous-agents vivants par session (delegate) ; 0 = pas de sous-agents
  kubernetes: {namespace, image, pvc, resources: {cpu, memory}, exec_timeout_s, network: deny|egress, …}
```

Le bloc `kubernetes` est transmis TEL QUEL au module du bac à sable (`nokker/tools/k8s_sandbox.py`) : ici on n'en
contrôle que les types, plus `idle_ttl_s` recopié de `code.idle_ttl_s`. `namespace` vide = celui du noyau, lu par le
module. Un nom inconnu est gardé et journalisé (le module peut en lire de nouveaux).
"""
from __future__ import annotations

import importlib
import logging
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any

from nokker.code_sessions.paths import TENANT_RE

logger = logging.getLogger("nokker.code_sessions.config")

BACKENDS = ("docker", "kubernetes")
LAYOUT = "tenant/user/session"
MODES = ("default", "accept_edits", "plan", "auto")
K8S_MODULE = "nokker.tools.k8s_sandbox"
DEFAULT_MOUNT = "/workspace"

# Clés du bloc `code.kubernetes` (contrat du module du bac à sable) et leur type.
K8S_TYPES: dict[str, type | tuple[type, ...]] = {
    "namespace": str, "image": str, "image_pull_secrets": list, "pvc": str, "workspace_mount": str,
    "service_account": str, "resources": dict, "resources_requests": dict, "ephemeral_storage": str, "tmp_size": str,
    "exec_timeout_s": int, "start_timeout_s": int, "max_lifetime_s": int, "network": str, "egress_proxy": str,
    "run_as_user": int, "fs_group": int, "node_selector": dict, "tolerations": list, "colocate_with_core": bool,
    "idle_ttl_s": int, "core_pod_labels": dict,  # sélecteur du pod du noyau (colocate_with_core, module k8s_sandbox)
}
_LABEL_BAD = re.compile(r"[^a-z0-9.-]+")


class CodeConfigError(ValueError):
    """Bloc `code` refusé : le noyau ne démarre pas (message clair, jamais de repli)."""


def sanitize_label(value: str) -> str:
    """Valeur d'étiquette kubernetes : 63 caractères au plus, `[a-z0-9.-]`, alphanumérique au début et à la fin."""
    v = _LABEL_BAD.sub("-", str(value).lower())[:63]
    v = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", v)
    return v or "x"


def k8s_labels(instance: str, tenant: str, session: str) -> dict[str, str]:
    return {"app.kubernetes.io/name": "nokker-sandbox", "nokker.instance": sanitize_label(instance),
            "nokker.tenant": sanitize_label(tenant), "nokker.session": sanitize_label(session)}


def _int(raw: dict[str, Any], key: str, default: int, minimum: int = 1) -> int:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise CodeConfigError(f"code.{key} : entier ≥ {minimum} attendu")
    return v


def _check_type(where: str, value: Any, kind: type | tuple[type, ...]) -> None:
    ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
    if not ok:
        name = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
        raise CodeConfigError(f"{where} : {name} attendu")


def kubernetes_module() -> Any:
    try:
        return importlib.import_module(K8S_MODULE)
    except ImportError as e:
        raise CodeConfigError(f"code.backend kubernetes : module {K8S_MODULE} absent de cette image ({e}) — "
                              "installer une image qui le porte, ou code.backend: docker") from None


def stable_instance(raw: dict[str, Any]) -> str:
    """Identifiant stable du noyau (étiquette `nokker.instance`, ménage au démarrage) : `code.instance`, sinon
    NOKKER_INSTANCE, sinon HOSTNAME (nom du pod : stable pour un StatefulSet, PAS pour un Deployment), sinon le nom
    de la machine."""
    for v in (raw.get("instance"), os.environ.get("NOKKER_INSTANCE"), os.environ.get("HOSTNAME"),
              socket.gethostname()):
        if v and str(v).strip():
            return str(v).strip()
    return "nokker"


@dataclass(frozen=True)
class CodeConfig:
    backend: str
    workspace_root: str
    max_sessions_per_user: int = 3
    idle_ttl_s: int = 1800
    instance: str = "nokker"
    modes: tuple[str, ...] = ("default", "accept_edits", "plan")
    default_mode: str = "accept_edits"
    forge_hosts: tuple[str, ...] = ()
    clone_timeout_s: int = 300
    hooks: bool = False
    # Skills du dépôt (§7.21) : lus dans le dépôt de la session, invisibles sans la confiance de son propriétaire.
    project_skills: bool = True
    # Sous-agents (§7.22) : enfants vivants au plus par session (premier plan, fond, en attente d'une carte) ; 0 = aucun
    # outil `delegate*`.
    max_subagents: int = 3
    state_dir: str = ""
    secrets: dict[str, Any] | None = None
    docker: dict[str, Any] = field(default_factory=dict)
    kubernetes: dict[str, Any] = field(default_factory=dict)
    env_factory: Any = None  # KubernetesEnvironment du module (backend kubernetes), sinon None
    reaper: Any = None  # reap_k8s_sandboxes du module (backend kubernetes), sinon None

    @property
    def mount(self) -> str:
        """Chemin de la racine de session DANS le bac à sable."""
        if self.backend == "kubernetes":
            return str(self.kubernetes.get("workspace_mount") or DEFAULT_MOUNT)
        return DEFAULT_MOUNT

    def k8s_cfg(self) -> dict[str, Any]:
        """Le dict passé au module : le bloc tel quel, plus `idle_ttl_s` = `code.idle_ttl_s`."""
        return {**self.kubernetes, "idle_ttl_s": self.idle_ttl_s}

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None, *, lokkal: bool) -> CodeConfig | None:
        """None si le bloc est absent ou `enabled` faux (rien ne change). `lokkal` : le noyau sert aussi `local`."""
        if raw is not None and not isinstance(raw, dict):
            raise CodeConfigError("code : bloc (dict) attendu")
        if not raw or not raw.get("enabled", False):
            return None
        backend = str(raw.get("backend") or "docker")
        if backend == "local":
            raise CodeConfigError("code.backend local refusé hors Lokkal : les commandes tourneraient sur l'hôte du "
                                  "noyau (docker | kubernetes)")
        if backend not in BACKENDS:
            raise CodeConfigError(f"code.backend inconnu : {backend!r} (docker | kubernetes)")
        if lokkal:
            raise CodeConfigError("code.enabled : les sessions multi-utilisateur ne tournent pas dans Lokkal "
                                  "(sessions locales : local.code)")
        if str(raw.get("layout") or LAYOUT) != LAYOUT:
            raise CodeConfigError(f"code.layout : seul {LAYOUT} est pris en charge")
        root = str(raw.get("workspace_root") or "")
        if not os.path.isabs(root):
            raise CodeConfigError("code.workspace_root : chemin absolu attendu")
        if not os.path.isdir(root):
            raise CodeConfigError(f"code.workspace_root introuvable : {root} (volume des espaces de travail monté ?)")
        root = os.path.realpath(root)
        max_s = _int(raw, "max_sessions_per_user", 3)
        idle = _int(raw, "idle_ttl_s", 1800, minimum=60)
        clone_t = _int(raw, "clone_timeout_s", 300)
        modes = raw.get("modes", ["default", "accept_edits", "plan"])
        if not isinstance(modes, list) or not modes or any(m not in MODES for m in modes):
            raise CodeConfigError(f"code.modes : liste parmi {', '.join(MODES)}")
        default_mode = str(raw.get("default_mode") or ("accept_edits" if "accept_edits" in modes else modes[0]))
        if default_mode not in modes:
            raise CodeConfigError("code.default_mode : doit être dans code.modes")
        forge = raw.get("forge") or {}
        _check_type("code.forge", forge, dict)
        hosts = forge.get("allowed_hosts", [])
        if not isinstance(hosts, list) or any(not isinstance(h, str) or not h.strip() for h in hosts):
            raise CodeConfigError("code.forge.allowed_hosts : liste de noms d'hôtes (hôte ou hôte:port)")
        secrets = raw.get("secrets")
        if secrets is not None:
            _check_type("code.secrets", secrets, dict)
        docker = dict(raw.get("docker") or {})
        _check_type("code.docker", docker, dict)
        k8s = raw.get("kubernetes") or {}
        _check_type("code.kubernetes", k8s, dict)
        k8s = dict(k8s)
        for k, v in k8s.items():
            kind = K8S_TYPES.get(k)
            if kind is None:
                logger.warning("code.kubernetes.%s : clé inconnue du noyau, transmise telle quelle", k)
            elif v is not None:
                _check_type(f"code.kubernetes.{k}", v, kind)
        if k8s.get("network") not in (None, "deny", "egress"):
            raise CodeConfigError("code.kubernetes.network : deny | egress")
        state_dir = os.path.realpath(str(raw.get("state_dir") or os.path.join(root, ".nokker-state")))
        if not os.path.isabs(str(raw.get("state_dir") or "/")):
            raise CodeConfigError("code.state_dir : chemin absolu attendu")
        inside = os.path.relpath(state_dir, root)
        if not inside.startswith("..") and TENANT_RE.fullmatch(inside.split(os.sep)[0]):
            raise CodeConfigError("code.state_dir : jamais sous le dossier d'un locataire (un bac à sable le verrait)")
        hooks = raw.get("hooks", False)
        _check_type("code.hooks", hooks, bool)
        project_skills = raw.get("project_skills", True)
        _check_type("code.project_skills", project_skills, bool)
        max_sub = _int(raw, "max_subagents", 3, minimum=0)
        if max_sub > 10:
            raise CodeConfigError("code.max_subagents : 10 au plus (un enfant = un tour du modèle dans le noyau)")
        env_factory = reaper = None
        if backend == "kubernetes":
            mod = kubernetes_module()
            env_factory = getattr(mod, "KubernetesEnvironment", None)
            reaper = getattr(mod, "reap_k8s_sandboxes", None)
            if not callable(env_factory) or not callable(reaper):
                raise CodeConfigError(f"{K8S_MODULE} : KubernetesEnvironment et reap_k8s_sandboxes attendus")
        return cls(backend=backend, workspace_root=root, max_sessions_per_user=max_s, idle_ttl_s=idle,
                   instance=stable_instance(raw), modes=tuple(modes), default_mode=default_mode,
                   forge_hosts=tuple(h.strip().lower() for h in hosts), clone_timeout_s=clone_t, hooks=hooks,
                   project_skills=project_skills, max_subagents=max_sub, state_dir=state_dir,
                   secrets=dict(secrets) if secrets is not None else None, docker=docker,
                   kubernetes=k8s if backend == "kubernetes" else {}, env_factory=env_factory, reaper=reaper)


__all__ = ["BACKENDS", "CodeConfig", "CodeConfigError", "K8S_MODULE", "K8S_TYPES", "k8s_labels", "kubernetes_module",
           "sanitize_label", "stable_instance"]
