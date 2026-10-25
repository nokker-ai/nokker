"""`python -m nokker` — pas de binaire `nokker` installé par ce paquet (voir `pyproject.toml` : le nom
est déjà pris par le binaire de la dépendance amont épinglée, collision trouvée en réel le 17.10.2026)."""
from __future__ import annotations

import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["review"]:
        # X13 : `python -m nokker review` sans charger le noyau (serveur, canaux, Postgres) — l'image d'action n'a
        # besoin que du paquet de relecture.
        from nokker.review.cli import main as review_main

        raise SystemExit(review_main(sys.argv[2:]))
    from nokker.cli import main

    raise SystemExit(main())
