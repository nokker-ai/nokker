"""Identité d'instance d'un noyau local et ménage des bacs à sable (incident : deux noyaux en conteneur sur un même
démon portaient la même étiquette, le démarrage de l'un supprimait les bacs de l'autre)."""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import uuid

import pytest

from nokker.api.server import reap_local_sandboxes
from nokker.local.api import INSTANCE_FILE, LocalApi, instance_id, legacy_instance_id
from nokker.tools import code


class FakeDaemon:
    """Un démon docker minimal : `ps -a --filter label=k=v --format …` (filtre volontairement par PRÉFIXE, le pire cas)
    et `rm -f ids…`, avec le journal de tout ce qui a été supprimé."""

    def __init__(self) -> None:
        self.boxes: dict[str, str] = {}
        self.removed: list[str] = []

    def add(self, label: str, n: int = 1) -> list[str]:
        ids = [uuid.uuid4().hex[:12] for _ in range(n)]
        self.boxes.update({i: label for i in ids})
        return ids

    def run(self, argv, **kw):
        out = ""
        if argv[1:3] == ["ps", "-a"]:
            want = argv[argv.index("--filter") + 1].split("=", 2)[2]
            out = "".join(f"{i}\t{lab}\n" for i, lab in self.boxes.items() if lab.startswith(want))
        elif argv[1] == "rm":
            for i in argv[3:]:
                self.removed.append(i)
                self.boxes.pop(i, None)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture()
def daemon(monkeypatch):
    d = FakeDaemon()
    monkeypatch.setattr(subprocess, "run", d.run)
    return d


def _core(state, **local):
    return LocalApi.from_config({"state_dir": str(state), "code": {"roots": [str(state.parent)], "backend": "docker"},
                                 **local}, port=8742)


def test_instance_id_is_random_persisted_private(tmp_path):
    a = instance_id(tmp_path / "e")
    assert len(a) == 16 and int(a, 16) >= 0 and a != legacy_instance_id(tmp_path / "e")
    assert instance_id(tmp_path / "e") == a
    f = tmp_path / "e" / INSTANCE_FILE
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not [p for p in (tmp_path / "e").iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("junk", ["", "xyz", "ABCDEF0123456789", "0123456789abcde", "../../etc"])
def test_instance_id_regenerated_when_content_malformed(tmp_path, junk):
    (tmp_path / INSTANCE_FILE).write_text(junk)
    got = instance_id(tmp_path)
    assert len(got) == 16 and got != junk and (tmp_path / INSTANCE_FILE).read_text().strip() == got


def test_instance_id_unwritable_state_falls_back_to_process_id(tmp_path, caplog):
    (tmp_path / "f").write_text("")  # « dossier d'état » qui est un fichier : rien ne s'écrit dessous
    with caplog.at_level(logging.WARNING, logger="nokker.local.api"):
        a, b = instance_id(tmp_path / "f"), instance_id(tmp_path / "f" / "x")
    assert len(a) == 16 and a == b and a != legacy_instance_id(tmp_path / "f")
    assert "non persistée" in caplog.text


def test_same_state_dir_same_instance(tmp_path):
    assert instance_id(tmp_path / "s") == instance_id(tmp_path / "s")


def test_two_cores_same_state_path_do_not_share_label(tmp_path, daemon):
    """Même chemin d'état (deux conteneurs, chacun son volume) : avant, même hash donc B supprimait les bacs de A."""
    state = tmp_path / "data"
    a = _core(state)
    boxes_a = daemon.add(a.code.instance, 4)
    shutil.rmtree(state)  # autre conteneur, autre volume, même chemin
    b = _core(state)
    assert b.code.instance != a.code.instance
    assert reap_local_sandboxes(b) == 0
    assert daemon.removed == [] and sorted(daemon.boxes) == sorted(boxes_a)


def test_startup_reaps_only_own_exact_label(tmp_path, daemon):
    a, b = _core(tmp_path / "a"), _core(tmp_path / "b")
    mine, theirs = daemon.add(b.code.instance, 2), daemon.add(a.code.instance, 3)
    daemon.add(b.code.instance + "x", 1)  # préfixe : le filtre du faux démon le renvoie, le re-contrôle l'écarte
    assert reap_local_sandboxes(b) == 2
    assert sorted(daemon.removed) == sorted(mine) and set(theirs) <= set(daemon.boxes)
    assert len(daemon.boxes) == 4


def test_legacy_label_is_neither_removed_nor_warned_about(tmp_path, daemon, caplog):
    """Le noyau ne signale plus les bacs de l'ancienne étiquette (muet derrière le proxy du socket, et le ménage est fait côté
    hôte par install.sh --update) : jamais supprimés, aucun avertissement, aucun marqueur écrit dans l'état."""
    state = tmp_path / "data"
    core = _core(state)
    legacy = legacy_instance_id(state)
    old = daemon.add(legacy, 3)
    with caplog.at_level(logging.WARNING):
        assert reap_local_sandboxes(core) == 0
        assert reap_local_sandboxes(core) == 0
    assert daemon.removed == [] and set(old) <= set(daemon.boxes)
    assert not [r for r in caplog.records if "étiquette héritée" in r.getMessage()]
    assert not (state / "instance-legacy-noted").exists() and not hasattr(code, "note_legacy_sandboxes")


def test_explicit_instance_has_priority_and_legacy_equal_is_own(tmp_path, daemon, caplog):
    state = tmp_path / "data"
    legacy = legacy_instance_id(state)
    core = _core(state, instance=legacy)  # l'ancien comportement, choisi explicitement
    assert core.code.instance == legacy and not (state / INSTANCE_FILE).exists()
    old = daemon.add(legacy, 2)
    assert reap_local_sandboxes(core) == 2 and sorted(daemon.removed) == sorted(old)
    explicit = _core(tmp_path / "e2", instance="mon-noyau-1")
    assert explicit.code.instance == "mon-noyau-1"


@pytest.mark.parametrize("bad", ["", "a", "abcde", " abcdef", "abcdef "])
def test_reap_refuses_empty_or_short_instance(daemon, bad):
    daemon.add("abcdef", 1)
    daemon.add("a", 1)
    assert code.reap_sandboxes(bad) == 0 and daemon.removed == []


def test_reap_is_a_noop_without_docker(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", boom)
    assert code.reap_sandboxes("0123456789abcdef") == 0


# ─── docker RÉEL (démon partagé : uniquement des conteneurs créés ici, étiquette unique sbxtest-<hex>) ───

def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "version"], check=False, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not os.environ.get("NOKKER_TEST_REAL_DOCKER") or not _docker_ok(),
                    reason="docker réel : NOKKER_TEST_REAL_DOCKER=1 et un démon")
