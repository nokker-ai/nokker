"""Bac à sable Kubernetes : un pod par session de code (lot X19, chart deploy/helm/nokker).

Même rôle que `tools.environments.docker.DockerEnvironment` de l'amont, sur un cluster : le noyau crée un pod par
session avec SON compte de service (Role limité à son namespace : pods, pods/exec, pods/log, get des PVC), y lance
les commandes par `kubectl exec`, et le supprime à la fin de la session ou après `idle_ttl_s` d'inactivité. Les
fichiers ne passent pas par exec : l'espace de travail est un PVC partagé (RWX) que le noyau monte sur
`code.workspace_root` et que le pod monte en `subPath` (la racine de la session) sur `workspace_mount`.

Le pod, à parité avec le bac à sable docker de Lokkal :
- aucun jeton de compte de service (`automountServiceAccountToken: false`, compte `service_account` sans droits),
  `enableServiceLinks: false` (pas de variables d'adresses des services du namespace) ;
- contexte restricted : non-root, `readOnlyRootFilesystem` (seuls l'espace de travail et /tmp s'écrivent), drop ALL,
  seccomp RuntimeDefault, ni hostPath, ni hostNetwork, ni hostPID ; uid et fsGroup omis quand la config les met à
  null (OpenShift : la SCC les attribue) ;
- limites CPU, mémoire et stockage éphémère ; `activeDeadlineSeconds` = filet si le noyau meurt sans revenir ;
- réseau : `deny` = la NetworkPolicy du chart refuse toute sortie (label `nokker.network=deny`), `egress` = sortie par
  le proxy du chart seulement (HTTP(S)_PROXY, label `nokker.network=egress`).

Labels de chaque pod : `app.kubernetes.io/name=nokker-sandbox`, `nokker.instance` (le noyau propriétaire : ménage au
démarrage, `reap_k8s_sandboxes`), et ceux que l'appelant donne (locataire, session), assainis pour Kubernetes.
Aucune valeur de secret dans le manifeste ni dans les journaux : le pod ne reçoit que HOME, les proxys et les
variables non secrètes de l'appelant.

`kubectl` (dans l'image du noyau) parle à l'API avec le jeton du compte de service du pod du noyau ; `Kubectl` est
injectable (tests sur un faux, sans cluster).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SANDBOX_APP = "nokker-sandbox"
CONTAINER = "sandbox"
_SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
_LABEL_VALUE = re.compile(r"[^A-Za-z0-9_.-]")


class K8sSandboxError(RuntimeError):
    """Le pod n'a pas pu être créé ou n'est pas devenu prêt (message sans secret : nom du pod, raison courte)."""


def label_value(raw: str) -> str:
    """Valeur de label Kubernetes (63 caractères, [A-Za-z0-9_.-], alphanumérique aux bouts) ; une valeur trop longue
    garde un suffixe d'empreinte pour rester distincte."""
    v = _LABEL_VALUE.sub("-", str(raw)).strip("-_.")
    if len(v) > 63:
        v = v[:54].rstrip("-_.") + "-" + hashlib.sha256(str(raw).encode()).hexdigest()[:8]
    return v or "x"


def pod_name(labels: dict[str, str], salt: str = "") -> str:
    """Nom DNS-1123 (≤ 63) stable pour une session : `nokker-sbx-<empreinte>`."""
    key = json.dumps(sorted(labels.items())) + salt
    return "nokker-sbx-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def current_namespace(default: str = "default") -> str:
    try:
        with open(_SA_NAMESPACE_FILE, encoding="utf-8") as f:
            return f.read().strip() or default
    except OSError:
        return default


@dataclass
class Kubectl:
    """Appels `kubectl` : `run` (rend (code, stdout, stderr)) et `exec_argv` (argv d'un exec, lancé par l'appelant
    en sous-processus pour garder un ProcessHandle). stderr n'est journalisé que tronqué ; jamais stdin."""

    exe: str = "kubectl"
    namespace: str = "default"

    def _base(self) -> list[str]:
        return [self.exe, "--namespace", self.namespace]

    def run(self, args: list[str], stdin: str | None = None, timeout: float = 60) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._base(), *args], input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "délai dépassé"
        except OSError as e:
            return 127, "", type(e).__name__
        return p.returncode, p.stdout, p.stderr

    def exec_argv(self, pod: str, argv: list[str], stdin: bool) -> list[str]:
        return [*self._base(), "exec", *(["-i"] if stdin else []), pod, "-c", CONTAINER, "--", *argv]


