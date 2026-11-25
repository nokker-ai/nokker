"""Bac à sable SIMULÉ : tient la place de `docker exec -i -e NOM… <container> <argv>` dans les tests unitaires.

Même contrat que docker pour l'environnement : le processus lancé ne reçoit QUE les variables nommées par `-e NOM`
(valeur lue dans l'environnement de ce lanceur), jamais le reste de l'environnement de l'appelant."""
import os
import sys

args = sys.argv[1:]
env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
while args[:1] == ["-e"]:
    name = args[1]
    if name in os.environ:
        env[name] = os.environ[name]
    args = args[2:]
# Ce que voit le CLIENT (ce lanceur), noté pour les tests : les réglages `DOCKER_*` du noyau doivent lui parvenir.
if os.path.isdir(args[-1]):
    import json

    with open(os.path.join(args[-1], f"client-{os.getpid()}.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in os.environ.items() if k.startswith("DOCKER_")}, f)
os.execvpe(args[0], args, env)