def test_real_docker_prefix_label_is_not_reaped():
    tag = uuid.uuid4().hex[:8]
    a, b = f"sbxtest-{tag}", f"sbxtest-{tag}c"  # A est un PRÉFIXE de B
    names = {a: f"sbxtest-{tag}-a", b: f"sbxtest-{tag}-b"}

    def mine() -> dict[str, str]:  # {nom: étiquette} de CES deux conteneurs seulement (noms exacts)
        out = subprocess.run(["docker", "ps", "-a", "--filter", f"name=sbxtest-{tag}-", "--format",
                              '{{.Names}} {{.Label "nokker.instance"}}'], check=False, capture_output=True, text=True).stdout
        return dict(line.split(" ") for line in out.splitlines() if line.count(" ") == 1)

    try:
        for label, name in names.items():
            r = subprocess.run(["docker", "run", "-d", "--name", name, "--label", f"nokker.instance={label}",
                                "alpine:3", "sleep", "300"], check=False, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
        assert mine() == {names[a]: a, names[b]: b}
        # le filtre brut de docker : égalité ou préfixe ? (établi ici, journalisé par l'assertion)
        raw = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=nokker.instance={a}"],
                             check=False, capture_output=True, text=True).stdout.split()
        assert len(raw) == 1  # égalité de valeur exacte : le conteneur de B n'est pas renvoyé
        for lab in (b, a):  # liste des cibles AVANT chaque suppression (étiquette exacte uniquement)
            print("cibles", lab, subprocess.run(["docker", "ps", "-a", "--filter", f"label=nokker.instance={lab}",
                  "--format", '{{.ID}} {{.Names}} {{.Label "nokker.instance"}}'], check=False, capture_output=True, text=True).stdout)
        assert code.reap_sandboxes(b) == 1  # ménage de B (étiquette exacte sbxtest-<hex>c)
        assert mine() == {names[a]: a}  # B supprimé, A intact
        assert code.reap_sandboxes(a) == 1
        assert mine() == {}
    finally:
        for name in names.values():  # nos deux noms exacts, rien d'autre
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
