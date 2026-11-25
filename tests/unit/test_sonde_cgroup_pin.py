"""La sonde cgroup du proxy docker du bac (deploy/lokkal/sandboxd) est figée sur ce que l'amont envoie
(commande, plafonds). Si HERMES.lock change, la fixture doit être relevée de nouveau : ce test échoue tant que
l'épingle de la fixture n'a pas été mise à jour (docs/UPSTREAM.md)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIN = ROOT / "deploy/lokkal/sandboxd/tests/fixtures/sonde_cgroup.pin"


def _kv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def test_probe_fixture_is_pinned_to_the_current_upstream():
    lock, pin = _kv(ROOT / "HERMES.lock")["tag"], _kv(PIN)["tag"]
    assert pin == lock, (
        f"HERMES.lock passe à {lock} mais la sonde cgroup du proxy a été relevée sur {pin} : relever de nouveau la "
        f"sonde de l'amont (docker run --cpus/--memory/--pids-limit … sleep 0), mettre à jour "
        f"deploy/lokkal/sandboxd/tests/fixtures/docker_run_sonde_cgroup.json et dockerproxy.py si elle a changé, "
        f"puis le tag dans sonde_cgroup.pin")


def test_probe_fixture_is_the_documented_one():
    import json

    body = json.loads((PIN.parent / "docker_run_sonde_cgroup.json").read_text(encoding="utf-8"))
    assert body["Cmd"] == ["sleep", "0"]
