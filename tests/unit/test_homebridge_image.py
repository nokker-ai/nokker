"""Image de la passerelle du foyer (deploy/lokkal/home-bridge/Dockerfile) : chaque paquet importé par le code de la
passerelle hors bibliothèque standard est installé ET importé au build. `websockets` manquait (07.11) : toute
opération Home Assistant par WebSocket répondait 502."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "deploy" / "lokkal" / "home-bridge" / "Dockerfile").read_text(encoding="utf-8")


def test_websockets_installed_and_imported_at_build():
    assert re.search(r'pip install[^\n]*"websockets==[0-9.]+"', DOCKERFILE)
    assert "websockets.sync.client" in DOCKERFILE  # vérifié par le RUN python -c du build


def test_homebridge_code_uses_websockets_sync_client():
    # garde le test honnête : si le code cesse de l'utiliser, ce test le dira (et le paquet pourra partir)
    assert "websockets.sync.client" in (ROOT / "nokker" / "homebridge" / "ha.py").read_text(encoding="utf-8")