@dataclass
class K8sSandboxConfig:
    """Clés `code.kubernetes.*` (voir le module). `from_dict` ignore les clés inconnues."""

    namespace: str = ""
    image: str = ""
    image_pull_secrets: list[str] = field(default_factory=list)
    pvc: str = "nokker-workspaces"
    workspace_mount: str = "/workspace"
    service_account: str = "nokker-sandbox"
    resources: dict[str, str] = field(default_factory=lambda: {"cpu": "2", "memory": "4Gi"})
    resources_requests: dict[str, str] = field(default_factory=lambda: {"cpu": "250m", "memory": "512Mi"})
    ephemeral_storage: str = "2Gi"
    tmp_size: str = "1Gi"
    exec_timeout_s: int = 600
    start_timeout_s: int = 120
    max_lifetime_s: int = 86400
    network: str = "deny"
    egress_proxy: str = ""
    run_as_user: int | None = 1000
    fs_group: int | None = 1000
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: list[dict[str, Any]] = field(default_factory=list)
    colocate_with_core: bool = False
    core_pod_labels: dict[str, str] = field(default_factory=dict)  # colocate_with_core : sélecteur du pod du noyau
    idle_ttl_s: int = 1800

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, **extra: Any) -> "K8sSandboxConfig":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SIM118
        cfg = cls(**{k: v for k, v in {**(d or {}), **extra}.items() if k in known})
        if not cfg.image:
            raise ValueError("code.kubernetes.image requis (image du bac à sable, épinglée par digest)")
        if cfg.network not in ("deny", "egress"):
            raise ValueError(f"code.kubernetes.network : deny | egress, pas {cfg.network!r}")
        if cfg.network == "egress" and not cfg.egress_proxy:
            raise ValueError("code.kubernetes.network=egress demande code.kubernetes.egress_proxy")
        if not cfg.namespace:
            cfg.namespace = current_namespace()
        return cfg


def pod_manifest(cfg: K8sSandboxConfig, name: str, sub_path: str, labels: dict[str, str], instance: str,
                 env: dict[str, str] | None = None) -> dict[str, Any]:
    """Le manifeste du pod (dict JSON). `sub_path` : chemin de la session relatif à la racine du PVC."""
    sub = sub_path.strip("/")
    if not sub or any(part in ("", ".", "..") for part in sub.split("/")):
        raise ValueError(f"sous-chemin de PVC invalide : {sub_path!r}")
    # Les labels fixes passent APRÈS ceux de l'appelant : la NetworkPolicy du chart isole les pods
    # `app.kubernetes.io/name=nokker-sandbox`, le ménage les choisit par `nokker.instance` ; un label passé ne les écrase pas.
    all_labels = {**{k: label_value(v) for k, v in labels.items()},
                  "app.kubernetes.io/name": SANDBOX_APP, "app.kubernetes.io/managed-by": "nokker",
                  "nokker.instance": label_value(instance), "nokker.network": cfg.network}
    pod_sc: dict[str, Any] = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    if cfg.run_as_user is not None:
        pod_sc["runAsUser"] = cfg.run_as_user
        pod_sc["runAsGroup"] = cfg.run_as_user
    if cfg.fs_group is not None:
        pod_sc["fsGroup"] = cfg.fs_group
    env_vars = {"HOME": "/tmp", **(env or {})}
    if cfg.network == "egress":
        env_vars.update({"HTTP_PROXY": cfg.egress_proxy, "HTTPS_PROXY": cfg.egress_proxy,
                         "http_proxy": cfg.egress_proxy, "https_proxy": cfg.egress_proxy,
                         "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"})
    limits = {**cfg.resources, "ephemeral-storage": cfg.ephemeral_storage}
    spec: dict[str, Any] = {
        "serviceAccountName": cfg.service_account,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "hostNetwork": False, "hostPID": False, "hostIPC": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 5,
        "activeDeadlineSeconds": int(cfg.max_lifetime_s),
        "securityContext": pod_sc,
        "containers": [{
            "name": CONTAINER,
            "image": cfg.image,
            "imagePullPolicy": "IfNotPresent",
            # sleep de PID 1 : les commandes arrivent par exec ; tini n'est pas requis (aucun fils orphelin attendu
            # au-delà de la session, le pod part avec elle)
            "command": ["sleep", "infinity"],
            "workingDir": cfg.workspace_mount,
            "env": [{"name": k, "value": str(v)} for k, v in env_vars.items()],
            "resources": {"limits": limits, "requests": dict(cfg.resources_requests)},
            "securityContext": {"allowPrivilegeEscalation": False, "privileged": False, "runAsNonRoot": True,
                                "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "volumeMounts": [{"name": "workspace", "mountPath": cfg.workspace_mount, "subPath": sub},
                             {"name": "tmp", "mountPath": "/tmp"}],
        }],
        "volumes": [{"name": "workspace", "persistentVolumeClaim": {"claimName": cfg.pvc}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": cfg.tmp_size}}],
    }
    if cfg.image_pull_secrets:
        spec["imagePullSecrets"] = [{"name": n} for n in cfg.image_pull_secrets]
    if cfg.node_selector:
        spec["nodeSelector"] = dict(cfg.node_selector)
    if cfg.tolerations:
        spec["tolerations"] = list(cfg.tolerations)
    if cfg.colocate_with_core and cfg.core_pod_labels:
        spec["affinity"] = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
            "labelSelector": {"matchLabels": dict(cfg.core_pod_labels)}, "topologyKey": "kubernetes.io/hostname"}]}}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "labels": all_labels, "annotations": {"nokker.dev/sub-path": sub}},
            "spec": spec}


