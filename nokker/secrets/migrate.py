"""Migration d'un fournisseur à l'autre (`nokker vault migrate --from file --to openbao`, et le sens inverse).

Pour chaque portée (`system` puis `user`) et chaque nom de la source :
  - absent de la destination → écrit, puis RELU et comparé (`copied`) ; relu différent → `failed` ;
  - présent avec la même valeur → rien (`same`) : relancer la migration ne change rien (idempotente) ;
  - présent avec une autre valeur → signalé (`divergent`), JAMAIS écrasé, sauf `overwrite=True` (`overwritten`).
`check=True` : à blanc, rien n'est écrit. La source n'est jamais modifiée (pas d'effacement : on bascule le
fournisseur, on vérifie, puis l'utilisateur efface l'ancien coffre s'il le veut). Le rapport ne porte que des noms.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nokker.secrets.base import SecretsProvider


@dataclass
class MigrationReport:
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (portée, nom, état)

    def count(self, state: str) -> int:
        return sum(1 for r in self.rows if r[2] == state)

    @property
    def ok(self) -> bool:
        return self.count("divergent") == 0 and self.count("failed") == 0

    def render(self) -> str:
        lines = [f"{scope:<6}  {name:<40}  {state}" for scope, name, state in self.rows]
        states = ("copied", "same", "divergent", "overwritten", "failed", "would-copy", "would-overwrite")
        lines.append("bilan : " + ", ".join(f"{s} {self.count(s)}" for s in states if self.count(s)) if self.rows
                     else "bilan : coffre source vide")
        if self.count("divergent"):
            lines.append("valeurs divergentes NON écrasées : vérifier, puis relancer avec --overwrite si la source "
                         "fait foi")
        return "\n".join(lines)


def migrate(pairs: list[tuple[SecretsProvider, SecretsProvider]], *, overwrite: bool = False,
            check: bool = False) -> MigrationReport:
    report = MigrationReport()
    for src, dst in pairs:
        for info in src.list():
            value = src.get(info.name)
            if value is None:  # effacé entre la liste et la lecture
                continue
            current = dst.get(info.name)
            if current == value:
                state = "same"
            elif current is not None and not overwrite:
                state = "divergent"
            elif check:
                state = "would-overwrite" if current is not None else "would-copy"
            else:
                dst.set(info.name, value)
                state = ("overwritten" if current is not None else "copied") if dst.get(info.name) == value \
                    else "failed"
            report.rows.append((src.scope, info.name, state))
    return report


__all__ = ["MigrationReport", "migrate"]
