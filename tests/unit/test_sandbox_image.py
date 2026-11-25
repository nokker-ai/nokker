"""Image des bacs à sable de Lokkal (deploy/lokkal/sandbox/Dockerfile) : les commandes qu'un modèle tape d'instinct
existent (`python`, `pytest`). La vérification à l'exécution est faite par le banc de l'emballage (image construite)."""
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy" / "lokkal" / "sandbox" / "Dockerfile"


def test_python_is_python3_installed():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "python-is-python3" in text  # `python` → python3, sinon « python: command not found »


def test_pytest_preinstalled():
    assert "pip install pytest" in DOCKERFILE.read_text(encoding="utf-8")
