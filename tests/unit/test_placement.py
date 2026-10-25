"""Placement d'une session ou d'un agent (lot X15, docs/LOKKAL.md §7.19, `nokker/cloud/placement.py`) : fonction pure —
éligibilité (en ligne, ressources par noms, GPU), classement (RAM libre, puis CPU relatif, puis tours actifs, puis un
ordre stable), attente avec sa raison ; et la délégation X21 qui passe par la même logique."""
from __future__ import annotations

import pytest

from nokker.cloud import placement as pl
from nokker.cloud.handoff import choose
from nokker.cloud.link import Machine

NOW = 50_000.0


def load(free=8000, total=16000, count=8, load1=0.8, active=0, gpus=()):
    return {"v": 1, "ram": {"total_mb": total, "free_mb": free}, "cpu": {"count": count, "load1": load1},
            "gpus": [{"name": g, "memory_total_mb": 24576, "memory_free_mb": 20000, "util_pct": 3} for g in gpus],
            "sessions": 0, "active_turns": active}


def mac(mid, name, *, ld=None, age=10, repos=(), hosts=(), connectors=(), mcp=(), seen=None):
    return Machine(id=mid, account="us_p", tenant="tn_p", name=name, host=f"{name}.nokker.app", token_sha256="",
                   created_at=0, load=ld, load_at=None if ld is None else NOW - age, last_seen_at=seen,
                   resources={"v": 1, "repos": list(repos), "hosts": list(hosts), "connectors": list(connectors),
                              "mcp": list(mcp)})


def test_ram_first_then_relative_cpu_then_active_turns_then_a_stable_order():
    big = mac("mc_b", "gros", ld=load(free=24000, load1=6.0))  # beaucoup de RAM, CPU chargé
    small = mac("mc_s", "petit", ld=load(free=4000, load1=0.1))
    assert pl.place([small, big], pl.Needs(), NOW).machine is big  # la RAM d'abord
    # même RAM à 256 Mio près : le CPU relatif tranche (load1 / count)
    a = mac("mc_a", "a", ld=load(free=8000, count=16, load1=4.0))  # 0,25
    b = mac("mc_c", "b", ld=load(free=8100, count=4, load1=2.0))  # 0,5
    assert pl.place([b, a], pl.Needs(), NOW).machine is a
    # même RAM et même CPU : le moins de tours actifs
    c = mac("mc_d", "c", ld=load(active=2))
    d = mac("mc_e", "d", ld=load(active=0))
    assert pl.place([c, d], pl.Needs(), NOW).machine is d
    # égalité parfaite : par nom puis id, quel que soit l'ordre d'arrivée
    x, y = mac("mc_y", "zeta", ld=load()), mac("mc_x", "alpha", ld=load())
    assert pl.place([x, y], pl.Needs(), NOW).machine is y and pl.place([y, x], pl.Needs(), NOW).machine is y
    assert [m.name for m in pl.place([x, y, big], pl.Needs(), NOW).ranked] == ["gros", "alpha", "zeta"]


def test_gpu_needed_excludes_machines_without_one_even_with_more_ram():
    rich = mac("mc_r", "serveur", ld=load(free=60000))
    gpu = mac("mc_g", "atelier", ld=load(free=6000, gpus=["NVIDIA GeForce RTX 3090"]))
    p = pl.place([rich, gpu], pl.Needs(gpu=True), NOW)
    assert p.machine is gpu and p.lacking["mc_r"][0]["kind"] == "gpu"
    p = pl.place([rich], pl.Needs(gpu=True), NOW)
    assert p.machine is None and p.fallback is None and "pas de GPU sur « serveur »" in p.reason


def test_resources_by_name_only_machines_that_declare_them():
    a = mac("mc_a", "atelier", ld=load(free=4000), repos=["monapp"], hosts=["prod-db"])
    b = mac("mc_b", "portable", ld=load(free=12000), repos=["monapp"])
    needs = pl.Needs(repos=("monapp",), hosts=("prod-db",))
    assert pl.place([a, b], needs, NOW).machine is a  # b a plus de RAM, mais pas l'hôte
    p = pl.place([a, b], pl.Needs(repos=("absent",)), NOW)
    assert p.machine is None and "dépôt « absent » absent de « atelier »" in p.reason
    assert pl.place([], needs, NOW).reason.startswith("aucune machine liée")


def test_offline_machine_is_never_chosen_and_the_wait_says_why():
    stale = mac("mc_o", "vieux", ld=load(free=64000), age=pl.FRESH_S + 1)  # la meilleure, mais éteinte
    live = mac("mc_l", "portable", ld=load(free=2000))
    assert pl.place([stale, live], pl.Needs(), NOW).machine is live
    off = [mac("mc_a", "atelier", ld=load(), age=4000), mac("mc_b", "nas", ld=load(), age=900)]
    p = pl.place(off, pl.Needs(), NOW)
    assert p.machine is None and p.fallback is off[1]  # la plus récemment vue : la demande l'attendra
    assert "aucune machine éligible en ligne" in p.reason and "hors ligne depuis 15 min" in p.reason
    p = pl.place(off, pl.Needs(), NOW, only="mc_a")
    assert p.machine is None and "« atelier » hors ligne depuis 66 min" in p.reason and "attend" in p.reason
    assert "depuis 2 h" in pl.place([mac("mc_h", "h", ld=load(), age=8000)], pl.Needs(), NOW).reason
    assert "inconnue ou déliée" in pl.place(off, pl.Needs(), NOW, only="mc_zz").reason
    never = mac("mc_n", "neuve")
    assert "jamais vue en ligne" in pl.place([never], pl.Needs(), NOW).reason


def test_parse_needs_takes_names_never_paths():
    assert pl.parse_needs(None) == pl.Needs() and pl.Needs().to_json() is None
    n = pl.parse_needs({"repos": ["monapp", "monapp"], "gpu": True})
    assert n == pl.Needs(repos=("monapp",), gpu=True) and n.to_json()["repos"] == ["monapp"]
    for bad in ({"repos": ["/home/p/monapp"]}, {"repos": "monapp"}, {"gpu": "oui"}, {"disk": ["x"]}, []):
        with pytest.raises(pl.NeedsError):
            pl.parse_needs(bad)


def test_handoff_uses_the_same_placement():
    """X21 : un relais par besoin choisit sa machine avec la même logique (RAM d'abord)."""
    a = mac("mc_a", "atelier", ld=load(free=3000, load1=0.0), repos=["monapp"])
    b = mac("mc_b", "portable", ld=load(free=14000, load1=3.0), repos=["monapp"])
    assert choose([a, b], "repo", "monapp", NOW) == (b, True)
    assert choose([a, b], "secret", "x", NOW) == (None, False)
    off = [mac("mc_c", "c", ld=load(), age=999, repos=["x"])]
    assert choose(off, "repo", "x", NOW) == (off[0], False)
