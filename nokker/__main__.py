"""`python -m nokker` — pas de binaire `nokker` installé par ce paquet (voir `pyproject.toml` : le nom
est déjà pris par le binaire de la dépendance Hermes épinglée, collision trouvée en réel le 17.10.2026)."""
from __future__ import annotations

from nokker.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