def reap_k8s_sandboxes(instance: str, kubectl: Kubectl | None = None) -> int:
    """Supprime les pods de bac à sable de CE noyau (`nokker.instance=<instance>`) laissés par un arrêt sans ménage.
    À appeler au démarrage. Rend le nombre supprimé (0 si kubectl échoue)."""
    if not instance:
        return 0
    k = kubectl or Kubectl(namespace=current_namespace())
    sel = f"app.kubernetes.io/name={SANDBOX_APP},nokker.instance={label_value(instance)}"
    rc, out, _ = k.run(["get", "pods", "-l", sel, "-o", "name"], timeout=30)
    names = [n for n in out.split() if n.startswith("pod/")] if rc == 0 else []
    if names:
        k.run(["delete", *names, "--wait=false", "--ignore-not-found"], timeout=60)
    return len(names)


def _upstream_base() -> type:
    from tools.environments.base import BaseEnvironment

    return BaseEnvironment


class _Registry:
    """Environnements vivants du processus, pour le ménage à l'inactivité (un fil démon, une passe par minute)."""

    def __init__(self) -> None:
        self._envs: set[Any] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def add(self, env: Any) -> None:
        with self._lock:
            self._envs.add(env)
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="nokker-k8s-idle", daemon=True)
                self._thread.start()

    def discard(self, env: Any) -> None:
        with self._lock:
            self._envs.discard(env)

    def sweep(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            envs = list(self._envs)
        n = 0
        for e in envs:
            if e.idle_expired(now):
                e.release()
                n += 1
        return n

    def _loop(self) -> None:
        while True:
            time.sleep(60)
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 — le ménage ne tue jamais le noyau
                logger.exception("ménage des bacs à sable kubernetes")


REGISTRY = _Registry()


def KubernetesEnvironment(cfg: dict[str, Any] | K8sSandboxConfig, cwd: str = "", sub_path: str = "",  # noqa: N802
                          labels: dict[str, str] | None = None, timeout: int | None = None, *,
                          instance: str | None = None, env: dict[str, str] | None = None,
                          kubectl: Kubectl | None = None) -> Any:
    """Contrat du noyau (p2/code-tenant) : `KubernetesEnvironment(cfg, cwd, sub_path, labels, timeout)`. `cfg` = le
    dict `code.kubernetes` (plus `idle_ttl_s` si l'appelant l'y ajoute) ; `instance` = `labels["nokker.instance"]`
    s'il n'est pas donné. La classe (sous-classe de l'amont) est créée à la demande : l'amont n'est importé qu'ici."""
    labels = dict(labels or {})
    inst = instance or labels.pop("nokker.instance", "")
    if not inst:
        raise ValueError("KubernetesEnvironment : nokker.instance requis (labels ou instance=)")
    return _environment_class()(cfg, cwd=cwd, sub_path=sub_path, labels=labels, instance=inst, timeout=timeout,
                                env=env, kubectl=kubectl)


make_environment = KubernetesEnvironment


_CLASS: type | None = None


def _environment_class() -> type:
    global _CLASS
    if _CLASS is not None:
        return _CLASS
    Base = _upstream_base()

    class _KubernetesEnvironment(Base):  # type: ignore[misc,valid-type]
        """Un pod par session ; commandes par `kubectl exec`. Le pod est créé au premier usage, supprimé par
        `cleanup()` (fin de session) ou `release()` (inactivité : l'espace de travail reste sur le PVC), et recréé
        au prochain exec (`environment_recreated`, comme l'amont quand un container a disparu)."""

        def __init__(self, cfg: dict[str, Any] | K8sSandboxConfig, *, cwd: str, sub_path: str,
                     labels: dict[str, str], instance: str, timeout: int | None = None,
                     env: dict[str, str] | None = None, kubectl: Kubectl | None = None) -> None:
            self.cfg = cfg if isinstance(cfg, K8sSandboxConfig) else K8sSandboxConfig.from_dict(cfg)
            super().__init__(cwd=cwd or self.cfg.workspace_mount, timeout=int(timeout or self.cfg.exec_timeout_s))
            self.kubectl = kubectl or Kubectl(namespace=self.cfg.namespace)
            self.sub_path, self.labels, self.instance = sub_path, dict(labels), instance
            self.pod_env = dict(env or {})
            self.pod = pod_name({**labels, "_sub": sub_path}, salt=instance)
            self._live = False
            self._closed = False
            self._last_used = time.monotonic()
            self._pod_lock = threading.Lock()
            self._ensure_pod(first=True)
            REGISTRY.add(self)
            self.init_session()

        # ── pod ──
        def _ensure_pod(self, first: bool = False) -> None:
            with self._pod_lock:
                if self._live:
                    return
                manifest = pod_manifest(self.cfg, self.pod, self.sub_path, self.labels, self.instance, self.pod_env)
                # `apply` : un pod du même nom laissé par un noyau d'avant est repris s'il vit encore
                rc, _, err = self.kubectl.run(["apply", "-f", "-"], stdin=json.dumps(manifest), timeout=60)
                if rc != 0:
                    raise K8sSandboxError(f"création du pod {self.pod} refusée : {err.strip()[:200]}")
                rc, _, err = self.kubectl.run(
                    ["wait", "--for=condition=Ready", f"pod/{self.pod}", f"--timeout={int(self.cfg.start_timeout_s)}s"],
                    timeout=self.cfg.start_timeout_s + 15)
                if rc != 0:
                    self.kubectl.run(["delete", "pod", self.pod, "--wait=false", "--ignore-not-found"], timeout=30)
                    raise K8sSandboxError(f"pod {self.pod} pas prêt en {self.cfg.start_timeout_s} s : "
                                          f"{err.strip()[:200]}")
                self._live = True
                if not first:
                    self._mark_recreated()
                    self._snapshot_ready = False

        def _delete_pod(self) -> None:
            with self._pod_lock:
                if not self._live:
                    return
                self._live = False
            self.kubectl.run(["delete", "pod", self.pod, "--wait=false", "--ignore-not-found"], timeout=30)

        # ── interface de l'amont ──
        def _before_execute(self) -> None:
            if self._closed:
                raise K8sSandboxError("bac à sable fermé")
            self._last_used = time.monotonic()
            if not self._live:
                self._ensure_pod()
                self.init_session()

        def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                      stdin_data: str | None = None):
            from tools.environments.base_output import _popen_bash
            from tools.environments.remote_common import bash_argv

            self._last_used = time.monotonic()
            argv = self.kubectl.exec_argv(self.pod, bash_argv(cmd_string, login), stdin=stdin_data is not None)
            return _popen_bash(argv, stdin_data)

        def cleanup(self) -> None:
            """Fin de session : le pod part, l'environnement est fermé."""
            if getattr(self, "_closed", True):
                return
            self._closed = True
            REGISTRY.discard(self)
            self._delete_pod()

        # ── inactivité ──
        def idle_expired(self, now: float) -> bool:
            return (self._live and not self._closed and self.cfg.idle_ttl_s > 0
                    and now - self._last_used > self.cfg.idle_ttl_s)

        def release(self) -> None:
            """Inactivité : le pod part, l'environnement reste ouvert (recréé au prochain exec)."""
            logger.info("bac à sable %s inactif depuis %d s : pod supprimé", self.pod, self.cfg.idle_ttl_s)
            self._delete_pod()

    _KubernetesEnvironment.__module__ = __name__
    _KubernetesEnvironment.__qualname__ = "KubernetesEnvironment"
    _CLASS = _KubernetesEnvironment
    return _CLASS
