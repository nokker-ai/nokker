"""Les fichiers compose livrés sont du YAML valide.

rc.9.34 (17.11.2026) : une valeur non quotée contenant « : » (message de `${VAR:?…}`) cassait le compose Lokkal
entier — « mapping values are not allowed », `install.sh --update` arrêté au démarrage, chez tout utilisateur. Aucun
test ne chargeait le fichier.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSES = sorted(
    p for p in ROOT.glob("**/*.y*ml")
    if p.name in {"docker-compose.yml", "compose.yml", "docker-compose.yaml", "compose.yaml"}
    and "node_modules" not in p.parts and ".git" not in p.parts
)


def test_compose_files_found() -> None:
    assert ROOT / "deploy/lokkal/docker-compose.yml" in COMPOSES


@pytest.mark.parametrize("path", COMPOSES, ids=lambda p: str(p.relative_to(ROOT)))
def test_compose_is_valid_yaml(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and isinstance(doc.get("services"), dict), f"{path} : pas de bloc services"


def test_lokkal_dockerproxy_requires_instance() -> None:
    doc = yaml.safe_load((ROOT / "deploy/lokkal/docker-compose.yml").read_text(encoding="utf-8"))
    env = doc["services"]["dockerproxy"]["environment"]
    assert env["NOKKER_INSTANCE"].startswith("${NOKKER_INSTANCE:?")


def _lokkal() -> dict:
    return yaml.safe_load((ROOT / "deploy/lokkal/docker-compose.yml").read_text(encoding="utf-8"))


def test_lokkal_publishes_nothing_beyond_loopback_by_default():
    """Sans profil, seuls des ports en 127.0.0.1 sont publiés (web) ; le relais ACCORD n'existe qu'avec le profil
    `accord`, que install.sh n'allume que sur NOKKER_ACCORD_LAN explicite (deploy/lokkal/tests/accord-lan.sh)."""
    for name, svc in _lokkal()["services"].items():
        if svc.get("profiles"):
            continue
        for port in svc.get("ports") or []:
            assert str(port).startswith("127.0.0.1:"), f"{name} publie {port} hors de la boucle locale par défaut"


def test_accord_relay_is_profiled_and_published_only_on_explicit_lan_address():
    svc = _lokkal()["services"]["accord-relay"]
    assert svc["profiles"] == ["accord"]
    assert svc["ports"] == ["${NOKKER_ACCORD_LAN:-127.0.0.1:0}:8443"]  # repli inerte, jamais 0.0.0.0, pas de `:?`
    assert ":?" not in str(svc) and svc.get("read_only") is True and svc.get("user") == "101:101"
    assert "./web/accord-stream.conf:/etc/nginx/accord-stream.conf:ro" in svc["volumes"]
    conf = (ROOT / "deploy/lokkal/web/accord-stream.conf").read_text(encoding="utf-8")
    assert "proxy_pass core:8443;" in conf and "ssl" not in conf.replace("ssl_preread", "")  # TLS de bout en bout
