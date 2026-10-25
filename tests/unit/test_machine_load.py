"""Charge des machines Lokkal (lot X5, nokker/local/load.py, docs/LOKKAL.md §7.2) : mesure (cgroup v2 ou /proc, CPU,
GPU par nvidia-smi sans jamais échouer), fils actifs, envoi par le jeton de machine, nettoyage et lecture côté nokker.ai
(« mes machines »)."""
from __future__ import annotations

import subprocess
import threading
import time

import httpx
import pytest

from nokker.cloud.link import LinkService, MemoryLinkStore
from nokker.local.load import ActiveTurns, LoadReporter, clean_load, measure, nvidia_gpus

GIB = 1024 ** 3


def _proc(tmp_path, total_kb=16 * 1024 * 1024, avail_kb=10 * 1024 * 1024, cgroup="0::/"):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text(f"MemTotal:       {total_kb} kB\nMemFree:  1 kB\nMemAvailable:   {avail_kb} kB\n")
    (proc / "self" / "cgroup").write_text(cgroup + "\n")
    cg = tmp_path / "cgroup"
    cg.mkdir()
    return proc, cg


def _m(proc, cg, **kw):
    return measure(proc=proc, cgroup_root=cg, loadavg=lambda: (1.234, 0.5, 0.1), host_cpus=lambda: 8, **kw)


def test_host_without_cgroup_limit_reads_meminfo(tmp_path):
    proc, cg = _proc(tmp_path)
    (cg / "memory.max").write_text("max\n")
    (cg / "cpu.max").write_text("max 100000\n")
    m = _m(proc, cg, version="0.1.0", sessions=2, active_turns=1)
    assert m == {"v": 1, "version": "0.1.0",
                 "ram": {"total_mb": 16384, "free_mb": 10240, "source": "host"},
                 "cpu": {"count": 8, "load1": 1.23, "source": "host"},
                 "gpus": [], "sessions": 2, "active_turns": 1}


def test_container_limit_wins_and_inactive_cache_counts_as_free(tmp_path):
    proc, cg = _proc(tmp_path)
    (cg / "memory.max").write_text(str(4 * GIB))
    (cg / "memory.current").write_text(str(3 * GIB))
    (cg / "memory.stat").write_text(f"anon 1\ninactive_file {1 * GIB}\nactive_file 5\n")
    (cg / "cpu.max").write_text("150000 100000\n")
    m = _m(proc, cg)
    assert m["ram"] == {"total_mb": 4096, "free_mb": 2048, "source": "cgroup"}
    assert m["cpu"] == {"count": 1.5, "load1": 1.23, "source": "cgroup"}


