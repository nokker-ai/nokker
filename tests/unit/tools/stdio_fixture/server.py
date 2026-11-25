"""Petit serveur MCP stdio de test, écrit avec le SDK officiel (`mcp.server.mcpserver`) et lancé pour de vrai par les
tests de `nokker/tools/mcp_stdio.py`. Il ne dépend que du SDK : il tourne aussi dans un bac à sable docker.

argv[1] = dossier de preuves : le serveur y écrit ce qu'il voit de SON côté (environnement, pid, appels), pour que
les tests le lisent sans passer par ce que voit le modèle."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

PROOF = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp")
WHO = os.environ.get("TEST_SESSION_USER", "")
server = MCPServer("nokkan-stdio-test")


def _note(kind: str) -> None:
    with open(PROOF / f"calls-{os.getpid()}.log", "a", encoding="utf-8") as f:
        f.write(kind + "\n")


@server.tool(description="Renvoie le texte.")
def echo(text: str) -> str:
    _note("echo")
    return text


# Un serveur qui se décrit avec son identité : le catalogue du noyau ne doit pas la garder.
@server.tool(description=f"Notes de {WHO or 'personne'}.")
def notes_list() -> str:
    return "aucune note"


@server.tool(description="Échoue en citant son contexte.")
def boom() -> str:
    raise ToolError(f"panne pour {WHO} (jeton {os.environ.get('TEST_API_TOKEN', '')})")


@server.tool(description="Rend le secret du coffre (un serveur indiscret).")
def leak() -> str:
    return f"jeton={os.environ.get('TEST_API_TOKEN', '')}"


@server.tool(description="Un serveur bavard : rend son identité dans un résultat NORMAL.")
def whoami() -> str:
    return f"session de {WHO}"


@server.tool(description="Rend plusieurs blocs de contenu ; l'identité est dans le second.")
def blocks() -> list[str]:
    return ["premier bloc, sans rien", f"second bloc, pour {WHO}"]


@server.tool(description="Parle du projet de la session (un nom légitime, identique à une valeur d'identité).")
def project() -> str:
    return f"3 notes dans le projet {os.environ.get('TEST_SESSION_PROJECT', '')}"


@server.tool(description="Le processus meurt pendant l'appel.")
def die() -> str:
    _note("die")
    os._exit(3)


@server.tool(description="Dort.")
def slow(seconds: float) -> str:
    time.sleep(seconds)
    return "réveillé"


@server.tool(description="Rend le pid du serveur.")
def pid() -> str:
    return str(os.getpid())


if __name__ == "__main__":
    PROOF.mkdir(parents=True, exist_ok=True)
    (PROOF / f"env-{os.getpid()}.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    server.run("stdio")
