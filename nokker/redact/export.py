"""`python -m nokker.redact.export > rules.json` : écrit les octets EXACTS de `GET /v1/redact/rules` (même JSON, même
version), sans serveur ni configuration. Sert au test de dérive de l'app (nokker-apps, `NOKKER_CORE_REDACT_RULES`)."""
from __future__ import annotations

import sys

from nokker.redact.rules import rules_json


def main() -> None:
    sys.stdout.buffer.write(rules_json()[0])


if __name__ == "__main__":
    main()