def test_cgroup_of_the_process_is_read_before_the_root(tmp_path):
    """Hôte : la limite est sur la slice systemd du processus (/proc/self/cgroup), pas à la racine."""
    proc, cg = _proc(tmp_path, cgroup="0::/system.slice/lokkal.service")
    d = cg / "system.slice" / "lokkal.service"
    d.mkdir(parents=True)
    (d / "memory.max").write_text(str(2 * GIB))
    (d / "memory.current").write_text(str(GIB // 2))
    assert _m(proc, cg)["ram"] == {"total_mb": 2048, "free_mb": 1536, "source": "cgroup"}


def test_limit_above_the_host_is_not_a_limit_and_host_pressure_caps_free(tmp_path):
    proc, cg = _proc(tmp_path, total_kb=8 * 1024 * 1024, avail_kb=512 * 1024)
    (cg / "memory.max").write_text(str(64 * GIB))
    (cg / "memory.current").write_text("0")
    assert _m(proc, cg)["ram"]["source"] == "host"
    (cg / "memory.max").write_text(str(4 * GIB))  # le container a de la marge, l'hôte n'en a plus
    assert _m(proc, cg)["ram"] == {"total_mb": 4096, "free_mb": 512, "source": "cgroup"}


def test_quota_above_the_cpus_of_the_host_is_ignored(tmp_path):
    proc, cg = _proc(tmp_path)
    (cg / "cpu.max").write_text("1600000 100000\n")
    assert _m(proc, cg)["cpu"]["count"] == 8 and _m(proc, cg)["cpu"]["source"] == "host"


def test_unreadable_proc_never_raises(tmp_path):
    m = measure(proc=tmp_path / "rien", cgroup_root=tmp_path / "rien", host_cpus=lambda: 4,
                gpus=lambda: (_ for _ in ()).throw(RuntimeError("pilote")))
    assert m["ram"] == {"total_mb": 0, "free_mb": 0, "source": "host"} and m["gpus"] == [] and m["cpu"]["count"] == 4


def test_real_measure_is_light():
    measure()  # premier appel : imports et caches du système de fichiers
    t0 = time.perf_counter()
    m = measure(version="x")
    assert (time.perf_counter() - t0) < 0.05
    assert m["ram"]["total_mb"] > 0 and m["cpu"]["count"] >= 1 and m["gpus"] == []


class _Run:
    def __init__(self, out="", code=0, exc=None):
        self.out, self.code, self.exc, self.argv = out, code, exc, None

    def __call__(self, argv, **kw):
        self.argv = argv
        assert kw["timeout"] <= 2.0 and kw["check"] is False
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.code, stdout=self.out, stderr="")


def test_nvidia_gpus_parses_the_standard_query():
    run = _Run("NVIDIA GeForce RTX 3090, 24576, 20000, 7\nNVIDIA GeForce RTX 3090, 24576, [N/A], [Not Supported]\n")
    gpus = nvidia_gpus(run=run, which=lambda n: "/usr/bin/nvidia-smi")
    assert gpus == [{"name": "NVIDIA GeForce RTX 3090", "memory_total_mb": 24576, "memory_free_mb": 20000, "util_pct": 7},
                    {"name": "NVIDIA GeForce RTX 3090", "memory_total_mb": 24576, "memory_free_mb": None,
                     "util_pct": None}]
    assert run.argv[0] == "/usr/bin/nvidia-smi" and run.argv[1].startswith("--query-gpu=")


@pytest.mark.parametrize("run", [_Run(code=9), _Run(exc=subprocess.TimeoutExpired("nvidia-smi", 2)),
                                 _Run(exc=FileNotFoundError()), _Run("garbage\n")])
def test_nvidia_gpus_never_fails(run):
    assert nvidia_gpus(run=run, which=lambda n: "/usr/bin/nvidia-smi") == []


def test_no_nvidia_smi_no_process():
    run = _Run("ne doit pas tourner")
    assert nvidia_gpus(run=run, which=lambda n: None) == [] and run.argv is None


def test_active_turns_counts_threads_in_progress_even_on_error():
    active = ActiveTurns()
    inside = threading.Event()
    go = threading.Event()

    class Inp:
        def __init__(self, tid):
            self.thread_id = tid

    def run_turn(inp, sink=None):
        if inp.thread_id == "th_boom":
            raise RuntimeError("amont")
        inside.set()
        go.wait(5)
        return "tour"

    wrapped = active.wrap(run_turn)
    t = threading.Thread(target=wrapped, args=(Inp("th_a"),))
    t.start()
    assert inside.wait(5) and active.count() == 1
    with pytest.raises(RuntimeError):
        wrapped(Inp("th_boom"))
    assert active.count() == 1
    go.set()
    t.join(5)
    assert active.count() == 0 and wrapped(Inp("th_a"), None) == "tour"


class _Client:
    def __init__(self, status=200, exc=None):
        self.status, self.exc, self.seen = status, exc, []

    def post(self, url, json=None, headers=None):
        self.seen.append((url, json, headers))
        if self.exc:
            raise self.exc

        class R:
            status_code = self.status

        return R()


def test_reporter_sends_only_when_linked_and_never_raises():
    token = {"v": None}
    client = _Client()
    rep = LoadReporter(base_url="https://app.nokker.ai/", token=lambda: token["v"], measure=lambda: {"ram": {}},
                       client=client)
    assert rep.send_once() is False and client.seen == []  # non liée : rien
    token["v"] = "mt-secret"
    assert rep.send_once() is True
    assert client.seen[-1] == ("https://app.nokker.ai/v1/gateway/machine/load", {"ram": {}},
                               {"Authorization": "Bearer mt-secret"})
    rep.client = _Client(status=401)
    assert rep.send_once() is False and rep.last_status == 401  # jeton révoqué : silencieux
    rep.client = _Client(exc=httpx.ConnectError("hors ligne"))
    assert rep.send_once() is False
    rep.measure = lambda: 1 / 0
    assert rep.send_once() is False


def test_reporter_kick_sends_at_once():
    client = _Client()
    rep = LoadReporter(base_url="https://app.nokker.ai", token=lambda: "mt", measure=lambda: {"cpu": {}},
                       client=client, interval_s=3600)
    rep.start()
    try:
        for _ in range(100):
            if client.seen:
                break
            time.sleep(0.01)
        assert len(client.seen) == 1  # envoi au démarrage
        rep.kick()  # tunnel ouvert
        for _ in range(100):
            if len(client.seen) == 2:
                break
            time.sleep(0.01)
        assert len(client.seen) == 2
    finally:
        rep.stop()


def test_clean_load_keeps_known_bounded_fields_only():
    raw = {"v": 1, "version": "0.1.0" + "x" * 100, "secret": "non",
           "ram": {"total_mb": 4096, "free_mb": -5, "source": "cgroup", "extra": 1},
           "cpu": {"count": 1.5, "load1": float("nan"), "source": "ailleurs"},
           "gpus": [{"name": "A" * 200, "memory_total_mb": 24576, "memory_free_mb": True, "util_pct": 140}, "x"] * 20,
           "sessions": 3, "active_turns": "2"}
    c = clean_load(raw)
    assert "secret" not in c and "extra" not in c["ram"] and len(c["version"]) == 40
    assert c["ram"] == {"total_mb": 4096, "free_mb": None, "source": "cgroup"}
    assert c["cpu"] == {"count": 1.5, "load1": None, "source": None}
    assert len(c["gpus"]) == 8 and c["gpus"][0] == {"name": "A" * 80, "memory_total_mb": 24576,
                                                    "memory_free_mb": None, "util_pct": None}
    assert c["sessions"] == 3 and c["active_turns"] == 0
    assert clean_load([1]) is None and clean_load({}) is None and clean_load({"ram": "x"}) is None


def test_measure_shape_survives_cleaning(tmp_path):
    proc, cg = _proc(tmp_path)
    m = _m(proc, cg, version="0.1.0", sessions=1, active_turns=1,
           gpus=lambda: [{"name": "RTX", "memory_total_mb": 24576, "memory_free_mb": 1, "util_pct": 0}])
    assert clean_load(m) == m


def test_loads_are_kept_per_tenant_one_row_per_machine():
    svc = LinkService()
    d = svc.device("atelier")
    svc.approve(d["user_code"], "us_a", "tn_aaaaaaaaaaaa")
    m = svc.machine_for_token(svc.token(d["device_code"])["machine_token"])
    assert svc.report_load(m, {"ram": {"total_mb": 1}}) is not None
    svc.report_load(m, {"ram": {"total_mb": 2}})
    store: MemoryLinkStore = svc.store
    assert len(store.loads) == 1  # pas d'historique
    assert store.loads_of("tn_bbbbbbbbbbbb", [m.id]) == {}  # un autre locataire ne lit rien
    [mine] = svc.machines_with_load("us_a", "tn_aaaaaaaaaaaa")
    assert mine.load["ram"]["total_mb"] == 2 and mine.load_at is not None
    assert svc.machine_of("us_a", m.id).load is None  # le registre n'est pas modifié par la lecture
    assert svc.report_load(m, "junk") is None


def test_tunnel_opening_kicks_the_load_report(tmp_path, monkeypatch):
    """`LocalApi.start_tunnel` branche l'envoi de la charge sur l'ouverture du tunnel (`on_up`)."""
    from cryptography.fernet import Fernet

    from nokker.local.api import LocalApi
    from nokker.local.tunnel import Tunnel

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(Tunnel, "start", lambda self: None)  # pas de réseau
    root = tmp_path / "projets"
    root.mkdir()
    api = LocalApi.from_config({"state_dir": str(tmp_path / "etat"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}}, port=8742)
    kicks = []
    api.load = LoadReporter(base_url="https://app.nokker.ai", token=lambda: None, measure=dict)
    api.load.kick = lambda: kicks.append(1)
    assert api.start_tunnel("wss://relay.nokker.app/v1/tunnel", "http://127.0.0.1:8742") is False  # non liée
    api.setup.vault.put("machine_token", "mt")
    api.setup.state["relay_host"] = "atelier.nokker.app"
    assert api.start_tunnel("wss://relay.nokker.app/v1/tunnel", "http://127.0.0.1:8742") is True
    api.tunnel._set("up")
    assert kicks == [1]
